import os
import unittest
import tempfile
from fastapi.testclient import TestClient
import app as app_module
from app import app, init_db


class TestGetPredictionImage(unittest.TestCase):
    def setUp(self):
        _, app_module.DB_PATH = tempfile.mkstemp(suffix=".db")
        init_db()
        self.client = TestClient(app)

    def tearDown(self):
        if os.path.exists(app_module.DB_PATH):
            os.remove(app_module.DB_PATH)

    def test_get_prediction_image(self):

        # instead of calling the predict endpoint, we can directly insert a prediction session and
        # detection objects into the database for testing the retrieval logic
        uid = "test-uid-123"
        original_image = "original_image_data.jpg"
        # Create a fake predicted image file
        predicted_image_full_path = os.path.join(tempfile.gettempdir(), "predicted_image.jpeg")
        with open(predicted_image_full_path, "wb") as f:
            f.write(b"fake image data")

        app_module.save_prediction_session(uid, original_image, predicted_image_full_path)

        # Now, retrieve the annotated image for this prediction
        response = self.client.get(f"/prediction/{uid}/image")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertEqual(response.content, b"fake image data")
        

    def test_get_prediction_image_not_found(self):
        response = self.client.get("/prediction/nonexistent-uid/image")
        self.assertEqual(response.status_code, 404)
        data = response.json()
        self.assertIn("detail", data)
        self.assertEqual(data["detail"], "Image not found")