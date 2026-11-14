"""Boundary and upstream-contract tests; all external HTTP calls are mocked."""
import io
from email.message import Message
from unittest.mock import patch
from urllib.error import URLError

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from .models import TransformLimits
from .tests import video_bytes
from .transforms import (MAX_FILE_BYTES, MAX_OUTPUT_BYTES, TransformError, admit_request,
                         call_transform_service, processing_slot, validate_source, validate_video)


class VideoValidationTests(TestCase):
    # Allow the exact service size boundary but reject one additional byte.
    def test_exact_clip_size_boundary(self):
        video = video_bytes()
        exact = video + b'\0' * (MAX_FILE_BYTES - len(video))
        self.assertEqual(validate_video(SimpleUploadedFile('clip.mp4', exact)), exact)
        with self.assertRaises(TransformError) as caught:
            validate_video(SimpleUploadedFile('clip.mp4', exact + b'\0'))
        self.assertEqual(caught.exception.status, 413)

    # Reject renamed text regardless of its extension or claimed MIME type.
    def test_spoofed_video_is_rejected(self):
        upload = SimpleUploadedFile('looks-valid.mp4', b'not a video', content_type='video/mp4')
        with self.assertRaises(TransformError):
            validate_source(upload)
        self.assertEqual(upload.tell(), 0)

    # Restore the source pointer after probing so saving does not lose the initial bytes.
    def test_source_probe_rewinds_and_accepts_long_video(self):
        content = video_bytes(150)
        upload = SimpleUploadedFile('source.mov', content, content_type='video/quicktime')
        self.assertIs(validate_source(upload), upload)
        self.assertEqual(upload.read(), content)


    # Reject a valid audio container that has no video stream at all.
    def test_audio_only_upload_is_rejected(self):
        import tempfile
        from pathlib import Path
        from .video_queue import run_ffmpeg
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'audio.m4a'
            run_ffmpeg(['-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.1', '-c:a', 'aac', source])
            with self.assertRaisesMessage(TransformError, 'readable video'):
                validate_source(SimpleUploadedFile('renamed.mp4', source.read_bytes(), content_type='video/mp4'))


@override_settings(TRANSFORM_SERVICE_TOKEN='test-only-token', TRANSFORM_BASE_URL='https://service.example/')
class ServiceContractTests(TestCase):
    # Return an in-memory HTTP response with real parsed Content-Type headers.
    def response(self, content, content_type='video/mp4'):
        response = io.BytesIO(content)
        response.headers = Message()
        response.headers['Content-Type'] = content_type
        return response

    # Verify authentication, multipart naming, binary bytes, URL, and timeout together.
    def test_outgoing_request_contract(self):
        clip = b'\x00\xff\r\nvideo-data'
        with patch('core.transforms.urlopen', return_value=self.response(b'result')) as http:
            self.assertEqual(call_transform_service(clip), (b'result', 'video/mp4'))
        request = http.call_args.args[0]
        self.assertEqual(request.full_url, 'https://service.example/api/transform')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-only-token')
        self.assertIn(b'name="clip"; filename="video.mp4"', request.data)
        self.assertIn(b'Content-Type: video/mp4\r\n\r\n' + clip, request.data)
        self.assertLessEqual(len(request.data), 3_200_000)
        self.assertEqual(http.call_args.kwargs['timeout'], 30)

    # Missing credentials must fail before any outgoing network request is made.
    @override_settings(TRANSFORM_SERVICE_TOKEN='')
    def test_missing_credentials_never_calls_service(self):
        with patch('core.transforms.urlopen') as http, self.assertRaises(TransformError) as caught:
            call_transform_service(b'video')
        self.assertEqual(caught.exception.status, 503)
        http.assert_not_called()

    # Reject oversized clips locally even if a caller bypasses normal validation.
    def test_oversized_clip_never_calls_service(self):
        with patch('core.transforms.urlopen') as http, self.assertRaises(TransformError) as caught:
            call_transform_service(b'x' * (MAX_FILE_BYTES + 1))
        self.assertEqual(caught.exception.status, 413)
        http.assert_not_called()

    # Treat HTML, empty bodies, and oversized outputs as upstream failures.
    def test_invalid_service_responses(self):
        for content, mime in [(b'error page', 'text/html'), (b'', 'video/mp4'),
                              (b'x' * (MAX_OUTPUT_BYTES + 1), 'video/mp4')]:
            with self.subTest(length=len(content), mime=mime):
                with patch('core.transforms.urlopen', return_value=self.response(content, mime)), self.assertRaises(TransformError) as caught:
                    call_transform_service(b'video')
                self.assertEqual(caught.exception.status, 502)

    # Accept the guaranteed output upper bound without accidentally applying the input bound.
    def test_exact_output_size_boundary(self):
        content = b'x' * MAX_OUTPUT_BYTES
        with patch('core.transforms.urlopen', return_value=self.response(content)):
            self.assertEqual(len(call_transform_service(b'video')[0]), MAX_OUTPUT_BYTES)

    # Do not leak credentials when the remote host is unavailable.
    @patch('core.transforms.urlopen', side_effect=URLError('unreachable'))
    def test_connection_error_is_safe(self, http):
        with self.assertRaises(TransformError) as caught:
            call_transform_service(b'video')
        self.assertEqual(caught.exception.status, 502)
        self.assertNotIn('test-only-token', str(caught.exception))

    # urllib-wrapped socket timeouts must retain timeout semantics rather than becoming 502s.
    @patch('core.transforms.urlopen', side_effect=URLError(TimeoutError('timed out')))
    def test_wrapped_timeout_maps_to_504(self, http):
        with self.assertRaises(TransformError) as caught:
            call_transform_service(b'video')
        self.assertEqual(caught.exception.status, 504)


class LimiterEdgeTests(TestCase):
    # An arrival exactly one second old must no longer occupy the rolling window.
    def test_exact_window_boundary(self):
        TransformLimits.objects.create(pk=1, arrivals=[100.0] * 12)
        with patch('core.transforms.time.time', return_value=101.0):
            admit_request()
        self.assertEqual(TransformLimits.objects.get(pk=1).arrivals, [101.0])

    # Exhausting upload admission must not consume the external service's separate quota.
    def test_upload_and_service_quotas_are_independent(self):
        with patch('core.transforms.time.time', return_value=100.0):
            for _ in range(12):
                admit_request(limiter_id=1)
            admit_request(limiter_id=2)
        self.assertEqual(len(TransformLimits.objects.get(pk=2).arrivals), 1)

    # Reclaim slots owned by dead processes instead of rejecting every subsequent request.
    @patch('core.transforms.os.kill', side_effect=ProcessLookupError())
    def test_dead_worker_slots_are_reclaimed(self, kill):
        TransformLimits.objects.create(pk=1, slots={str(i): {'pid': 999999} for i in range(6)})
        with processing_slot():
            self.assertEqual(len(TransformLimits.objects.get(pk=1).slots), 1)
        self.assertEqual(TransformLimits.objects.get(pk=1).slots, {})

    # Permission errors do not prove a worker exited and must not free its occupied slots.
    @patch('core.transforms.os.kill', side_effect=PermissionError())
    def test_inaccessible_live_workers_keep_their_slots(self, kill):
        TransformLimits.objects.create(pk=1, slots={str(i): {'pid': 123} for i in range(6)})
        with self.assertRaises(TransformError) as caught:
            with processing_slot():
                self.fail('A seventh slot was admitted.')
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(len(TransformLimits.objects.get(pk=1).slots), 6)
