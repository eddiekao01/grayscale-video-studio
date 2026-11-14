"""Upload protocol and missing/partial media regression tests."""
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .models import VideoJob
from .tests import video_bytes
from .transforms import TransformError


class APIEdgeTests(TestCase):
    # Isolate all uploaded and downloaded assets from real jobs.
    def setUp(self):
        self.storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        context = override_settings(VIDEO_JOB_ROOT=self.storage.name)
        context.enable()
        self.addCleanup(context.disable)

    # Reject wrong field names and multiple files without publishing partial jobs.
    def test_upload_requires_exactly_one_clip(self):
        for fields in [{'wrong': SimpleUploadedFile('one.mp4', video_bytes())},
                       {'clip': [SimpleUploadedFile('one.mp4', video_bytes()), SimpleUploadedFile('two.mp4', video_bytes())]},
                       {'clip': SimpleUploadedFile('one.mp4', video_bytes()), 'extra': SimpleUploadedFile('two.mp4', video_bytes())}]:
            with self.subTest(fields=list(fields)):
                response = self.client.post(reverse('transform'), fields)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(VideoJob.objects.count(), 0)

    # Real CSRF-protected multipart uploads must work with the page's cookie and header.
    def test_csrf_protected_upload_succeeds(self):
        client = Client(enforce_csrf_checks=True)
        client.get(reverse('home'))
        response = client.post(reverse('transform'), {'clip': SimpleUploadedFile('source.mp4', video_bytes())},
                               HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response['Location'], response.json()['status_url'])

    # Upload admission errors must include retry guidance without creating a job.
    @patch('core.views.admit_request', side_effect=TransformError('busy', 429, 1))
    def test_rate_limit_response_has_retry_after(self, admission):
        response = self.client.post(reverse('transform'), {'clip': SimpleUploadedFile('source.mp4', video_bytes())})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response['Retry-After'], '1')
        self.assertEqual(VideoJob.objects.count(), 0)

    # Unknown jobs must not reveal any source, output, or status information.
    def test_unknown_video_endpoints(self):
        for route in ['video_source', 'video_result', 'video_status']:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(reverse(route, args=[uuid.uuid4()])).status_code, 404)

    # Missing files report gone rather than a server error or a successful empty response.
    def test_missing_source_and_result_files(self):
        job = VideoJob.objects.create(status='completed', input_location=str(Path(self.storage.name)/'missing-source'),
                                      final_output=str(Path(self.storage.name)/'missing-result'))
        for route in ['video_source', 'video_result']:
            with self.subTest(route=route):
                response = self.client.get(reverse(route, args=[job.pk]))
                self.assertEqual(response.status_code, 410)
                self.assertIn('error', response.json())

    # Job progress exposes stable API URLs instead of internal filesystem locations.
    def test_status_does_not_leak_storage_paths(self):
        job = VideoJob.objects.create(status='failed', error='Service unavailable', input_location='/private/source.mp4')
        response = self.client.get(reverse('video_status', args=[job.pk]))
        payload = response.json()
        self.assertEqual(payload['error'], 'Service unavailable')
        self.assertIsNone(payload['result_url'])
        self.assertNotIn('/private/', response.content.decode())
        self.assertEqual(response['Cache-Control'], 'no-store')

    # GET-only status and read-only media endpoints reject POST requests.
    def test_read_endpoints_reject_post(self):
        job = VideoJob.objects.create(input_location='source')
        for route in ['video_source', 'video_result', 'video_status']:
            with self.subTest(route=route):
                self.assertEqual(self.client.post(reverse(route, args=[job.pk])).status_code, 405)


class MediaEdgeTests(TestCase):
    # Create a deterministic file larger than the streamer's individual read chunks.
    def setUp(self):
        self.storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        path = Path(self.storage.name)/'video.mp4'
        self.content = bytes(range(256))*600
        path.write_bytes(self.content)
        self.job = VideoJob.objects.create(status='completed', input_location=str(path), final_output=str(path))
        self.url = reverse('video_result', args=[self.job.pk])

    # A range spanning multiple chunks must match every requested byte and advertised length.
    def test_multichunk_range(self):
        response = self.client.get(self.url, HTTP_RANGE='bytes=500-140000')
        self.assertEqual(response.status_code, 206)
        self.assertEqual(b''.join(response.streaming_content), self.content[500:140001])
        self.assertEqual(int(response['Content-Length']), 139501)
        response.close()

    # Safari-style two-byte probes and the final byte must both return exact partial data.
    def test_first_probe_and_final_byte(self):
        for header, expected in [('bytes=0-1', self.content[:2]), (f'bytes={len(self.content)-1}-', self.content[-1:])]:
            with self.subTest(header=header):
                response = self.client.get(self.url, HTTP_RANGE=header)
                self.assertEqual(response.status_code, 206)
                self.assertEqual(b''.join(response.streaming_content), expected)
                response.close()

    # Large suffix requests are clamped to the file instead of starting at a negative offset.
    def test_suffix_larger_than_file(self):
        response = self.client.get(self.url, HTTP_RANGE='bytes=-99999999')
        self.assertEqual(response.status_code, 206)
        self.assertEqual(b''.join(response.streaming_content), self.content)
        response.close()

    # Unvalidated If-Range conditions must fall back to a full file response.
    def test_if_range_falls_back_to_full_response(self):
        response = self.client.get(self.url, HTTP_RANGE='bytes=0-1', HTTP_IF_RANGE='"old-etag"')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('Content-Range', response)
        self.assertEqual(b''.join(response.streaming_content), self.content)
        response.close()

    # HEAD ignores a range header and returns full-file metadata with no body.
    def test_head_with_range(self):
        response = self.client.head(self.url, HTTP_RANGE='bytes=0-1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(int(response['Content-Length']), len(self.content))
        self.assertEqual(response.content, b'')
