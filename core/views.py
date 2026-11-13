from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET


@require_GET
# Render the video upload and preview page.
def home(request):
    return render(request, 'core/home.html')


@require_GET
# Return the starter backend greeting.
def hello(request):
    return JsonResponse({'message': 'Hello, world! From your Python backend.'})


@require_GET
# Report whether the backend can query SQLite.
def health(request):
    # Execute a real query so this checks the database connection too.
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
    except DatabaseError:
        return JsonResponse({'status': 'error', 'database': 'unavailable'}, status=503)
    return JsonResponse({'status': 'ok', 'database': 'connected'})


# Return a readable JSON error when CSRF validation fails.
def csrf_failure(request, reason=''):
    return JsonResponse({'error': 'Your session expired. Reload the page and try again.'}, status=403)


from pathlib import Path

from .media import video_response
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_POST, require_safe
from .models import VideoJob
from .transforms import TransformError, admit_request, validate_source
from .video_queue import enqueue_video


# Serialize durable progress without exposing server file paths.
def job_payload(video):
    completed = video.segments.filter(status='completed').count()
    return {
        'video_id': str(video.id),
        'status': video.status,
        'expected_segments': video.expected_segments,
        'completed_segments': completed,
        'error': video.error,
        'status_url': reverse('video_status', args=[video.id]),
        'source_url': reverse('video_source', args=[video.id]),
        'result_url': reverse('video_result', args=[video.id]) if video.status == 'completed' else None,
    }


@require_POST
# Validate the upload and publish a queued video without waiting for transformations.
def transform(request):
    try:
        admit_request()
        if len(request.FILES) != 1 or len(request.FILES.getlist('clip')) != 1:
            raise TransformError('Upload exactly one video using the clip field.')
        data = validate_source(request.FILES.get('clip'))
        video = enqueue_video(data)
        response = JsonResponse(job_payload(video), status=202)
        response['Location'] = reverse('video_status', args=[video.id])
        return response
    except TransformError as exc:
        response = JsonResponse({'error': exc.message}, status=exc.status)
        if exc.retry_after:
            response['Retry-After'] = str(exc.retry_after)
        return response


@require_GET
# Report segment progress and the final result location for one video.
def video_status(request, video_id):
    video = get_object_or_404(VideoJob, pk=video_id)
    response = JsonResponse(job_payload(video))
    response['Cache-Control'] = 'no-store'
    return response


@require_safe
# Stream the final video only after every segment and the merge have succeeded.
def video_result(request, video_id):
    video = get_object_or_404(VideoJob, pk=video_id)
    if video.status != 'completed':
        return JsonResponse({'error': 'The final video is not ready yet.'}, status=409)
    if not video.final_output or not Path(video.final_output).is_file():
        return JsonResponse({'error': 'The final video file is unavailable.'}, status=410)
    return video_response(request, video.final_output)


@require_safe
# Serve the saved source for resumable previews using the same byte-range support.
def video_source(request, video_id):
    video = get_object_or_404(VideoJob, pk=video_id)
    if not Path(video.input_location).is_file():
        return JsonResponse({'error': 'The source video is unavailable.'}, status=410)
    return video_response(request, video.input_location)
