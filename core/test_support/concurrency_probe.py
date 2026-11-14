"""Exercise real SQLite connections in an isolated process, never the live database."""
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
from django.conf import settings

settings.DATABASES = {'default': {
    **settings.DATABASES['default'], 'NAME': sys.argv[2], 'OPTIONS': {'timeout': 10},
}}
import django
django.setup()
from django.core.management import call_command
from django.db import close_old_connections, connections
from core.models import TransformLimits
from core.transforms import TransformError, admit_request, processing_slot, transform_video


# Keep each test thread's connection lifecycle separate from its neighbors.
def isolated(action):
    close_old_connections()
    try:
        return action()
    finally:
        connections.close_all()


# Hold six real database-backed slots simultaneously and reject a seventh.
def check_slots():
    barrier = threading.Barrier(7)
    release = threading.Event()

    # Keep one slot occupied until the parent finishes its admission assertions.
    def hold():
        with processing_slot():
            barrier.wait(timeout=10)
            assert release.wait(timeout=10), 'Release signal was never delivered.'

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(isolated, hold) for _ in range(6)]
        try:
            barrier.wait(timeout=10)
            assert len(TransformLimits.objects.get(pk=1).slots) == 6
            try:
                with processing_slot():
                    raise AssertionError('Seventh simultaneous conversion was admitted.')
            except TransformError as error:
                assert error.status == 429
        finally:
            release.set()
        for future in futures:
            future.result(timeout=10)
    assert TransformLimits.objects.get(pk=1).slots == {}, 'Slots were leaked.'
    return {'active': 6, 'seventh_rejected': True, 'released': True}


# Send sixteen simultaneous admissions through independent SQLite connections.
def check_rate():
    start = threading.Event()

    # Wait for a shared start, then record the admission result for one request.
    def attempt():
        assert start.wait(timeout=10)
        try:
            admit_request(limiter_id=3)
            return True
        except TransformError as error:
            assert error.status == 429
            return False

    with patch('core.transforms.time.time', return_value=1000.0):
        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = [executor.submit(isolated, attempt) for _ in range(16)]
            start.set()
            accepted = sum(future.result(timeout=15) for future in futures)
    assert accepted == 12, f'Expected 12 admissions, got {accepted}.'
    assert len(TransformLimits.objects.get(pk=3).arrivals) == 12
    return {'accepted': accepted, 'rejected': 16-accepted}


# Ensure a fast result returns while an earlier slow service call remains unfinished.
def check_independent_results():
    slow_started = threading.Event()
    release_slow = threading.Event()

    # Simulate service latency with events rather than fragile sleep-based ordering.
    def service(data):
        if data == b'slow':
            slow_started.set()
            assert release_slow.wait(timeout=10)
        return data, 'video/mp4'

    # Process a sample through the actual slot and rate coordinator.
    def slow_action():
        return transform_video(b'slow')

    # Process a second sample without waiting for the first service response.
    def fast_action():
        return transform_video(b'fast')

    with patch('core.transforms.call_transform_service', side_effect=service):
        with ThreadPoolExecutor(max_workers=2) as executor:
            slow = executor.submit(isolated, slow_action)
            try:
                assert slow_started.wait(timeout=10)
                fast = executor.submit(isolated, fast_action)
                assert fast.result(timeout=5) == (b'fast', 'video/mp4')
                assert not slow.done(), 'Slow call finished before being released.'
            finally:
                release_slow.set()
            assert slow.result(timeout=10) == (b'slow', 'video/mp4')
    assert TransformLimits.objects.get(pk=1).slots == {}
    return {'fast_returned_first': True, 'released': True}


if __name__ == '__main__':
    call_command('migrate', verbosity=0, interactive=False)
    TransformLimits.objects.create(pk=1)
    checks = {'slots': check_slots, 'rate': check_rate, 'independent': check_independent_results}
    print(json.dumps(checks[sys.argv[1]]()))
    connections.close_all()
