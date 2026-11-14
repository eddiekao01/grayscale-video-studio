"""Real concurrent SQLite integration checks isolated from both test and live databases."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase


class ConcurrentCoordinatorTests(SimpleTestCase):
    # Run each probe against a fresh private database in a separate Python process.
    def probe(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(__file__).parent/'test_support'/'concurrency_probe.py'
            result = subprocess.run([sys.executable, str(script), mode, str(Path(directory)/'queue.sqlite3')],
                                    capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)

    # Verify actual simultaneous reservations enforce six active slots and release every slot.
    def test_six_simultaneous_slots(self):
        self.assertEqual(self.probe('slots'), {'active': 6, 'seventh_rejected': True, 'released': True})

    # Verify simultaneous arrivals cannot race past the global twelve-per-second limit.
    def test_simultaneous_rate_admission(self):
        self.assertEqual(self.probe('rate'), {'accepted': 12, 'rejected': 4})

    # Verify no FIFO completion barrier prevents a fast transformation from returning first.
    def test_fast_conversion_finishes_before_slow(self):
        self.assertEqual(self.probe('independent'), {'fast_returned_first': True, 'released': True})
