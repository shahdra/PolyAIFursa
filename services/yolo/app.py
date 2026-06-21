from fastapi import FastAPI, UploadFile, File, HTTPException, Depends
from fastapi.responses import FileResponse
from prometheus_fastapi_instrumentator import Instrumentator
from ultralytics import YOLO
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session
import logging
import os
import uuid
import shutil
import time
import signal
import sys

from database import (
    DetectionObject,
    PredictionSession,
    get_db,
    init_db as init_db_impl,
    save_detection_object,
    save_prediction_session,
)


is_shutting_down = False

def handle_sigterm(signum, frame):
    global is_shutting_down
    is_shutting_down = True
    logging.info("Received SIGTERM. Shutting down gracefully...")
    # Perform cleanup: close DB connections, finish pending work, etc.
    logging.info("Cleanup done. Exiting.")
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_sigterm)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# Disable GPU usage
import torch
torch.cuda.is_available = lambda: False

app = FastAPI()

# Expose /metrics endpoint with default process metrics + FastAPI HTTP metrics
Instrumentator().instrument(app).expose(app)

# Confidence threshold for object detection (0.0 - 1.0).
# Detections below this score are discarded.
# Override with: export CONFIDENCE_THRESHOLD=0.7
_raw_threshold = os.environ.get("CONFIDENCE_THRESHOLD")
if _raw_threshold is not None:
    CONFIDENCE_THRESHOLD = float(_raw_threshold)
    logging.info(f"CONFIDENCE_THRESHOLD set to {CONFIDENCE_THRESHOLD} (from environment)")
else:
    CONFIDENCE_THRESHOLD = 0.5
    logging.info(f"CONFIDENCE_THRESHOLD not set, using default: {CONFIDENCE_THRESHOLD}")

UPLOAD_DIR = "uploads/original" # Directory to save original uploaded images
PREDICTED_DIR = "uploads/predicted" # Directory to save predicted images with bounding boxes drawn on them
DB_PATH = "predictions.db" # Path to the SQLite database file
os.makedirs(UPLOAD_DIR, exist_ok=True) # Create the upload directories if they don't exist   
os.makedirs(PREDICTED_DIR, exist_ok=True)

# Download the AI model (tiny model ~6MB)
model = YOLO("yolov8n.pt")


def init_db():
    init_db_impl(DB_PATH)


@app.get("/ready")
def ready():
    if is_shutting_down:
        raise HTTPException(status_code=503, detail="Service is shutting down")
    return {"status": "ready"}


@app.post("/predict")
def predict(file: UploadFile = File(...)):
    # Record the start time of the prediction process to calculate how long it takes to process the image and return the results.
    start_time = time.time()
    """
    Predict objects in an image
    """
    ext = os.path.splitext(file.filename)[1]
    #for debugging purposes
    logging.info(f"Received file: {file.filename} with extension: {ext}")

    if ext.lower() not in [".jpg", ".jpeg", ".png"]:
        raise HTTPException(status_code=400, detail="Only image files are supported")
    
    uid = str(uuid.uuid4())# Generate a unique identifier for this prediction session
    original_path = os.path.join(UPLOAD_DIR, uid + ext) #create unique file paths for the original and predicted images using the generated uid and the original file extension
    predicted_path = os.path.join(PREDICTED_DIR, uid + ext)

    with open(original_path, "wb") as f:
        shutil.copyfileobj(file.file, f) # Save the uploaded file to disk at the originsl_path we just created.

    # Run the YOLO model on the saved image with the specified confidence threshold
    results = model(original_path, device="cpu", conf=CONFIDENCE_THRESHOLD)
    # results is a list of results for each image (we only have one image, so we take the first result)
    annotated_frame = results[0].plot()  # results[0].plot() returns a NumPy array with the bounding boxes drawn on the original image
    # We convert the annotated frame to a PIL Image and save it to disk
    #Image.fromarray() converts the numpy array to image with bounding boxes 
    annotated_image = Image.fromarray(annotated_frame)
    # We save the annotated image to the predicted path on disk
    annotated_image.save(predicted_path)

    # We save the prediction session to the database, including the uid of the session, original image path, and predicted image path
    save_prediction_session(uid, original_path, predicted_path)
    
    # We loop through the detected boxes in the results and save each detected object to the database with its label,
    # confidence score, and bounding box coordinates. We also collect the labels of the detected objects in a list to include in the API response.
    detected_labels = []
    for box in results[0].boxes:
        label_idx = int(box.cls[0].item()) # box.cls[0] gives us the class index of the detected object, which we convert to an integer and use to look up the corresponding label from the model's names list (model.names).
        label = model.names[label_idx]
        score = float(box.conf[0])
        bbox = box.xyxy[0].tolist() # box.xyxy[0] gives us the bounding box coordinates in the format [x_min, y_min, x_max, y_max], which we convert to a list for easier storage in the database.
        save_detection_object(uid, label, score, bbox)
        detected_labels.append(label)

    processing_time = round(time.time() - start_time, 2)
    return {
        "prediction_uid": uid, # uid of the current predition session
        "detection_count": len(results[0].boxes),
        "labels": detected_labels,
        "time_took": processing_time
    }

@app.get("/prediction/{uid}")
def get_prediction_by_uid(uid: str, db: Session = Depends(get_db)):
    """
    Get prediction session by uid with all detected objects
    """
    session = db.get(PredictionSession, uid)
    if not session:
        raise HTTPException(status_code=404, detail="Prediction not found")

    return {
        "uid": session.uid,
        "timestamp": session.timestamp,
        "original_image": session.original_image,
        "predicted_image": session.predicted_image,
        "detection_objects": [
            {
                "id": obj.id,
                "label": obj.label,
                "score": obj.score,
                "box": obj.box,
            }
            for obj in session.detection_objects
        ],
    }


@app.get("/prediction/{uid}/image")
def get_prediction_image(uid: str, db: Session = Depends(get_db)):
    """
    Return the annotated (bounding-box) image for a prediction
    """
    session = db.get(PredictionSession, uid)
    if not session or not os.path.exists(session.predicted_image):
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(session.predicted_image)

@app.get("/predictions/label/{label}")
def get_predictions_by_label(label: str, db: Session = Depends(get_db)):
    """
    Get prediction sessions that have at least one detected object with the specified label
    """
    if not label.strip():
        raise HTTPException(status_code=400, detail="Label cannot be empty")

    valid_labels = set(model.names.values())
    if label not in valid_labels:
        raise HTTPException(status_code=400, detail=f"Invalid label. Valid labels are: {', '.join(valid_labels)}")

    rows = db.execute(
        select(PredictionSession.uid, PredictionSession.timestamp)
        .join(DetectionObject, PredictionSession.uid == DetectionObject.prediction_uid)
        .where(DetectionObject.label == label)
        .distinct()
    ).all()

    result = []
    for row in rows:
        session = db.get(PredictionSession, row.uid)
        result.append(
            {
                "uid": session.uid,
                "timestamp": session.timestamp,
                "detection_objects": [
                    {
                        "id": obj.id,
                        "label": obj.label,
                        "score": obj.score,
                        "box": obj.box,
                    }
                    for obj in session.detection_objects
                ],
            }
        )

    return result


@app.get("/predictions/score/{min_score}")
def get_predictions_objects_by_min_score(min_score: float, db: Session = Depends(get_db)):
    """
    Get detected objects with a confidence score above the specified threshold
    """
    if min_score < 0.0 or min_score > 1.0:
        raise HTTPException(status_code=400, detail="min_score must be between 0.0 and 1.0")

    rows = db.execute(
        select(DetectionObject)
        .where(DetectionObject.score >= min_score)
    ).scalars().all()

    return [
        {
            "id": row.id,
            "prediction_uid": row.prediction_uid,
            "label": row.label,
            "score": row.score,
            "box": row.box,
        }
        for row in rows
    ]

@app.get("/print_hello")
def print_hello():
    """
    Print hello endpoint
    """
    return {"message": "Hello from YOLO service!"}

@app.get("/health")
def health():
    """
    Health check endpoint
    """
    #added comment for test deployment :)
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn

    init_db()
    
    uvicorn.run(app, host="0.0.0.0", port=8080)
