from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase
from django.urls import reverse


class StarterTests(TestCase):
    # Check that the upload page includes its template and frontend script.
    def test_home_renders_template_and_frontend(self):
        response = self.client.get(reverse('home'))
        self.assertTemplateUsed(response, 'core/home.html')
        self.assertContains(response, 'Grayscale studio')
        self.assertContains(response, '/static/core/app.js')

    # Check that the greeting endpoint returns the expected JSON.
    def test_hello_api(self):
        response = self.client.get(reverse('hello'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'message': 'Hello, world! From your Python backend.'})

    # Check that health reporting executes a real database query.
    def test_health_checks_database(self):
        with self.assertNumQueries(1):
            response = self.client.get(reverse('health'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok', 'database': 'connected'})

    # Check that database failures produce an unavailable health response.
    def test_health_reports_database_failure(self):
        with patch('core.views.connection.cursor', side_effect=DatabaseError('unavailable')):
            response = self.client.get(reverse('health'))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['database'], 'unavailable')

    # Check that read-only starter endpoints reject POST requests.
    def test_endpoints_reject_writes(self):
        for name in ('home', 'hello', 'health'):
            with self.subTest(name=name):
                self.assertEqual(self.client.post(reverse(name)).status_code, 405)


import io
from contextlib import ExitStack
from urllib.error import HTTPError

import av
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings

from .models import TransformLimits
from .transforms import (TransformError, admit_request, processing_slot,
                         validate_video, call_transform_service, transform_video)


# Generate a tiny MP4 with a controlled number of frames for testing.
def video_bytes(frames=5, width=16):
    output = io.BytesIO()
    with av.open(output, mode='w', format='mp4') as container:
        stream = container.add_stream('libx264', rate=10)
        stream.width = width
        stream.height = 16
        stream.pix_fmt = 'yuv420p'
        for _ in range(frames):
            frame = av.VideoFrame(width, 16, 'yuv420p')
            for plane in frame.planes:
                plane.update(bytes([128]) * plane.buffer_size)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return output.getvalue()


class TransformTests(TestCase):
    @classmethod
    # Create a reusable valid video for upload tests.
    def setUpTestData(cls):
        cls.video = video_bytes()

    # Isolate uploaded test assets from the real local queue.
    def setUp(self):
        import tempfile
        self.storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        self.settings_override = override_settings(VIDEO_JOB_ROOT=self.storage.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    # Submit a video to the transform endpoint using a multipart request.
    def upload(self, data=None):
        return self.client.post('/api/transform/', {'clip': SimpleUploadedFile('clip.mp4', self.video if data is None else data, content_type='video/mp4')})

    # Check that missing, empty, and unreadable videos are rejected.
    def test_missing_empty_and_invalid_files(self):
        self.assertEqual(self.client.post('/api/transform/').status_code, 400)
        for content in (b'', b'not a video'):
            self.assertEqual(self.upload(content).status_code, 400)

    # Check that the per-clip size limit still rejects oversized service clips.
    def test_file_size_limit(self):
        with self.assertRaises(TransformError) as caught:
            validate_video(SimpleUploadedFile('large.mp4', b'x' * 3_000_001))
        self.assertEqual(caught.exception.status, 413)

    # Check that valid source uploads above both former upload limits are accepted.
    def test_large_source_upload_is_accepted(self):
        content = video_bytes(150) + b'\0' * 3_300_000
        response = self.upload(content)
        self.assertEqual(response.status_code, 202)
        video = VideoJob.objects.get(pk=response.json()['video_id'])
        self.assertEqual(Path(video.input_location).stat().st_size, len(content))

    # Check that ten-second videos pass and longer videos fail.
    def test_duration_boundary(self):
        validate_video(SimpleUploadedFile('exact.mp4', video_bytes(100)))
        with self.assertRaisesMessage(TransformError, '10 seconds'):
            validate_video(SimpleUploadedFile('long.mp4', video_bytes(101)))

    # Check that the thirteenth request fails until the rate window resets.
    def test_rate_limit_and_window_reset(self):
        with patch('core.transforms.time.time', return_value=100):
            for _ in range(12):
                admit_request()
            with self.assertRaises(TransformError) as caught:
                admit_request()
            self.assertEqual(caught.exception.status, 429)
        with patch('core.transforms.time.time', return_value=101.01):
            admit_request()

    # Check that only six slots can be held and released slots become available.
    def test_six_slots_and_release(self):
        with ExitStack() as stack:
            for _ in range(6):
                stack.enter_context(processing_slot())
            with self.assertRaises(TransformError) as caught:
                with processing_slot():
                    pass
            self.assertEqual(caught.exception.status, 429)
        self.assertEqual(TransformLimits.objects.get(pk=1).slots, {})
        with processing_slot():
            pass

    @patch('core.transforms.call_transform_service', side_effect=TransformError('Service failed', 502))
    # Check that a failed service call releases its concurrency slot.
    def test_service_failure_releases_slot(self, service):
        with self.assertRaises(TransformError):
            transform_video(self.video)
        self.assertEqual(TransformLimits.objects.get(pk=1).slots, {})

    # Check that uploads return a durable queued job rather than waiting for video bytes.
    def test_upload_returns_queued_job(self):
        response = self.upload()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['status'], 'queued')
        self.assertIsNone(response.json()['result_url'])
        status = self.client.get(response.json()['status_url'])
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()['video_id'], response.json()['video_id'])

    # Check that transformations require POST and a valid CSRF token.
    def test_method_and_csrf(self):
        self.assertEqual(self.client.get('/api/transform/').status_code, 405)
        response = Client(enforce_csrf_checks=True).post('/api/transform/')
        self.assertEqual(response.status_code, 403)
        self.assertIn('error', response.json())

    @override_settings(TRANSFORM_SERVICE_TOKEN='test-token')
    @patch('core.transforms.urlopen', side_effect=TimeoutError())
    # Check that service timeouts map to an HTTP 504 error.
    def test_service_timeout(self, call):
        with self.assertRaises(TransformError) as caught:
            call_transform_service(self.video)
        self.assertEqual(caught.exception.status, 504)

    @override_settings(TRANSFORM_SERVICE_TOKEN='test-token')
    @patch('core.transforms.urlopen', side_effect=HTTPError('https://example.test', 500, 'failed', {}, None))
    # Check that upstream HTTP failures map to an HTTP 502 error.
    def test_service_error(self, call):
        with self.assertRaises(TransformError) as caught:
            call_transform_service(self.video)
        self.assertEqual(caught.exception.status, 502)


from pathlib import Path
import tempfile
from django.db import IntegrityError, transaction
from .models import VideoJob, VideoSegment
from .video_queue import (enqueue_video, split_video, process_segment, reconcile_video,
                         merge_video, next_task, recover_abandoned, execute_task)


class QueueTests(TestCase):
    # Create isolated queue storage and a valid long video for each test.
    def setUp(self):
        self.storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        context = override_settings(VIDEO_JOB_ROOT=self.storage.name)
        context.enable()
        self.addCleanup(context.disable)

    # Drive one durable claimed task without closing the test transaction's connection.
    def run_next(self):
        task = next_task()
        self.assertIsNotNone(task)
        with patch('core.video_queue.close_old_connections'):
            execute_task(*task)
        return task

    # Verify long uploads split into ordered, valid clips and merge only after all succeed.
    @patch('core.transforms.call_transform_service')
    def test_long_video_queue_and_ordered_merge(self, service):
        service.side_effect = lambda data: (data, 'video/mp4')
        job = enqueue_video(video_bytes(213))
        self.run_next()
        job.refresh_from_db()
        self.assertEqual(job.status, 'processing')
        self.assertEqual(job.expected_segments, 3)
        self.assertEqual(list(job.segments.values_list('segment_order', flat=True)), [1, 2, 3])
        for segment in job.segments.all():
            validate_video(SimpleUploadedFile('segment.mp4', Path(segment.input_location).read_bytes()))
        claimed = [next_task() for _ in range(3)]
        # Complete in reverse order to prove finish order does not determine merge order.
        process_segment(claimed[2][1])
        reconcile_video(job)
        job.refresh_from_db()
        self.assertEqual(job.status, 'processing')
        self.assertIsNone(next_task())
        process_segment(claimed[1][1])
        process_segment(claimed[0][1])
        self.run_next()
        job.refresh_from_db()
        self.assertEqual(job.status, 'completed')
        self.assertTrue(Path(job.final_output).is_file())
        manifest = (Path(job.input_location).parent / 'concat.txt').read_text()
        self.assertLess(manifest.index('000001'), manifest.index('000002'))
        with av.open(job.final_output) as container:
            self.assertAlmostEqual(float(container.duration / av.time_base), 21.3, delta=0.2)
        response = self.client.get(reverse('video_result', args=[job.pk]))
        self.assertEqual(response.status_code, 200)
        response.close()
        self.assertIsNone(next_task())

    # Verify transient retries preserve successful clips and stop after three attempts.
    @patch('core.transforms.call_transform_service', side_effect=TransformError('Service unavailable', 502))
    def test_segment_retry_and_terminal_failure(self, service):
        job = enqueue_video(video_bytes())
        self.run_next()
        for attempt in range(1, 4):
            VideoSegment.objects.filter(video=job).update(available_at=0)
            self.run_next()
            segment = job.segments.get()
            self.assertEqual(segment.attempts, attempt)
            self.assertEqual(segment.status, 'pending' if attempt < 3 else 'failed')
        reconcile_video(job)
        job.refresh_from_db()
        self.assertEqual(job.status, 'failed')
        self.assertFalse(job.final_output)

    # Verify uniqueness and an incomplete segment set cannot trigger a merge.
    def test_duplicates_and_missing_segments(self):
        job = enqueue_video(video_bytes())
        job.status = 'processing'
        job.expected_segments = 2
        job.save()
        VideoSegment.objects.create(video=job, segment_order=1, status='completed', input_location='input', output_location='output')
        with self.assertRaises(IntegrityError), transaction.atomic():
            VideoSegment.objects.create(video=job, segment_order=1, input_location='duplicate')
        reconcile_video(job)
        job.refresh_from_db()
        self.assertEqual(job.status, 'processing')
        with self.assertRaises(TransformError):
            merge_video(job)

    # Verify abandoned tasks are recovered while live workers retain their claims.
    @patch('core.video_queue.process_alive', side_effect=lambda pid: pid == 123)
    def test_recovery_preserves_completed_segments(self, alive):
        job = enqueue_video(video_bytes())
        job.status = 'processing'
        job.save()
        ready = VideoSegment.objects.create(video=job, segment_order=1, status='completed', input_location='in', output_location='out')
        dead = VideoSegment.objects.create(video=job, segment_order=2, status='processing', owner_pid=456, input_location='in')
        live = VideoSegment.objects.create(video=job, segment_order=3, status='processing', owner_pid=123, input_location='in')
        recover_abandoned()
        dead.refresh_from_db()
        live.refresh_from_db()
        ready.refresh_from_db()
        self.assertEqual(dead.status, 'pending')
        self.assertEqual(live.status, 'processing')
        self.assertEqual(ready.output_location, 'out')
        self.assertEqual(ready.status, 'completed')

    # Verify a failed merge retries without sending completed clips through the API again.
    @patch('core.video_queue.merge_video', side_effect=TransformError('Merge failed', 500))
    def test_merge_retry_does_not_repeat_transformations(self, merge):
        job = enqueue_video(video_bytes())
        job.status = 'merge_pending'
        job.expected_segments = 1
        job.save()
        VideoSegment.objects.create(video=job, segment_order=1, status='completed', input_location='in', output_location='out')
        self.run_next()
        job.refresh_from_db()
        self.assertEqual(job.status, 'merge_pending')
        self.assertEqual(job.merge_attempts, 1)
        self.assertEqual(job.segments.get().status, 'completed')

    # Verify unfinished jobs do not expose a partial video as a completed result.
    def test_result_is_unavailable_before_completion(self):
        job = enqueue_video(video_bytes())
        response = self.client.get(reverse('video_result', args=[job.pk]))
        self.assertEqual(response.status_code, 409)

    # Verify original audio survives even when every transformed clip has no audio.
    @patch('core.transforms.call_transform_service')
    def test_original_audio_is_restored(self, service):
        from .video_queue import run_ffmpeg
        source = Path(self.storage.name) / 'audio-source.mp4'
        run_ffmpeg(['-f', 'lavfi', '-i', 'color=c=red:s=32x32:r=10:d=0.5',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.5',
                    '-c:v', 'libx264', '-c:a', 'aac', source])
        service.return_value = (video_bytes(), 'video/mp4')
        job = enqueue_video(source.read_bytes())
        self.run_next()
        self.run_next()
        self.run_next()
        job.refresh_from_db()
        self.assertEqual(job.status, 'completed')
        with av.open(job.final_output) as container:
            self.assertEqual(len(container.streams.audio), 1)
            self.assertGreater(len(list(container.decode(audio=0))), 0)

    # Verify incompatible output resolutions use normalized concatenation instead of stream copy.
    def test_different_output_formats_are_normalized(self):
        job = enqueue_video(video_bytes(10))
        directory = Path(job.input_location).parent / 'segments'
        directory.mkdir()
        for index, width in enumerate([16, 32], 1):
            path = directory / f'output-{index:06d}.mp4'
            path.write_bytes(video_bytes(width=width))
            VideoSegment.objects.create(video=job, segment_order=index, status='completed',
                                        input_location=str(path), output_location=str(path))
        job.status = 'merge_pending'
        job.expected_segments = 2
        job.save()
        self.run_next()
        job.refresh_from_db()
        self.assertEqual(job.status, 'completed')
        with av.open(job.final_output) as container:
            frames = list(container.decode(video=0))
            self.assertTrue(all(frame.width == 16 for frame in frames))
            self.assertAlmostEqual(container.duration / av.time_base, 1, delta=0.1)


class MediaDeliveryTests(TestCase):
    # Create a completed asset with predictable bytes for exact range assertions.
    def setUp(self):
        self.storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        path = Path(self.storage.name) / 'video.mp4'
        self.content = bytes(range(256))
        path.write_bytes(self.content)
        self.job = VideoJob.objects.create(status='completed', input_location=str(path), final_output=str(path))
        self.url = reverse('video_result', args=[self.job.pk])

    # Verify normal playback advertises byte-range support and returns the complete file.
    def test_full_video(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Accept-Ranges'], 'bytes')
        self.assertEqual(b''.join(response.streaming_content), self.content)
        response.close()

    # Verify bounded, open-ended, and suffix ranges return exactly the requested bytes.
    def test_single_ranges(self):
        for value, start, end in [('bytes=10-19', 10, 19), ('bytes=250-', 250, 255), ('bytes=-5', 251, 255), ('bytes=250-999', 250, 255)]:
            with self.subTest(value=value):
                response = self.client.get(self.url, HTTP_RANGE=value)
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response['Content-Range'], f'bytes {start}-{end}/256')
                self.assertEqual(int(response['Content-Length']), end-start+1)
                self.assertEqual(b''.join(response.streaming_content), self.content[start:end+1])
                response.close()

    # Verify invalid ranges fail explicitly rather than returning misleading partial content.
    def test_invalid_ranges(self):
        for value in ['bytes=999-', 'bytes=20-10', 'bytes=-0', 'bytes=-', 'bytes=0-1,3-4']:
            response = self.client.get(self.url, HTTP_RANGE=value)
            self.assertEqual(response.status_code, 416)
            self.assertEqual(response['Content-Range'], 'bytes */256')

    # Verify metadata-only requests return the correct size without a response body.
    def test_head(self):
        response = self.client.head(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Length'], '256')
        self.assertEqual(response.content, b'')

    # Verify restored original previews use the same byte-range delivery as results.
    def test_source_preview_range(self):
        response = self.client.get(reverse('video_source', args=[self.job.pk]), HTTP_RANGE='bytes=20-29')
        self.assertEqual(response.status_code, 206)
        self.assertEqual(b''.join(response.streaming_content), self.content[20:30])
        response.close()

    # Verify status responses expose a source URL for restoring both preview players.
    def test_status_exposes_source(self):
        response = self.client.get(reverse('video_status', args=[self.job.pk]))
        self.assertEqual(response.json()['source_url'], reverse('video_source', args=[self.job.pk]))
