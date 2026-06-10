import os
import unittest
import tempfile
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch
import app as app_module
from app import app, init_db

TEST_IMAGE = os.path.join(os.path.dirname(__file__), "data", "beatles.jpeg")


class TestPredictionTime(unittest.TestCase):
    def setUp(self):
        _, app_module.DB_PATH = tempfile.mkstemp(suffix=".db")
        init_db()
        self.client = TestClient(app)

    def tearDown(self):
        if os.path.exists(app_module.DB_PATH):
            os.remove(app_module.DB_PATH)

    @patch("app.Image")   
    @patch("app.model")   # prevent the real YOLO model from running
    def test_predict_includes_processing_time(self, mock_model, mock_image):
        # We mock the model's behavior to return a predictable result without actually running the YOLO model,
        # which allows us to test the timing logic in the /predict endpoint without relying on the actual model's 
        # performance or the presence of a GPU. This makes the test more reliable and faster.

        # We create first fake detection results that the mocked model will return when called. This simulates what the YOLO model would output after processing an image.
        fake_box_1 = MagicMock() # We create a fake detection box object 
        fake_box_1.cls = [MagicMock(item=lambda: 0)]  # fake label index 0 → "person" 
        fake_box_1.conf = [0.95] # fake confidence score for this detection
        fake_box_1.xyxy = [MagicMock(tolist=lambda: [10, 20, 30, 40])] # fake bounding box coordinates for this detection

        # second fake detection box object with different label and coordinates
        fake_box_2 = MagicMock()
        fake_box_2.cls = [MagicMock(item=lambda: 1)]  # label index 1 → "car"
        fake_box_2.conf = [0.85]
        fake_box_2.xyxy = [MagicMock(tolist=lambda: [50, 60, 70, 80])]

        fake_result = MagicMock() # create a fake result object that the mocked model will return
        fake_result.boxes = [fake_box_1, fake_box_2] # We set the boxes attribute of the fake result to be a list of our two fake detection boxes, simulating the output of the model after processing an image and detecting two objects in it.
        # The .plot() method is used in the /predict endpoint to create an annotated image with bounding boxes.
        fake_result.plot.return_value = MagicMock()     # .return_value is used to specify what the .plot() method should return when called
        
        # mock_model.return_value is what the mocked model will return when it is called in the /predict endpoint.
        mock_model.return_value = [fake_result]            # ← list wrapper! We set the return value of the mocked model to be a list containing our fake result, which simulates the actual output format of the YOLO model when it processes an image. This allows us to test how the /predict endpoint handles the model's output and calculates the processing time without relying on the real model's behavior.
        # mock_model.names is what the mocked model will use to look up class names 
        mock_model.names = {0: "person", 1: "car"}         # ← model.names lookup
        
        with open(TEST_IMAGE, "rb") as f:
            response = self.client.post( 
                "/predict",
                files={"file": ("beatles.jpeg", f, "image/jpeg")} 
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("time_took", data) 
        self.assertIsInstance(data["time_took"], (int, float))
        self.assertGreaterEqual(data["time_took"], 0)
        self.assertIn("prediction_uid", data)
        self.assertIn("detection_count", data)
        self.assertIn("labels", data)
        self.assertEqual(data["detection_count"], 2)# We expect 2 detections based on our mocked model output
        self.assertListEqual(data["labels"], ["person", "car"])# We expect the labels to match the names we defined in the mocked model output

    def test_predict_invalid_file_type(self):
        with open(TEST_IMAGE, "rb") as f:
            response = self.client.post(
                "/predict",
                files={"file": ("testfile.pdf", f, "text/plain")} # We simulate uploading a non-image file (a PDF in this case) by giving it a .pdf extension and a text/plain MIME type
            )
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("detail", data)
        self.assertEqual(data["detail"], "Only image files are supported")
    