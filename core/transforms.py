"""Video validation, shared admission limits, and the external service adapter."""
import io
import math
import os
import socket
import time
import uuid
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import av
from django.conf import settings
from django.db import transaction

from .models import TransformLimits

MAX_FILE_BYTES = 3_000_000
MAX_REQUEST_BYTES = 3_200_000
MAX_OUTPUT_BYTES = 4_000_000
MAX_DURATION = 10


class TransformError(Exception):
    # Store a client-facing error, HTTP status, and optional retry delay.
    def __init__(self, message, status=400, retry_after=None):
        self.message, self.status, self.retry_after = message, status, retry_after
        super().__init__(message)


@contextmanager
# Lock the shared limiter row while reading and updating its state.
def locked_limits(limiter_id=1):
    TransformLimits.objects.get_or_create(pk=limiter_id)
    with transaction.atomic():
        # An UPDATE obtains SQLite's write lock before reading the shared state.
        TransformLimits.objects.filter(pk=limiter_id).update(touched=time.time())
        state = TransformLimits.objects.get(pk=limiter_id)
        yield state
        state.save(update_fields=['arrivals', 'slots'])


# Enforce the shared rolling limit of twelve requests per second.
def admit_request(limiter_id=1):
    with locked_limits(limiter_id) as state:
        now = time.time()
        state.arrivals = [t for t in state.arrivals if t > now - 1]
        if len(state.arrivals) >= 12:
            raise TransformError('Rate limit reached. Try again in one second.', 429, 1)
        state.arrivals.append(now)


@contextmanager
# Reserve one of six processing slots and always release it afterward.
def processing_slot():
    key = uuid.uuid4().hex
    with locked_limits() as state:
        # A long conversion keeps its slot. Reclaim only slots of exited workers.
        live = {}
        for slot, owner in state.slots.items():
            try:
                os.kill(owner['pid'], 0)
                live[slot] = owner
            except ProcessLookupError:
                pass
            except PermissionError:
                live[slot] = owner
        state.slots = live
        if len(state.slots) >= 6:
            raise TransformError('All six processing slots are busy. Please retry shortly.', 429, 1)
        state.slots[key] = {'pid': os.getpid()}
    try:
        yield
    finally:
        with locked_limits() as state:
            state.slots.pop(key, None)


# Probe a source upload without imposing the per-clip size or duration limits.
def validate_source(upload):
    if upload is None or upload.size == 0:
        raise TransformError('Choose a non-empty video file.')
    try:
        with av.open(upload) as container:
            if not container.streams.video or next(container.decode(video=0), None) is None:
                raise TransformError('The uploaded file does not contain readable video.')
    except (av.FFmpegError, ValueError, OverflowError):
        raise TransformError('This file is not a valid, readable video.')
    finally:
        upload.seek(0)
    return upload


# Validate a video, optionally enforcing the external service duration limit.
def validate_video(upload, max_duration=MAX_DURATION, max_file_bytes=MAX_FILE_BYTES):
    if upload is None or upload.size == 0:
        raise TransformError('Choose a non-empty video file.')
    if upload.size > max_file_bytes:
        raise TransformError('Video must be 3 MB or smaller.', 413)
    data = upload.read(max_file_bytes + 1)
    if len(data) > max_file_bytes:
        raise TransformError('Video must be 3 MB or smaller.', 413)
    try:
        with av.open(io.BytesIO(data)) as container:
            if not container.streams.video:
                raise TransformError('The uploaded file does not contain video.')
            stream = container.streams.video[0]
            if stream.duration is not None:
                duration = float(stream.duration * stream.time_base)
                if not math.isfinite(duration) or (max_duration is not None and duration > max_duration):
                    raise TransformError('Video must be 10 seconds or shorter.')
            # Decode as well: don't trust the extension, MIME type, or metadata alone.
            first = None
            frames = 0
            for frame in container.decode(stream):
                frames += 1
                if frame.time is None:
                    raise TransformError('Video timestamps could not be validated.')
                if first is None:
                    first = frame.time
                frame_length = float(frame.duration * frame.time_base) if frame.duration else (float(1 / stream.average_rate) if stream.average_rate else 0)
                if max_duration is None:
                    return data  # Full decoding and segment validation happen in the worker.
                if frame.time - first + frame_length > max_duration + 0.000001:
                    raise TransformError('Video must be 10 seconds or shorter.')
            if not frames:
                raise TransformError('The video contains no readable frames.')
    except TransformError:
        raise
    except (av.FFmpegError, ValueError, OverflowError):
        raise TransformError('This file is not a valid, readable video.')
    return data


# Send the video to the external service and validate its response.
def call_transform_service(data):
    if not settings.TRANSFORM_SERVICE_TOKEN:
        raise TransformError('The transformation service token is not configured.', 503)
    if len(data) > MAX_FILE_BYTES:
        raise TransformError('Each service clip must be 3 MB or smaller.', 413)
    boundary = uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="clip"; filename="video.mp4"\r\nContent-Type: video/mp4\r\n\r\n'.encode() + data + f'\r\n--{boundary}--\r\n'.encode())
    if len(body) > MAX_REQUEST_BYTES:
        raise TransformError('Each service request must be 3.2 MB or smaller.', 413)
    request = Request(settings.TRANSFORM_BASE_URL.rstrip('/') + '/api/transform', data=body, headers={
        'Authorization': 'Bearer ' + settings.TRANSFORM_SERVICE_TOKEN,
        'Content-Type': 'multipart/form-data; boundary=' + boundary,
    }, method='POST')
    try:
        with urlopen(request, timeout=30) as response:
            content_type = response.headers.get_content_type()
            if not content_type.startswith('video/'):
                raise TransformError('The service returned an unexpected response.', 502)
            result = response.read(MAX_OUTPUT_BYTES + 1)
            if not result or len(result) > MAX_OUTPUT_BYTES:
                raise TransformError('The service returned an invalid output size.', 502)
            return result, content_type
    except HTTPError as exc:
        raise TransformError(f'The transformation service rejected the request (HTTP {exc.code}).', 502)
    except (TimeoutError, socket.timeout):
        raise TransformError('The transformation service timed out. Please retry.', 504)
    except URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise TransformError('The transformation service timed out. Please retry.', 504)
        raise TransformError('Could not reach the transformation service. Please retry.', 502)
    except OSError:
        raise TransformError('Could not reach the transformation service. Please retry.', 502)


# Run one independent conversion while holding a processing slot.
def transform_video(data):
    with processing_slot():
        admit_request(limiter_id=2)
        return call_transform_service(data)
