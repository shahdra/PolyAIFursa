import os
import unittest
import tempfile
import numpy as np
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch
import app as app_module
from app import app, init_db


class TestPredictionTime(unittest.TestCase):
    def setUp(self):
        _, app_module.DB_PATH = tempfile.mkstemp(suffix=".db")
        init_db()
        self.client = TestClient(app)

    def tearDown(self):
        if os.path.exists(app_module.DB_PATH):
            os.remove(app_module.DB_PATH)

    @patch("app.s3_client")
    @patch("app.model")
    def test_predict_includes_processing_time(self, mock_model, mock_s3):
        # Fake YOLO result (ultralytics-shaped), so no real inference runs.
        fake_box_1 = MagicMock()
        fake_box_1.cls = [MagicMock(item=lambda: 0)]   # label idx 0 -> "person"
        fake_box_1.conf = [0.95]
        fake_box_1.xyxy = [MagicMock(tolist=lambda: [10, 20, 30, 40])]

        fake_box_2 = MagicMock()
        fake_box_2.cls = [MagicMock(item=lambda: 1)]   # label idx 1 -> "car"
        fake_box_2.conf = [0.85]
        fake_box_2.xyxy = [MagicMock(tolist=lambda: [50, 60, 70, 80])]

        fake_result = MagicMock()
        fake_result.boxes = [fake_box_1, fake_box_2]
        # .plot() must return a real ndarray so PIL can save a real file to disk
        # (the handler reads that file back to upload it to S3).
        fake_result.plot.return_value = np.zeros((8, 8, 3), dtype=np.uint8)

        mock_model.return_value = [fake_result]         # model(...) -> [result]
        mock_model.names = {0: "person", 1: "car"}      # model.names[idx] -> label

        # Mock S3: get_object yields fake image bytes; put_object accepts the upload.
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: b"\xff\xd8\xff\xe0fake")
        }
        mock_s3.put_object.return_value = {}

        response = self.client.post(
            "/predict",
            json={"image_s3_key": "test-uid/original/image.jpg"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("time_took", data)
        self.assertIsInstance(data["time_took"], (int, float))
        self.assertGreaterEqual(data["time_took"], 0)
        self.assertIn("prediction_uid", data)
        self.assertIn("detection_count", data)
        self.assertIn("labels", data)
        self.assertEqual(data["detection_count"], 2)
        self.assertListEqual(data["labels"], ["person", "car"])
        # New field introduced by the S3 integration.
        self.assertIn("predicted_s3_key", data)
        # Confirm the S3 round trip was actually exercised.
        mock_s3.get_object.assert_called_once()
        mock_s3.put_object.assert_called_once()

    def test_predict_invalid_file_type(self):
        # The extension is now derived from the S3 key, not an uploaded filename.
        # A key ending in .pdf must be rejected (400) before any S3 download.
        response = self.client.post(
            "/predict",
            json={"image_s3_key": "test-uid/original/testfile.pdf"},
        )
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("detail", data)
        self.assertEqual(data["detail"], "Only image files are supported")