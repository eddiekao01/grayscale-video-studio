"""Queue publication, retry, restart, and terminal-failure regression tests."""
import io
import tempfile
import av
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from .models import VideoJob, VideoSegment
from .tests import video_bytes
from .transforms import TransformError
from .video_queue import enqueue_video, execute_task, next_task, reconcile_video, recover_abandoned


class QueueFailureTests(TestCase):
    # Keep all test sources and results outside the real application's storage.
    def setUp(self):
        self.storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        context = override_settings(VIDEO_JOB_ROOT=self.storage.name)
        context.enable()
        self.addCleanup(context.disable)

    # Create a processing job with durable input clips for failure-path tests.
    def make_job(self, count=1):
        content = video_bytes()
        job = enqueue_video(content)
        job.status = 'processing'
        job.expected_segments = count
        job.save()
        directory = Path(job.input_location).parent / 'segments'
        directory.mkdir()
        for index in range(1, count + 1):
            path = directory / f'input-{index:06d}.mp4'
            path.write_bytes(content)
            VideoSegment.objects.create(video=job, segment_order=index, input_location=str(path))
        return job

    # Drive a claimed task without closing the transaction owned by Django's test runner.
    def execute(self, task):
        with patch('core.video_queue.close_old_connections'):
            execute_task(*task)

    # Save file uploads through their chunk interface without losing or duplicating bytes.
    def test_chunked_source_is_saved_exactly(self):
        content = video_bytes() + b'padding' * 10000
        upload = SimpleUploadedFile('source.mov', content)
        with patch.object(upload, 'chunks', return_value=iter([content[:10], content[10:500], content[500:]])):
            job = enqueue_video(upload)
        self.assertEqual(Path(job.input_location).read_bytes(), content)

    # Failed job creation must not leave a source directory that has no database record.
    @patch('core.video_queue.VideoJob.save', side_effect=RuntimeError('database unavailable'))
    def test_enqueue_failure_cleans_up_source_files(self, save):
        with self.assertRaises(RuntimeError):
            enqueue_video(video_bytes())
        self.assertEqual(list(Path(self.storage.name).iterdir()), [])
        self.assertEqual(VideoJob.objects.count(), 0)

    # Rate or concurrency saturation requeues a clip without consuming a service attempt.
    @patch('core.video_queue.transform_video', side_effect=TransformError('busy', 429, 1))
    def test_busy_response_preserves_retry_budget(self, transform):
        job = self.make_job()
        self.execute(next_task())
        segment = job.segments.get()
        self.assertEqual(segment.status, 'pending')
        self.assertEqual(segment.attempts, 0)
        self.assertIsNone(segment.owner_pid)
        self.assertIsNone(next_task())

    # Transient retries cannot run before their backoff deadline.
    @patch('core.video_queue.transform_video', side_effect=TransformError('temporary failure', 502))
    def test_retry_respects_backoff(self, transform):
        job = self.make_job()
        with patch('core.video_queue.time.time', return_value=100.0):
            self.execute(next_task())
            self.assertIsNone(next_task())
        with patch('core.video_queue.time.time', return_value=102.0):
            self.assertEqual(next_task()[1].pk, job.segments.get().pk)

    # A non-retryable failure stops immediately and retains its explanatory error.
    @patch('core.video_queue.transform_video', side_effect=TransformError('invalid clip', 400))
    def test_permanent_failure_does_not_retry(self, transform):
        job = self.make_job()
        self.execute(next_task())
        segment = job.segments.get()
        self.assertEqual((segment.status, segment.attempts), ('failed', 1))
        self.assertEqual(segment.error, 'invalid clip')

    # A fake video response cannot count toward successful completion or merging.
    @patch('core.video_queue.transform_video', return_value=(b'not video', 'video/mp4'))
    def test_unreadable_service_output_is_retried(self, transform):
        job = self.make_job()
        self.execute(next_task())
        segment = job.segments.get()
        self.assertEqual(segment.status, 'pending')
        self.assertFalse(segment.output_location)
        self.assertIn('unreadable', segment.error)
        reconcile_video(job)
        job.refresh_from_db()
        self.assertEqual(job.status, 'processing')

    # A terminal clip failure cancels queued siblings without erasing successful outputs.
    def test_failure_cancels_pending_but_preserves_completed_siblings(self):
        job = self.make_job(3)
        first, second, third = list(job.segments.all())
        first.status, first.output_location = 'completed', 'saved-output.mp4'
        first.save()
        second.status, second.error = 'failed', 'service exhausted retries'
        second.save()
        reconcile_video(job)
        job.refresh_from_db()
        first.refresh_from_db()
        third.refresh_from_db()
        self.assertEqual(job.status, 'failed')
        self.assertEqual(third.status, 'cancelled')
        self.assertEqual((first.status, first.output_location), ('completed', 'saved-output.mp4'))

    # An interrupted split or merge is requeued in the correct phase after worker exit.
    @patch('core.video_queue.process_alive', return_value=False)
    def test_recovery_requeues_split_and_merge_phases(self, alive):
        split = enqueue_video(video_bytes())
        merge = enqueue_video(video_bytes())
        VideoJob.objects.filter(pk=split.pk).update(status='splitting', owner_pid=999999)
        VideoJob.objects.filter(pk=merge.pk).update(status='merging', owner_pid=999999, merge_attempts=1)
        recover_abandoned()
        split.refresh_from_db()
        merge.refresh_from_db()
        self.assertEqual((split.status, split.owner_pid), ('queued', None))
        self.assertEqual((merge.status, merge.merge_attempts), ('merge_pending', 1))

    # Repeated reconciliation and polling can only claim one merge for a ready video.
    def test_ready_video_has_only_one_merge_claim(self):
        job = self.make_job(2)
        job.segments.update(status='completed', output_location='output.mp4')
        reconcile_video(job)
        reconcile_video(job)
        task = next_task()
        self.assertEqual((task[0], task[1].pk), ('merge', job.pk))
        self.assertIsNone(next_task())

    # Exhausted merge retries fail the video without resubmitting completed service clips.
    @patch('core.video_queue.merge_video', side_effect=TransformError('merge failed', 500))
    def test_merge_attempt_limit(self, merge):
        job = self.make_job()
        job.segments.update(status='completed', output_location='saved.mp4')
        for attempt in range(1, 4):
            VideoJob.objects.filter(pk=job.pk).update(available_at=0)
            self.execute(next_task())
            job.refresh_from_db()
            self.assertEqual(job.merge_attempts, attempt)
            self.assertEqual(job.status, 'merge_pending' if attempt < 3 else 'failed')
        self.assertEqual(job.segments.get().output_location, 'saved.mp4')
        self.assertIsNone(next_task())

    # Splitting failures are visible terminal errors rather than forever-queued jobs.
    @patch('core.video_queue.split_video', side_effect=TransformError('corrupt source', 400))
    def test_split_failure_is_persisted(self, split):
        job = enqueue_video(video_bytes())
        self.execute(next_task())
        job.refresh_from_db()
        self.assertEqual((job.status, job.error), ('failed', 'corrupt source'))
        self.assertFalse(job.segments.exists())


    # Verify actual decoded frame order, not merely the text order of the concat manifest.
    def test_merged_frames_follow_segment_order(self):
        job = self.make_job(3)
        for segment in reversed(list(job.segments.all())):
            buffer = io.BytesIO()
            with av.open(buffer, mode='w', format='mp4') as container:
                stream = container.add_stream('libx264', rate=10)
                stream.width = stream.height = 16
                stream.pix_fmt = 'yuv420p'
                for _ in range(5):
                    frame = av.VideoFrame(16, 16, 'yuv420p')
                    frame.planes[0].update(bytes([segment.segment_order * 60]) * frame.planes[0].buffer_size)
                    for plane in frame.planes[1:]:
                        plane.update(bytes([128]) * plane.buffer_size)
                    for packet in stream.encode(frame):
                        container.mux(packet)
                for packet in stream.encode():
                    container.mux(packet)
            output = Path(segment.input_location).with_name(f'output-{segment.segment_order:06d}.mp4')
            output.write_bytes(buffer.getvalue())
            segment.status, segment.output_location = 'completed', str(output)
            segment.save()
        self.execute(next_task())
        job.refresh_from_db()
        self.assertEqual(job.status, 'completed')
        with av.open(job.final_output) as container:
            frames = list(container.decode(video=0))
        self.assertEqual(len(frames), 15)
        lumas = [bytes(frame.planes[0])[0] for frame in frames]
        self.assertLess(max(lumas[:5]) + 30, min(lumas[5:10]))
        self.assertLess(max(lumas[5:10]) + 30, min(lumas[10:]))
