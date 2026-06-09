import unittest
from fastapi.testclient import TestClient
import app as app_module
from app import app, init_db


class TestReady(unittest.TestCase):
    def setUp(self):
        # Initialize the TestClient with the FastAPI app to simulate API requests in tests
        self.client = TestClient(app)
    
    def test_ready(self):
        """
        This test checks that the /ready endpoint returns a 200 status code and a JSON response with {"status": "ok"},
        confirming that the API is ready to handle requests.
        """
        response = self.client.get("/ready")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ready"})
    
    def test_ready_shutting_down(self):
        """
        This test checks that the /ready endpoint returns a 503 status code and a JSON response with {"status": "shutting down"},
        confirming that the API correctly indicates when it is in the process of shutting down and not ready to handle requests.
        """
        # Simulate shutdown by setting the global variable to True
        app_module.is_shutting_down = True
        response = self.client.get("/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Service is shutting down"})