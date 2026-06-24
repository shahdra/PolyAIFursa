import os
import unittest
import tempfile
from fastapi.testclient import TestClient
import app as app_module
from app import app, init_db


class TestPredictionByMinScore(unittest.TestCase):

    def setUp(self):
        # Create a temporary database for testing
        # gives us a unique file path for the database that we can use for testing without affecting the production database
        _, app_module.DB_PATH = tempfile.mkstemp(suffix=".db")
        init_db()
        # Initialize the TestClient with the FastAPI app to simulate API requests in tests
        self.client = TestClient(app)
    
    def tearDown(self):
        # Remove the temporary database file after tests are done to clean up resources
        if os.path.exists(app_module.DB_PATH):
            os.remove(app_module.DB_PATH)   

    def test_predictions_by_min_score_empty(self):
        """
        This test checks that when we search for a minimum score that doesn't exist in the database,
        we get an empty list back, confirming that the API correctly handles cases where no sessions 
        contain predictions above the specified score.
        """
        # instead of calling the predict endpoint, we can directly insert a prediction session and
        # detection objects into the database for testing the retrieval logic
        uid = "test-uid-123"
        original_image = "original_image_data.jpg"
        predicted_image = "predicted_image_data.jpg"
        app_module.save_prediction_session(uid, original_image, predicted_image)
        app_module.save_detection_object(uid, "person", 0.95, [10, 20, 30, 40])
        app_module.save_detection_object(uid, "car", 0.85, [50, 60, 70, 80])


        response = self.client.get(f"/predictions/score/1.0")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 0)
    
    def test_predictions_by_min_score_valid(self):
        """
        This test checks that when we search for a minimum score that does exist in the database,
        we get a non-empty list back, confirming that the API correctly returns sessions 
        that contain predictions above the specified score.
        """
        # instead of calling the predict endpoint, we can directly insert a prediction session and
        # detection objects into the database for testing the retrieval logic
        uid = "test-uid-123"        
        original_image = "original_image_data.jpg"
        predicted_image = "predicted_image_data.jpg"
        app_module.save_prediction_session(uid, original_image, predicted_image)
        app_module.save_detection_object(uid, "person", 0.95, [10, 20, 30, 40])
        app_module.save_detection_object(uid, "car", 0.85, [50, 60, 70, 80])
        

        response = self.client.get(f"/predictions/score/0.9")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)
        for session in data:
            self.assertIn("prediction_uid", session)
            self.assertIn("id", session)
            self.assertIn("label", session)
            self.assertIn("score", session)
            self.assertGreaterEqual(session["score"], 0.9)
            self.assertIn("box", session)
    
    def test_predictions_by_min_score_invalid(self):
        """
        This test checks that when we search for a minimum score that is invalid (less than 0.0 or greater than 1.0),
        we get a 400 Bad Request response, confirming that the API correctly validates the input and handles errors.
        """
        response = self.client.get("/predictions/score/-0.1")
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("detail", data)
        self.assertEqual(data["detail"], "min_score must be between 0.0 and 1.0")

        response = self.client.get("/predictions/score/1.1")
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("detail", data)
        self.assertEqual(data["detail"], "min_score must be between 0.0 and 1.0")