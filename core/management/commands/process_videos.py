import time
from concurrent.futures import ThreadPoolExecutor

from django.core.management.base import BaseCommand
from django.db import OperationalError, close_old_connections

from core.video_queue import execute_task, next_task, recover_abandoned


class Command(BaseCommand):
    help = 'Process the persistent video queue with up to six workers.'

    # Poll durable tasks and dispatch them without blocking on earlier slow segments.
    def handle(self, *args, **options):
        self.stdout.write('Video queue worker running (6 threads).')
        pending = set()
        try:
            with ThreadPoolExecutor(max_workers=6) as executor:
                while True:
                    try:
                        for future in list(pending):
                            if future.done():
                                pending.remove(future)
                                try:
                                    future.result()
                                except Exception as exc:
                                    self.stderr.write(f'Worker task error: {exc}')
                        close_old_connections()
                        recover_abandoned()
                        while len(pending) < 6:
                            task = next_task()
                            if not task:
                                break
                            pending.add(executor.submit(execute_task, *task))
                    except OperationalError:
                        # Another SQLite writer may briefly hold the lock; retry the poll.
                        close_old_connections()
                    time.sleep(0.25)
        except KeyboardInterrupt:
            self.stdout.write('Video worker stopped.')
