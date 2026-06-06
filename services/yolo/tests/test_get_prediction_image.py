import os
import unittest
import tempfile
from fastapi.testclient import TestClient
import app as app_module
from app import app, init_db

TEST_IMAGE = os.path.join(os.path.dirname(__file__), "data", "beatles.jpeg")


class TestGetPredictionImage(unittest.TestCase):
    def setUp(self):
        _, app_module.DB_PATH = tempfile.mkstemp(suffix=".db")
        init_db()
        self.client = TestClient(app)

    def tearDown(self):
        if os.path.exists(app_module.DB_PATH):
            os.remove(app_module.DB_PATH)

    def test_get_prediction_image(self):
        # First, create a prediction session by uploading an image
        with open(TEST_IMAGE, "rb") as f:
            response = self.client.post(
                "/predict",
                files={"file": ("beatles.jpeg", f, "image/jpeg")}
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        uid = data["prediction_uid"]

        # Now, retrieve the annotated image for this prediction
        response = self.client.get(f"/prediction/{uid}/image")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/jpeg")

    def test_get_prediction_image_not_found(self):
        response = self.client.get("/prediction/nonexistent-uid/image")
        self.assertEqual(response.status_code, 404)
        data = response.json()
        self.assertIn("detail", data)
        self.assertEqual(data["detail"], "Image not found")