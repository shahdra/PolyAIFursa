import os
import unittest
import tempfile
from fastapi.testclient import TestClient
import app as app_module
from app import app, init_db
import sqlite3

TEST_IMAGE = os.path.join(os.path.dirname(__file__), "data", "beatles.jpeg")


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
        # first upload an image to create a real session in the database
        with open(TEST_IMAGE, "rb") as f:
            self.client.post("/predict", files={"file": f})
        # check the max score in the db and  use a number higher than that to ensure we get an empty result
        with sqlite3.connect(app_module.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT MAX(score) as max_score FROM detection_objects").fetchone()

            max_score = row["max_score"] if row["max_score"] is not None else 0

        response = self.client.get(f"/predictions/score/{max_score + 0.001}")
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
        # first upload an image to create a real session in the database
        with open(TEST_IMAGE, "rb") as f:
            self.client.post("/predict", files={"file": f})
        with sqlite3.connect(app_module.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT MIN(score) as min_score FROM detection_objects").fetchone()
            min_score = row["min_score"] if row["min_score"] is not None else 0

        response = self.client.get(f"/predictions/score/{min_score}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0)
        for session in data:
            self.assertIn("prediction_uid", session)
            self.assertIn("id", session)
            self.assertIn("label", session)
            self.assertIn("score", session)
            self.assertGreaterEqual(session["score"], min_score)
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