"""Persistent segment queue, retry bookkeeping, and one final ordered merge."""
import os
import shutil
import subprocess
import time
from pathlib import Path

import av
import imageio_ffmpeg
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, transaction
from django.db.models import F

from .models import VideoJob, VideoSegment
from .transforms import MAX_OUTPUT_BYTES, TransformError, transform_video, validate_video

MAX_ATTEMPTS = 3


# Run a bounded FFmpeg operation without a shell or exposing service credentials.
def run_ffmpeg(arguments):
    try:
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', *map(str, arguments)], check=True, capture_output=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise TransformError('Local video processing timed out.', 500)
    except subprocess.CalledProcessError:
        raise TransformError('Could not split or merge this video.', 500)


# Save an upload before publishing a durable queued job.
def enqueue_video(data):
    video = VideoJob()
    directory = Path(settings.VIDEO_JOB_ROOT) / str(video.id)
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / 'original.mp4'
    try:
        with source.open('wb') as target:
            if hasattr(data, 'chunks'):
                for chunk in data.chunks():
                    target.write(chunk)
            else:
                target.write(data)
        video.input_location = str(source)
        video.save()
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return video


# Encode independent clips below ten seconds and validate each before enqueueing.
def split_video(video):
    directory = Path(video.input_location).parent / 'segments'
    directory.mkdir(exist_ok=True)
    for old in directory.glob('input-*.mp4'):
        old.unlink()
    # 9.9 seconds leaves room for AAC packet rounding under the service's 10s limit.
    run_ffmpeg(['-i', video.input_location, '-map', '0:v:0', '-map', '0:a:0?',
                '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1',
                '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
                '-r', '30', '-bf', '0', '-b:v', '1400k', '-maxrate', '1600k', '-bufsize', '1600k',
                '-c:a', 'aac', '-b:a', '96k', '-ar', '48000', '-ac', '2',
                '-force_key_frames', 'expr:gte(t,n_forced*9.9)', '-f', 'segment',
                '-segment_time', '9.9', '-reset_timestamps', '1', '-segment_start_number', '1',
                directory / 'input-%06d.mp4'])
    paths = sorted(directory.glob('input-*.mp4'))
    if not paths:
        raise TransformError('No video segments could be produced.')
    for path in paths:
        validate_video(SimpleUploadedFile(path.name, path.read_bytes(), content_type='video/mp4'))
    with transaction.atomic():
        video.segments.all().delete()
        VideoSegment.objects.bulk_create([
            VideoSegment(video=video, segment_order=index, input_location=str(path))
            for index, path in enumerate(paths, 1)
        ])
        VideoJob.objects.filter(pk=video.pk, status='splitting').update(
            expected_segments=len(paths), status='processing', owner_pid=None, error='')


# Publish file contents atomically so database records never point at partial outputs.
def save_output(path, content):
    temporary = path.with_suffix('.partial.mp4')
    temporary.write_bytes(content)
    temporary.replace(path)


# Retry a transient segment error without discarding any successful sibling outputs.
def fail_segment(segment, error):
    transient = not isinstance(error, TransformError) or error.status >= 500 or error.status == 429
    busy = isinstance(error, TransformError) and error.status == 429
    retry = transient and (busy or segment.attempts < MAX_ATTEMPTS)
    updates = dict(status='pending' if retry else 'failed', error=str(error), owner_pid=None,
                   available_at=time.time() + (1 if busy else 2 ** segment.attempts))
    if busy:
        updates['attempts'] = F('attempts') - 1
    VideoSegment.objects.filter(pk=segment.pk, status='processing').update(**updates)


# Transform one claimed segment and record its output exactly once.
def process_segment(segment):
    try:
        if segment.video.status == 'failed':
            VideoSegment.objects.filter(pk=segment.pk).update(status='cancelled', owner_pid=None)
            return
        content, _ = transform_video(Path(segment.input_location).read_bytes())
        # The provider must return a readable clip, not merely a video Content-Type.
        try:
            validate_video(SimpleUploadedFile('processed.mp4', content), max_duration=None, max_file_bytes=MAX_OUTPUT_BYTES)
        except TransformError:
            raise TransformError('The service returned an unreadable video clip.', 502)
        output = Path(segment.input_location).with_name(f'output-{segment.segment_order:06d}.mp4')
        save_output(output, content)
        VideoSegment.objects.filter(pk=segment.pk, status='processing').update(
            status='completed', output_location=str(output), error='', owner_pid=None)
    except Exception as exc:
        fail_segment(segment, exc)


# Concatenate the complete ordered manifest once and atomically publish the result.
def merge_video(video):
    segments = list(video.segments.all())
    if len(segments) != video.expected_segments or any(
        item.status != 'completed' or item.segment_order != index or not item.output_location
        for index, item in enumerate(segments, 1)
    ):
        raise TransformError('Cannot merge: some expected segments are missing.', 500)
    directory = Path(video.input_location).parent
    manifest = directory / 'concat.txt'
    # Filenames are generated internally; relative paths avoid quoting user input.
    manifest.write_text(''.join(f"file 'segments/output-{s.segment_order:06d}.mp4'\n" for s in segments))
    temporary = directory / 'merged.partial.mp4'
    signatures = []
    durations = []
    for segment in segments:
        with av.open(segment.output_location) as container:
            stream = container.streams.video[0]
            codec = stream.codec_context
            durations.append(float(stream.duration * stream.time_base) if stream.duration is not None else float(container.duration / av.time_base))
            signatures.append((codec.name, codec.width, codec.height, codec.format.name,
                               stream.time_base, codec.extradata))
    if all(signature == signatures[0] for signature in signatures):
        arguments = ['-f', 'concat', '-safe', '1', '-i', manifest,
                     '-i', video.input_location, '-map', '0:v:0', '-map', '1:a:0?', '-c:v', 'copy']
    else:
        # Normalize differing service outputs in a single decode-and-encode concatenation.
        arguments = []
        width, height = signatures[0][1:3]
        filters = []
        for index, segment in enumerate(segments):
            arguments.extend(['-i', segment.output_location])
            filters.append(f'[{index}:v:0]scale={width}:{height},setsar=1,setpts=PTS-STARTPTS,fps=30,tpad=stop_mode=clone:stop_duration={durations[index]},trim=duration={durations[index]},setpts=PTS-STARTPTS[v{index}]')
        filters.append(''.join(f'[v{i}]' for i in range(len(segments))) + f'concat=n={len(segments)}:v=1:a=0[out]')
        arguments.extend(['-i', video.input_location, '-filter_complex', ';'.join(filters),
                          '-map', '[out]', '-map', f'{len(segments)}:a:0?',
                          '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20'])
    # Restore original audio because the external grayscale service removes it.
    run_ffmpeg([*arguments, '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', temporary])
    # Decode the result to catch unreadable output before advertising completion.
    run_ffmpeg(['-i', temporary, '-map', '0:v:0', '-map', '0:a:0?', '-f', 'null', '-'])
    output = directory / 'grayscale.mp4'
    temporary.replace(output)
    VideoJob.objects.filter(pk=video.pk, status='merging').update(
        status='completed', final_output=str(output), owner_pid=None, error='')


# Derive terminal failure or merge readiness from durable segment records.
def reconcile_video(video):
    if video.segments.filter(status='failed').exists():
        error = video.segments.filter(status='failed').first().error
        VideoJob.objects.filter(pk=video.pk, status='processing').update(status='failed', error=error)
        video.segments.filter(status='pending').update(status='cancelled')
    elif video.expected_segments and video.segments.count() == video.expected_segments and not video.segments.exclude(status='completed').exists():
        # Conditional transition prevents duplicate merge jobs across workers.
        VideoJob.objects.filter(pk=video.pk, status='processing').update(status='merge_pending', error='')


# Determine whether a recorded local worker process still exists.
def process_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# Requeue work abandoned by exited workers without reprocessing successful segments.
def recover_abandoned():
    for video in VideoJob.objects.filter(status__in=['splitting', 'merging']):
        if not process_alive(video.owner_pid):
            VideoJob.objects.filter(pk=video.pk, status=video.status, owner_pid=video.owner_pid).update(
                status='queued' if video.status == 'splitting' else 'merge_pending', owner_pid=None)
    for segment in VideoSegment.objects.filter(status='processing'):
        if not process_alive(segment.owner_pid):
            VideoSegment.objects.filter(pk=segment.pk, status='processing', owner_pid=segment.owner_pid).update(
                status='pending', owner_pid=None)


# Claim a queued item using an atomic conditional update across worker processes.
def claim(queryset, status, attempt_field=None):
    candidate = queryset.order_by(*(['created_at', 'pk'] if queryset.model is VideoJob else ['pk'])).first()
    if candidate is None:
        return None
    updates = {'status': status, 'owner_pid': os.getpid()}
    if attempt_field:
        updates[attempt_field] = F(attempt_field) + 1
    if queryset.filter(pk=candidate.pk, status=candidate.status).update(**updates):
        candidate.refresh_from_db()
        return candidate
    return None


# Select durable queue work, reconciling completion before claiming new tasks.
def next_task():
    now = time.time()
    for video in VideoJob.objects.filter(status='processing'):
        reconcile_video(video)
    video = claim(VideoJob.objects.filter(status='merge_pending', available_at__lte=now), 'merging', 'merge_attempts')
    if video:
        return 'merge', video
    segment = claim(VideoSegment.objects.filter(status='pending', available_at__lte=now, video__status='processing'), 'processing', 'attempts')
    if segment:
        return 'segment', segment
    video = claim(VideoJob.objects.filter(status='queued'), 'splitting')
    if video:
        return 'split', video
    return None


# Execute a claimed task with isolated database connections and persistent failure state.
def execute_task(kind, record):
    close_old_connections()
    try:
        if kind == 'segment':
            process_segment(record)
        elif kind == 'split':
            split_video(record)
        else:
            merge_video(record)
    except Exception as exc:
        if kind == 'segment':
            fail_segment(record, exc)
        elif kind == 'merge' and record.merge_attempts < MAX_ATTEMPTS:
            VideoJob.objects.filter(pk=record.pk, status='merging').update(
                status='merge_pending', owner_pid=None, error=str(exc), available_at=time.time() + 2 ** record.merge_attempts)
        else:
            VideoJob.objects.filter(pk=record.pk).update(status='failed', owner_pid=None, error=str(exc))
    finally:
        close_old_connections()
