import os
import unittest
import tempfile
from fastapi.testclient import TestClient
import app as app_module
from app import app, init_db


class TestGetPredictionByUid(unittest.TestCase):
    def setUp(self):
        
        # Create a temporary database file for testing and initialize it
        _, app_module.DB_PATH = tempfile.mkstemp(suffix=".db") 
        init_db()
        self.client = TestClient(app)

    def tearDown(self):
        if os.path.exists(app_module.DB_PATH):
            os.remove(app_module.DB_PATH)
    
    def test_get_prediction_by_uid(self):

        # instead of calling the predict endpoint, we can directly insert a prediction session and
        # detection objects into the database for testing the retrieval logic
        uid = "test-uid-123"
        original_image = "original_image_data"
        predicted_image = "predicted_image_data"
        app_module.save_prediction_session(uid, original_image, predicted_image)
        app_module.save_detection_object(uid, "person", 0.95, [10, 20, 30, 40])
        app_module.save_detection_object(uid, "car", 0.85, [50, 60, 70, 80])    


        # Now, retrieve the prediction session by uid
        response = self.client.get(f"/prediction/{uid}")
        self.assertEqual(response.status_code, 200)
        prediction_data = response.json() 
        
        # Validate the structure and content of the response
        self.assertEqual(prediction_data["uid"], uid) 
        self.assertIn("timestamp", prediction_data) 
        self.assertIn("original_image", prediction_data)
        self.assertIn("predicted_image", prediction_data)

        # Check that the detection objects are present and have the expected structure
        # in save_detection_object, we creat detection_objects list and save objects with details on it
        self.assertIn("detection_objects", prediction_data) 
        for obj in prediction_data["detection_objects"]:
            self.assertIn("id", obj)
            self.assertIn("label", obj)
            self.assertIn("score", obj)
            self.assertIn("box", obj)

    def test_get_prediction_by_uid_not_found(self):
        response = self.client.get("/prediction/nonexistent-uid")
        self.assertEqual(response.status_code, 404)
        data = response.json()
        self.assertIn("detail", data)
        self.assertEqual(data["detail"], "Prediction not found")