from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import FileResponse
from prometheus_fastapi_instrumentator import Instrumentator
from ultralytics import YOLO
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session
import logging
import os
import uuid
import boto3
from pydantic import BaseModel
from dotenv import load_dotenv
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

load_dotenv()
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

app = FastAPI() #This one line creates my web application. Before this line,we have a Python script. After this line, you have a web server that can receive HTTP requests and send responses.
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
S3_BUCKET = os.environ.get("AWS_S3_BUCKET", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
s3_client = boto3.client("s3", region_name=AWS_REGION)

# Download the AI model (tiny model ~6MB)
model = YOLO("yolov8n.pt")


def init_db():
    init_db_impl(DB_PATH)


@app.get("/ready")
def ready():
    if is_shutting_down:
        raise HTTPException(status_code=503, detail="Service is shutting down")
    return {"status": "ready"}

class PredictRequest(BaseModel):
    image_s3_key: str
    
@app.post("/predict")
def predict(request: PredictRequest):
    # Record the start time to calculate how long processing takes.
    start_time = time.time()
    """
    Predict objects in an image stored in S3.
    """
    image_s3_key = request.image_s3_key
    logging.info(f"Received S3 key: {image_s3_key}")

    ext = os.path.splitext(image_s3_key)[1] or ".jpg"
    if ext.lower() not in [".jpg", ".jpeg", ".png"]:
        raise HTTPException(status_code=400, detail="Only image files are supported")

    uid = str(uuid.uuid4())  # unique id for this prediction session (DB)
    original_path = os.path.join(UPLOAD_DIR, uid + ext)
    predicted_path = os.path.join(PREDICTED_DIR, uid + ext)

    # Download the original image from S3 to disk.
    try:
        s3_response = s3_client.get_object(Bucket=S3_BUCKET, Key=image_s3_key)
        with open(original_path, "wb") as f:
            f.write(s3_response["Body"].read())
    except Exception as e:
        logging.error(f"Failed to download {image_s3_key} from S3: {e}")
        raise HTTPException(status_code=404, detail="Image not found")

    # Run the YOLO model on the saved image.
    results = model(original_path, device="cpu", conf=CONFIDENCE_THRESHOLD)
    annotated_frame = results[0].plot()
    annotated_image = Image.fromarray(annotated_frame)
    annotated_image.save(predicted_path)

    # Upload the predicted image back to S3, reusing the original's key prefix
    # so original and predicted sit together: <prefix>/predicted/image<ext>
    key_prefix = image_s3_key.split("/original/")[0]
    predicted_s3_key = f"{key_prefix}/predicted/image{ext}"
    content_type = "image/png" if ext.lower() == ".png" else "image/jpeg"
    with open(predicted_path, "rb") as f:
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=predicted_s3_key,
            Body=f.read(),
            ContentType=content_type,
        )

    # Save the prediction session to the database.
    save_prediction_session(uid, original_path, predicted_path)

    detected_labels = []
    for box in results[0].boxes:
        label_idx = int(box.cls[0].item())
        label = model.names[label_idx]
        score = float(box.conf[0])
        bbox = box.xyxy[0].tolist()
        save_detection_object(uid, label, score, bbox)
        detected_labels.append(label)

    processing_time = round(time.time() - start_time, 2)
    return {
        "prediction_uid": uid,
        "detection_count": len(results[0].boxes),
        "labels": detected_labels,
        "time_took": processing_time,
        "predicted_s3_key": predicted_s3_key,
    }

@app.get("/prediction/{uid}")
def get_prediction_by_uid(uid: str, db: Session = Depends(get_db)):
    # db: Session = Depends(get_db) — dependency injection. This tells FastAPI: "before running this function,
    # call get_db(), take the session it yields, and pass it in as db."
    # The endpoint didn't open a connection, didn't call a factory, didn't write any setup code. 
    # It just said "I need a database session" and FastAPI delivered one.
    """
    Get prediction session by uid with all detected objects
    """
    #This is the entire database query — one line. db.get() is SQLAlchemy's "get by primary key" shortcut.
    # It says: "in the prediction_sessions table, find the row whose primary key (uid) equals the value I passed.
    # " If found, session is a PredictionSession object with all its attributes filled in. If not found, session is None.
    session = db.get(PredictionSession, uid)
    if not session:
        raise HTTPException(status_code=404, detail="Prediction not found")

    return {
    # Each key maps to an attribute of the PredictionSession object. 
    # session.uid reads the uid column value, session.timestamp reads the timestamp, etc.
    # weaccess database columns as Python attributes, not by indexing into a row or parsing SQL results.
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
    
    # it tells app to start listening on port 8080 for incoming requests.
    # Before that line runs, app exists but isn't listening. After it, your service is live.
    uvicorn.run(app, host="0.0.0.0", port=8080)
