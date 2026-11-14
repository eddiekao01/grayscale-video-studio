# Grayscale video client

Django templates + plain JavaScript + SQLite, with a persistent video queue.

## Run

```sh
./start.command
```

Open http://127.0.0.1:8001. The launcher loads the ignored `.env`, applies migrations, and starts both Django and a six-thread queue worker. The project uses its own `.venv` and `.python` runtime. Restart the launcher after changing worker code; Django's automatic reload only reloads the web server.

On another environment, install `requirements.txt` into a working Python virtual environment. PyAV validates videos; imageio-ffmpeg supplies the FFmpeg executable used for splitting and merging. The local `.env` provides `BASE_URL` and `TRANSFORM_SERVICE_TOKEN`; credentials never reach the browser.

## Workflow

1. Upload and preview a video. Source videos have no application-level size or duration cap. Uploads spill to temporary disk storage and are copied in chunks; the 3 MB file, 3.2 MB multipart request, and ten-second duration limits apply to individual external service clips only.
2. `POST /api/transform/` validates the upload, saves its source, creates a durable job, and responds with HTTP 202, `video_id`, and `status_url`.
3. The worker encodes ordered independent MP4 clips, targeting 9.9 seconds to leave a small timing margin below the service's ten-second limit. Every clip is checked for readable frames, duration, and the 3 MB file limit before queueing.
4. Segment tasks run independently with globally shared limits of six active service calls and twelve starts per rolling second. Upload admission has its own twelve-per-second limit. Successful outputs are saved immediately, even when earlier clips are still processing.
5. Transient failures retry up to three attempts with backoff. Local rate/concurrency rejections wait without consuming an attempt. A permanently failed clip fails the video; successful siblings are retained.
6. Once all expected segment records are completed, an atomic state transition publishes one durable merge task. Outputs are loaded by segment order, independent of completion order.
7. Matching video formats use FFmpeg stream-copy concatenation; incompatible formats use one normalized re-encoding pass. The original audio is restored during final assembly because the external service removes audio. The final file is decoded for validation, atomically published, then marked completed.
8. The frontend polls status and shows upload progress, splitting, processed clip counts, merging, errors, and the final preview/download next to the original. Result delivery supports HTTP byte ranges for native playback and seeking. Both previews paint decoded video frames onto canvases to avoid blank embedded video surfaces. Accessible Play/Pause, seek, and mute controls operate the underlying decoder, and paused frames remain visible. Reload preview resets decoding without repeating conversion; Open video uses a standalone player. Saved jobs reopen with `/?video=<video_id>`, including the original source preview.

The guaranteed 4 MB service output bound applies to each segment; the assembled video may be larger. The upstream socket timeout is 30 seconds, separate from video duration. Local FFmpeg operations have a five-minute timeout. Splitting re-encodes input to make independent bounded clips, so it adds processing cost and can change visual quality/frame timing slightly.

## Durable bookkeeping

- `VideoJob`: UUID, status, expected segment count, source/final locations, error, worker PID, merge attempts.
- `VideoSegment`: video ID, segment order, status, input/output locations, attempts, retry time, error, worker PID.
- Unique `(video_id, segment_order)` prevents duplicate segment records. Queue emptiness is never used to infer success.
- Completed clips survive worker restarts. Claims owned by exited processes are requeued; a pending merge is recoverable without another service call.
- Files remain under ignored `.tools/video-jobs/`. Successful and failed assets are retained for recovery; automatic retention cleanup is not implemented.

The queue and limiter assume all worker processes share SQLite and the same machine/filesystem. This is a local development design. A multi-host deployment would need shared storage and a suitable shared queue/limiter. No database transaction is held across a service call or FFmpeg operation.

## Endpoints

- `GET /`: upload interface.
- `POST /api/transform/`: multipart `clip` upload with Django CSRF protection; returns a queued job or JSON error.
- `GET /api/videos/<video_id>/`: status, completed/expected segment counts, error, and final URL when ready.
- `GET /api/videos/<video_id>/source/`: original source for restored previews, with byte-range support.
- `GET /api/videos/<video_id>/result/`: final MP4; returns 409 while unfinished.
- Original `/api/hello/`, `/api/health/`, and `/admin/` remain available.

## Checks

```sh
.venv/bin/python manage.py check
.venv/bin/python manage.py test
```

To run components separately after applying migrations, use `manage.py runserver 127.0.0.1:8001` and `manage.py process_videos` in separate terminals with the same environment. Both must be running for queued jobs to progress.

## Test coverage

The automated suite generates tiny real videos locally and mocks external HTTP, so it requires no token or service availability. It covers source/clip boundaries, spoofed and audio-only uploads, CSRF, multipart/authentication contracts, upstream failures and output bounds, retry backoff and exhaustion, dead-worker recovery, duplicate merge prevention, decoded frame ordering, audio restoration, format normalization, and exact byte-range delivery.

Concurrency probes use separate processes and private temporary SQLite databases to verify six simultaneous slots, twelve admissions under sixteen simultaneous requests, and fast completion without waiting for a slow request. They never access the live job database. Browser painting remains a manual browser check; backend tests cannot establish that an embedded player renders correctly.
