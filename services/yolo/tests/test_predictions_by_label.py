import os
import unittest
import tempfile
from fastapi.testclient import TestClient
import app as app_module
from app import app, init_db
import sqlite3

TEST_IMAGE = os.path.join(os.path.dirname(__file__), "data", "beatles.jpeg")


class TestPredictionByLabel(unittest.TestCase):

    def setUp(self):
        # Create a temporary database for testing
        # gives us a unique file path for the database that we can use for testing without affecting the production database
        _, app_module.DB_PATH = tempfile.mkstemp(suffix=".db")
        
        init_db()
        # Initialize the TestClient with the FastAPI app to simulate API requests in tests
        self.client = TestClient(app)
                
    
    def test_predictions_by_label_empty(self):
        """
        This test checks that when we search for a label that doesn't exist in the database,
        we get an empty list back, confirming that the API correctly handles cases where no sessions contain"""
        # simulate /predictions/label/ API endpoint to test empty label case
        response = self.client.get("/predictions/label/")
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("detail", data)
        self.assertEqual(data["detail"], "Label cannot be empty")

    def test_label_found_returns_sessions(self):
        """
        This test checks that when we search for a label that exists in the database, 
        we get back a list of prediction sessions that contain that label.
        """
        # first upload an image to create a real session in the database
        with open(TEST_IMAGE, "rb") as f:
            self.client.post("/predict", files={"file": f})

        # now search for a label - we don't know exactly which labels YOLO detected
        # so we get all sessions and pick a real label from the database
        with sqlite3.connect(app_module.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT label FROM detection_objects LIMIT 1").fetchone()
        
        label = row["label"]
        response = self.client.get(f"/predictions/label/{label}")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0)

    def test_response_structure(self):
        """
        Test that the response structure is correct for each session.
        """
        # upload an image to populate the database
        with open(TEST_IMAGE, "rb") as f:
            self.client.post("/predict", files={"file": f})

        with sqlite3.connect(app_module.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT label FROM detection_objects LIMIT 1").fetchone()

        label = row[0]
        response = self.client.get(f"/predictions/label/{label}")
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

    def test_sessions_without_label_are_excluded(self):
        """
        This test checks that if we search for a label that doesn't exist in the database,
        we get an empty list back, confirming that only sessions with the specified label are returned.
        """
        # upload an image to create a session
        with open(TEST_IMAGE, "rb") as f:
            self.client.post("/predict", files={"file": f})

        # search for a label that definitely doesn't exist
        response = self.client.get("/predictions/label/unicorn")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])