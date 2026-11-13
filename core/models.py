from django.db import models


class TransformLimits(models.Model):
    """One shared row; videos themselves are never stored in the database."""
    arrivals = models.JSONField(default=list)
    slots = models.JSONField(default=dict)
    touched = models.FloatField(default=0)


import uuid


class VideoJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    status = models.CharField(max_length=20, default='queued', db_index=True)
    expected_segments = models.PositiveIntegerField(default=0)
    input_location = models.TextField()
    final_output = models.TextField(blank=True)
    error = models.TextField(blank=True)
    owner_pid = models.PositiveIntegerField(null=True)
    merge_attempts = models.PositiveIntegerField(default=0)
    available_at = models.FloatField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)


class VideoSegment(models.Model):
    video = models.ForeignKey(VideoJob, related_name='segments', on_delete=models.CASCADE)
    segment_order = models.PositiveIntegerField()
    status = models.CharField(max_length=20, default='pending', db_index=True)
    input_location = models.TextField()
    output_location = models.TextField(blank=True)
    attempts = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    owner_pid = models.PositiveIntegerField(null=True)
    available_at = models.FloatField(default=0)

    class Meta:
        ordering = ['segment_order']
        constraints = [models.UniqueConstraint(fields=['video', 'segment_order'], name='unique_video_segment')]
