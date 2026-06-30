---
name: yolo-api-data-layer
description: Guide refactors and feature work for the YOLO service's persistence layer. Use this skill when the user asks to move the API to SQLAlchemy, add prediction-session queries, introduce feedback tables, change schema fields, delete sessions by UID, or make the database backend configurable. Trigger on prompts such as "refactor the api to use sqlalchemy", "add an endpoint GET /predictions/recent", "add a UserFeedback table", "the database layer doesn't follow our architectural design", "delete a prediction session and all its detection objects by uid", "add a column processing_time_ms", or "make the database backend configurable so we can use postgres in production".
---

# YOLO API Data Layer

Use this skill when the request is about how the service stores, retrieves, updates, or deletes prediction data.

## Core goal

Keep the HTTP layer thin and move persistence details into a clear, testable data layer.
The API should handle request validation and serialization, while the database layer should own schema, relationships, queries, and transaction behavior.

## Architectural guardrails

- Prefer SQLAlchemy models and a session dependency over raw `sqlite3` calls.
- Keep endpoint handlers focused on HTTP concerns, not database plumbing.
- Use explicit repository/service helpers for queries such as recent sessions, label lookups, score lookups, and deletions.
- Do not hide the important flow behind a framework wrapper if the lesson is about understanding how data moves through the system.
- Keep the model inference code separate from persistence logic.

## Database design expectations

When changing the data layer, favor this shape:

- `PredictionSession`
  - `uid` (primary key)
  - `timestamp`
  - `original_image`
  - `predicted_image`
  - `processing_time_ms` (if requested)
- `DetectionObject`
  - belongs to one prediction session
  - stores `label`, `score`, and `box`
- `UserFeedback` (if requested)
  - belongs to one prediction session
  - stores rating and optional comment

Use relationships and foreign keys so the schema communicates intent clearly.
If a session is deleted, related detection rows should also be removed (via cascade rules or explicit delete logic).

## SQLAlchemy guidance

- Use a declarative base and a session factory.
- Build the schema once with `Base.metadata.create_all(...)` for development, or use migrations if the project already expects them.
- Use `session.execute(...)` or ORM query patterns consistently instead of mixing styles.
- Use transactions explicitly:
  - `session.add(...)`
  - `session.commit()`
  - `session.rollback()` on error
- Avoid leaking open sessions between requests.

## Endpoint-specific expectations

- `GET /predictions/recent`
  - return the 10 most recent prediction sessions
  - sort by timestamp descending
  - keep the response shape consistent with the rest of the API

- `DELETE /prediction/{uid}` (or equivalent delete route)
  - remove the session and all related detection objects
  - return a clear success or not-found response

- `processing_time_ms`
  - add the column to the session model if the prompt asks for it
  - include it in JSON responses where relevant

- `UserFeedback`
  - add a feedback table for prediction ratings
  - make the relation explicit and easy to query


## Code Examples

These cover the data-layer features described in the evals. They match the service's real **flat layout** (`services/yolo/app.py`, `services/yolo/database.py`) and its real conventions:

- Errors are raised with `HTTPException`, never returned as a `(dict, status_code)` tuple — FastAPI ignores the status in a tuple and serializes it as a normal **200**.
- `box` is stored and returned as a **string** (`str(box)`), not a list.
- `timestamp` is serialized as an ISO-8601 string.
- Read endpoints take a session via `Depends(get_db)`. The existing `save_*` write helpers open their own session from the module-level `SessionLocal`, so **extend those helpers** rather than writing inline `db.add(...)` in a handler.

These are scaffolds, not drop-in code: match the real FK column names and the existing `save_*` signatures in `database.py` before pasting, then run `evals/run_evals.py` to confirm.

### Models — adding `processing_time_ms` and `UserFeedback` (`database.py`)

```python
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Float, Integer, ForeignKey, Text
from sqlalchemy.orm import relationship
# Base, engine, SessionLocal, get_db, init_db already exist in this module.

class PredictionSession(Base):
    __tablename__ = "prediction_sessions"
    uid = Column(String, primary_key=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    original_image = Column(String)
    predicted_image = Column(String)
    processing_time_ms = Column(Float, nullable=True)          # eval 5

    detection_objects = relationship(
        "DetectionObject",
        back_populates="session",
        cascade="all, delete-orphan",                          # ORM-side cascade
    )
    feedback = relationship(
        "UserFeedback", uselist=False, cascade="all, delete-orphan"
    )

class DetectionObject(Base):
    __tablename__ = "detection_objects"
    id = Column(Integer, primary_key=True, autoincrement=True)
    # keep the FK column name that already exists in your file:
    session_uid = Column(
        String,
        ForeignKey("prediction_sessions.uid", ondelete="CASCADE"),  # DB-side cascade
    )
    label = Column(String)
    score = Column(Float)
    box = Column(String)        # stored as str(box) — stays a string in responses
    session = relationship("PredictionSession", back_populates="detection_objects")

class UserFeedback(Base):                                      # eval 3
    __tablename__ = "user_feedback"
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_uid = Column(
        String,
        ForeignKey("prediction_sessions.uid", ondelete="CASCADE"),
        unique=True,
    )
    rating = Column(Integer)
    comment = Column(Text, nullable=True)
```

> **Cascade delete needs both halves.** `cascade="all, delete-orphan"` on the relationship handles it at the ORM level; `ondelete="CASCADE"` on the ForeignKey handles it at the DB level. One without the other leaves orphan rows.

### Shared serializer (matches the real `GET /prediction/{uid}` shape)

```python
def session_to_dict(s):
    return {
        "uid": s.uid,
        "timestamp": s.timestamp.isoformat(),                  # ISO-8601 string
        "original_image": s.original_image,
        "predicted_image": s.predicted_image,
        "processing_time_ms": s.processing_time_ms,            # present once eval 5 is done
        "detection_objects": [
            {"id": o.id, "label": o.label, "score": o.score, "box": o.box}  # box is a str
            for o in s.detection_objects
        ],
    }
```

### `GET /predictions/recent` (eval 2)

```python
from sqlalchemy import desc

@app.get("/predictions/recent")
def get_recent_predictions(db: Session = Depends(get_db)):
    sessions = (
        db.query(PredictionSession)
        .order_by(desc(PredictionSession.timestamp))
        .limit(10)
        .all()
    )
    return [session_to_dict(s) for s in sessions]
```

### `DELETE /prediction/{uid}` (eval 4)

```python
from fastapi import HTTPException

@app.delete("/prediction/{uid}")
def delete_prediction(uid: str, db: Session = Depends(get_db)):
    session = db.query(PredictionSession).filter(PredictionSession.uid == uid).first()
    if not session:
        # raise — do NOT `return {"detail": ...}, 404` (that serializes as a 200)
        raise HTTPException(status_code=404, detail="Prediction not found")
    db.delete(session)        # cascade removes detection_objects (+ feedback)
    db.commit()
    return {"detail": f"Deleted prediction session {uid}"}
```

### `processing_time_ms` — persist through the existing write helper (eval 5)

`/predict` already exists, takes a **file upload**, and returns `{prediction_uid, detection_count, labels, time_took}` — don't change that contract. Thread the timing through the real persistence seam instead: extend `save_prediction_session` to accept and store it. The serializer above already returns it.

```python
# database.py — extend the existing helper; keep its current style/signature order
def save_prediction_session(uid, original_image, predicted_image, processing_time_ms=None):
    with SessionLocal() as db:
        db.add(PredictionSession(
            uid=uid,
            original_image=original_image,
            predicted_image=predicted_image,
            processing_time_ms=processing_time_ms,
        ))
        db.commit()
```

### `UserFeedback` endpoints (eval 3)

```python
@app.post("/prediction/{uid}/feedback")
def submit_feedback(uid: str, rating: int, comment: str | None = None,
                    db: Session = Depends(get_db)):
    session = db.query(PredictionSession).filter(PredictionSession.uid == uid).first()
    if not session:
        raise HTTPException(status_code=404, detail="Prediction not found")
    db.add(UserFeedback(session_uid=uid, rating=rating, comment=comment))
    db.commit()
    return {"detail": "Feedback stored", "rating": rating}

@app.get("/prediction/{uid}/feedback")
def get_feedback(uid: str, db: Session = Depends(get_db)):
    fb = db.query(UserFeedback).filter(UserFeedback.session_uid == uid).first()
    if not fb:
        raise HTTPException(status_code=404, detail="No feedback for this session")
    return {"rating": fb.rating, "comment": fb.comment}
```


## Configurable backend guidance

If the request is to support multiple databases:

- use a single configuration source such as `DATABASE_URL`
- allow the app to switch between SQLite (dev) and PostgreSQL (production)
- keep the default local setup simple and reproducible
- document the expected env vars clearly

For example:

- local/dev: SQLite path or URL
- production: Postgres connection string

## Testing expectations

When adding or changing the data layer:

- write tests that exercise the real API contract
- use a temporary database for each test run
- avoid testing only mock behavior
- verify both the HTTP status code and the response body
- if the route reads/writes DB state, assert the stored data as well as the response

## Workflow

1. Identify the exact schema change or endpoint behavior requested.
2. Separate the work into small steps: models, session setup, repository helpers, endpoint updates, and tests.
3. Update the database layer before or alongside the endpoint logic so the API contract is grounded in the real schema.
4. Verify behavior with focused tests or manual requests.
5. If the request mentions production database switching, confirm the config is env-driven and not hardcoded.
6. Before claiming the task is complete, run the eval runner and confirm the assertions for the matching eval pass:
   `python .agents/skills/yolo-api-data-layer/evals/run_evals.py --pytest`
   Static assertions check the code; `--pytest` runs the suite for behavioral ones. A SKIP that says "no test matching '…' yet" means you still owe a test for that behavior.

## Tooling note

If you run Python locally for verification, use the workspace interpreter under `/home/shahd/PolyAiFursa/.venv` rather than a random system Python.

## Checklist before finishing

- [ ] The schema and relationships match the intended architecture.
- [ ] Endpoint logic is not mixing raw persistence details with response formatting.
- [ ] New queries are explicit and easy to reason about.
- [ ] Deletion and cascading behavior are correct.
- [ ] Configurable database settings are documented and env-driven.
- [ ] Tests cover happy paths and obvious failure cases.
- [ ] `run_evals.py --pytest` was run and the matching eval's assertions pass (no unexpected FAILs).