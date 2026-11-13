"""Single-range MP4 delivery for browser playback and seeking."""
import re
from pathlib import Path

from django.http import FileResponse, HttpResponse, StreamingHttpResponse


# Stream only the requested file region and close it on completion or disconnect.
def read_region(path, start, length):
    with open(path, 'rb') as source:
        source.seek(start)
        while length:
            chunk = source.read(min(length, 64 * 1024))
            if not chunk:
                break
            length -= len(chunk)
            yield chunk


# Serve full files, HEAD metadata, or standards-compliant single byte ranges.
def video_response(request, path):
    size = Path(path).stat().st_size
    range_header = request.headers.get('Range') if request.method == 'GET' and not request.headers.get('If-Range') else None
    headers = {'Accept-Ranges': 'bytes', 'Cache-Control': 'no-store',
               'Content-Disposition': 'inline; filename="grayscale.mp4"'}
    if range_header:
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', range_header.strip())
        try:
            if match is None or not any(match.groups()) or not size:
                raise ValueError
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(int(last), size - 1) if last else size - 1
            else:
                suffix = int(last)
                if suffix <= 0:
                    raise ValueError
                start, end = max(0, size - suffix), size - 1
            if start > end or start >= size:
                raise ValueError
        except ValueError:
            return HttpResponse(status=416, headers={**headers, 'Content-Range': f'bytes */{size}'})
        length = end - start + 1
        return StreamingHttpResponse(read_region(path, start, length), status=206, content_type='video/mp4',
                                     headers={**headers, 'Content-Length': str(length),
                                              'Content-Range': f'bytes {start}-{end}/{size}'})
    if request.method == 'HEAD':
        return HttpResponse(content_type='video/mp4', headers={**headers, 'Content-Length': str(size)})
    return FileResponse(open(path, 'rb'), content_type='video/mp4', headers=headers)
