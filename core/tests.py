from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase
from django.urls import reverse


class StarterTests(TestCase):
    def test_home_renders_template_and_frontend(self):
        response = self.client.get(reverse('home'))
        self.assertTemplateUsed(response, 'core/home.html')
        self.assertContains(response, 'Hello, world')
        self.assertContains(response, '/static/core/app.js')

    def test_hello_api(self):
        response = self.client.get(reverse('hello'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'message': 'Hello, world! From your Python backend.'})

    def test_health_checks_database(self):
        with self.assertNumQueries(1):
            response = self.client.get(reverse('health'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok', 'database': 'connected'})

    def test_health_reports_database_failure(self):
        with patch('core.views.connection.cursor', side_effect=DatabaseError('unavailable')):
            response = self.client.get(reverse('health'))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['database'], 'unavailable')

    def test_endpoints_reject_writes(self):
        for name in ('home', 'hello', 'health'):
            with self.subTest(name=name):
                self.assertEqual(self.client.post(reverse(name)).status_code, 405)
