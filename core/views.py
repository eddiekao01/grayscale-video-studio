from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET


@require_GET
def home(request):
    return render(request, 'core/home.html')


@require_GET
def hello(request):
    return JsonResponse({'message': 'Hello, world! From your Python backend.'})


@require_GET
def health(request):
    # Execute a real query so this checks the database connection too.
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
    except DatabaseError:
        return JsonResponse({'status': 'error', 'database': 'unavailable'}, status=503)
    return JsonResponse({'status': 'ok', 'database': 'connected'})
