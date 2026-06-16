from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import FileResponse, Response
from prometheus_fastapi_instrumentator import Instrumentator
from ultralytics import YOLO
from PIL import Image
import sqlite3
import logging
import os
import uuid
import shutil
import time
import signal
import sys


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

# Initialize SQLite
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        # Create the predictions main table to store the prediction session
        conn.execute("""
            CREATE TABLE IF NOT EXISTS prediction_sessions (
                uid TEXT PRIMARY KEY,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                original_image TEXT,
                predicted_image TEXT
            )
        """)
        
        # Create the objects table to store individual detected objects in a given image
        conn.execute("""
            CREATE TABLE IF NOT EXISTS detection_objects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prediction_uid TEXT,
                label TEXT,
                score REAL,
                box TEXT,
                FOREIGN KEY (prediction_uid) REFERENCES prediction_sessions (uid)
            )
        """)
        
        # Create index for faster queries
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prediction_uid ON detection_objects (prediction_uid)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_label ON detection_objects (label)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_score ON detection_objects (score)")

# saves the prediction session to the database, including the unique identifier (uid), the file paths of the original and predicted images, and a timestamp (automatically set to the current time).
def save_prediction_session(uid, original_image, predicted_image):
    """
    Save prediction session to database
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO prediction_sessions (uid, original_image, predicted_image)
            VALUES (?, ?, ?)
        """, (uid, original_image, predicted_image))
# saves each detected object to the database, including the unique identifier of the prediction session it belongs to (prediction_uid), the label of the detected object, the confidence score, and the bounding box coordinates (stored as a string).
def save_detection_object(prediction_uid, label, score, box):
    """
    Save detection object to database
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO detection_objects (prediction_uid, label, score, box)
            VALUES (?, ?, ?, ?)
        """, (prediction_uid, label, score, str(box)))


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
def get_prediction_by_uid(uid: str):
    """
    Get prediction session by uid with all detected objects
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row # This allows us to access the columns of the result rows by name (e.g., row["uid"] instead of row[0])
        # Get prediction session details for the given uid from the prediction_sessions table. fetchone() returns a single row that matches the query, or None if no matching row is found.
        session = conn.execute("SELECT * FROM prediction_sessions WHERE uid = ?", (uid,)).fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="Prediction not found")
            
        # Get all detection objects for this prediction. fetchall() returns a list of all rows that match the query, which in this case will be all detected objects associated with the given prediction uid.
        objects = conn.execute(
            "SELECT * FROM detection_objects WHERE prediction_uid = ?", 
            (uid,)
        ).fetchall()
        
        return {
            "uid": session["uid"],
            "timestamp": session["timestamp"],
            "original_image": session["original_image"],
            "predicted_image": session["predicted_image"],
            "detection_objects": [
                {
                    "id": obj["id"],
                    "label": obj["label"],
                    "score": obj["score"],
                    "box": obj["box"]
                } for obj in objects
            ]
        }


@app.get("/prediction/{uid}/image")
def get_prediction_image(uid: str):
    """
    Return the annotated (bounding-box) image for a prediction
    """
    with sqlite3.connect(DB_PATH) as conn:
        # row will contain the predicted_image path for the given uid, or None if no matching row is found
        row = conn.execute(
            "SELECT predicted_image FROM prediction_sessions WHERE uid = ?", (uid,)
        ).fetchone()
    if not row or not os.path.exists(row[0]):
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(row[0])

@app.get("/predictions/label/{label}")
def get_predictions_by_label(label: str):
    """
    Get prediction sessions that have at least one detected object with the specified label
    """
    # If label is an empty string
    if not label.strip(): # strip() removes any leading or trailing whitespace from the label string, and if the resulting string is empty, we raise an HTTPException with a 400 status code and a message indicating that the label cannot be empty. This prevents the API from processing requests with invalid or empty labels, which could lead to unexpected behavior or errors in the database queries.
        raise HTTPException(status_code=400, detail="Label cannot be empty") 

    valid_labels = set(model.names.values()) # We create a set of valid labels from the model's names to efficiently check if the provided label exists in the model's known classes. If the label is not in this set, we raise an HTTPException with a 400 status code and a message indicating that the label is invalid. This ensures that the API only processes requests for labels that the model can actually detect, preventing unnecessary database queries and potential confusion for users.   
    if label not in valid_labels:
        raise HTTPException(status_code=400, detail=f"Invalid label. Valid labels are: {', '.join(valid_labels)}")
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        #Start from the prediction_sessions table
        #JOIN detection_objects do ON ps.uid = do.prediction_uid 
        #This is an INNER JOIN — it links each session row to its detected objects by matching the session's uid with the detection object's prediction_uid.
        #WHERE do.label = ?
        #Filters rows to only those where the detected label matches what was passed in the URL. This ensures we only get sessions that have at least one detected object with the specified label.
        #SELECT DISTINCT ps.uid, ps.timestamp
        #We select the unique session uids and their timestamps. DISTINCT ensures that if a session has multiple detected objects with the same label, it will only appear once in the results.
        rows = conn.execute("""
            SELECT DISTINCT ps.uid, ps.timestamp
            FROM prediction_sessions ps
            JOIN detection_objects do ON ps.uid = do.prediction_uid
            WHERE do.label = ?
        """, (label,)).fetchall()

        result = []
        for row in rows:
            # this query gets all the detection objects for a given prediction session (uid)
            # objects will contain all detected objects for the current prediction session, including their label, score, and bounding box
            objects = conn.execute("""
                SELECT id, label, score, box
                FROM detection_objects
                WHERE prediction_uid = ?
            """, (row["uid"],)).fetchall()
            # we convert the sqlite3.Row objects to regular dictionaries for easier JSON serialization in the API response
            # detection_objects will be a list of dictionaries, where each dictionary represents a detected object with its id, label, score, and bounding box
            result_objects = []
            for obj in objects:
                result_objects.append(dict(obj))

            result.append({
                "uid": row["uid"],
                "timestamp": row["timestamp"],
                "detection_objects": result_objects
            })

    return result
    
@app.get("/predictions/score/{min_score}")
def get_predictions_objects_by_min_score(min_score: float):
    """
    Get detected objects with a confidence score above the specified threshold"""
    if min_score < 0.0 or min_score > 1.0:
        raise HTTPException(status_code=400, detail="min_score must be between 0.0 and 1.0")

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT do.id, do.prediction_uid, do.label, do.score, do.box
            FROM detection_objects do
            WHERE do.score >= ?
        """, (min_score,)).fetchall()

        result = []
        for row in rows:
            result.append(dict(row))
        
        
    return result


@app.get("/health")
def health():
    """
    Health check endpoint
    """
    return {"status": "ok from dev"}

if __name__ == "__main__":
    import uvicorn

    init_db()
    
    uvicorn.run(app, host="0.0.0.0", port=8080)
