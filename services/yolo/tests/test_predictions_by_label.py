import os
import unittest
import tempfile
from fastapi.testclient import TestClient
import app as app_module
from app import app, init_db
import sqlite3


class TestPredictionByLabel(unittest.TestCase):

    def setUp(self):
        # Create a temporary database for testing
        # gives us a unique file path for the database that we can use for testing without affecting the production database
        _, app_module.DB_PATH = tempfile.mkstemp(suffix=".db")
        
        init_db()
        # Initialize the TestClient with the FastAPI app to simulate API requests in tests
        self.client = TestClient(app)
                
    def tearDown(self):
        if os.path.exists(app_module.DB_PATH):
            os.remove(app_module.DB_PATH)
            
    def test_predictions_by_label_empty(self):
        """
        This test checks that when we search for a label that doesn't exist in the database,
        we get an empty list back, confirming that the API correctly handles cases where no sessions contain"""
        # simulate /predictions/label/ API endpoint to test empty label case
        response = self.client.get("/predictions/label/ ")
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("detail", data)
        self.assertEqual(data["detail"], "Label cannot be empty")

    def test_label_found_returns_sessions(self):
        """
        This test checks that when we search for a label that exists in the database, 
        we get back a list of prediction sessions that contain that label.
        """
        # instead of calling the predict endpoint, we can directly insert a prediction session and
        # detection objects into the database for testing the retrieval logic
        uid = "test-uid-123"
        original_image = "original_image_data.jpg"
        predicted_image = "predicted_image_data.jpg"
        app_module.save_prediction_session(uid, original_image, predicted_image)
        app_module.save_detection_object(uid, "person", 0.95, [10, 20, 30, 40])
        app_module.save_detection_object(uid, "car", 0.85, [50, 60, 70, 80])

        response = self.client.get(f"/predictions/label/person")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list) # We expect a list of sessions in the response
        self.assertGreater(len(data), 0) # We expect at least one session to be returned since we inserted one with the "person" label

    def test_response_structure(self):
        """
        Test that the response structure is correct for each session.
        """
        # instead of calling the predict endpoint, we can directly insert a prediction session and
        # detection objects into the database for testing the retrieval logic
        uid = "test-uid-123"
        original_image = "original_image_data.jpg"
        predicted_image = "predicted_image_data.jpg"
        app_module.save_prediction_session(uid, original_image, predicted_image)
        app_module.save_detection_object(uid, "person", 0.95, [10, 20, 30, 40])
        app_module.save_detection_object(uid, "car", 0.85, [50, 60, 70, 80])

        response = self.client.get(f"/predictions/label/person")
        data = response.json()

        for session in data:
            self.assertIn("uid", session)
            self.assertIn("timestamp", session)
            self.assertIn("detection_objects", session)
            self.assertIsInstance(session["detection_objects"], list)
            for obj in session["detection_objects"]:
                self.assertIn("id", obj)
                self.assertIn("label", obj)
                self.assertIn("score", obj)
                self.assertIn("box", obj)

    def test_matching_session_includes_all_detected_objects(self):
        """
        The endpoint should return the full prediction session for a matching label,
        not just the matching objects.
        """
        uid = "test-uid-456"
        app_module.save_prediction_session(uid, "orig.jpg", "pred.jpg")
        app_module.save_detection_object(uid, "person", 0.99, [1, 2, 3, 4])
        app_module.save_detection_object(uid, "car", 0.88, [5, 6, 7, 8])

        response = self.client.get("/predictions/label/person")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["uid"], uid)
        self.assertEqual(
            [obj["label"] for obj in data[0]["detection_objects"]],
            ["person", "car"]
        )

    def test_invalid_label_returns_error(self):
        """
        This test checks that if we search for an invalid label, we get an error response.
        """
        response = self.client.get("/predictions/label/invalid_label")
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("detail", data)
        self.assertTrue(data["detail"].startswith("Invalid label"))