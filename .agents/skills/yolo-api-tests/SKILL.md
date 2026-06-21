---
name: yolo-api-tests
description: Write automated HTTP API tests for the YOLO object-detection service. Use this skill whenever the user asks to add, fix, or expand tests for the YOLO service's API — e.g. "write tests for the /predict endpoint", "add an integration test", "test the YOLO service", or whenever new routes/handlers are added to the YOLO service and need coverage. Trigger even if the user only says "test this endpoint" while working inside the YOLO service.
---

# YOLO API Tests

Write tests that exercise the YOLO service's HTTP layer end-to-end — request in, response out — without ever touching the real database or running real model inference. A good test here proves the API *contract* holds: the right status code, the right response shape, and the right side effects on a throwaway database.

## Core rules

These are non-negotiable. Each exists for a reason, given inline so you understand the intent rather than just following it blindly.

1. **Use `pytest`** (preferred) or `unittest`. Drive the FastAPI app through Starlette's `TestClient` (`from fastapi.testclient import TestClient`) so requests go through the real routing, validation, and serialization stack — not by calling handler functions directly. Calling handlers directly skips exactly the layer these tests exist to verify.

2. **Always use a temporary SQLite database — never the real one.** Tests must be repeatable and must never mutate real data. Point the app at a throwaway DB via a fixture and tear it down afterward. See the database section for the in-memory connection-sharing gotcha.

3. **Assert both the HTTP status code AND the response body structure.** A `200` with the wrong body is still a broken API, and a correct body behind a `500` is meaningless. Check the status first, then assert the shape and key fields of the JSON.

4. **Name test files starting with `test_`** (e.g. `test_predict.py`, `test_health.py`) and test functions starting with `test_`. This is how pytest discovers them — anything else silently won't run.

5. **Mock the YOLO model.** Never load real weights or run inference in a test. It's slow, non-deterministic, and pulls in heavy dependencies. Replace the inference call with a stub that returns fixed, predictable detections so assertions stay stable.

6. **(Optional) Validate the body with `pydantic`.** Parsing the response into a Pydantic model is a stronger, self-documenting assertion than poking at individual keys — if the shape drifts, validation fails loudly.

## Workflow

1. Locate the FastAPI app object and the routes under test (usually `services/yolo/app/main.py` or similar). Note the endpoint path, method, expected request body, and the success/error response shapes.
2. Identify the two seams to control: the **database dependency** and the **model inference call**. These are what you override and mock.
3. Write fixtures in `conftest.py` for the temp DB and the `TestClient`.
4. Write `test_*.py` files, one per endpoint or concern. Cover the happy path plus the obvious failure paths (bad input → `422`, empty body, missing fields).
5. Run `pytest -q` and confirm everything passes and no test touches the real DB.

## Temporary database

Override the app's DB dependency so the application code is unchanged but every request inside a test hits a fresh, disposable database.

Prefer a **file-based temp DB** via pytest's `tmp_path` for simplicity, OR an **in-memory** DB if speed matters. If you use in-memory SQLite, you MUST share a single connection across the session, otherwise each connection gets its own empty database and your inserts vanish between the request and the assertion. Use `StaticPool` and disable same-thread checking:

```python
# conftest.py
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.db import Base, get_db  # adjust imports to the real module paths


@pytest.fixture
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},  # TestClient may use another thread
        poolclass=StaticPool,                        # one shared connection = one shared DB
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture
def client(db_engine):
    TestingSessionLocal = sessionmaker(bind=db_engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()  # don't leak the override into other tests
```

If the service reads a DB path from an environment variable instead of using a dependency, set it with `monkeypatch.setenv(...)` pointing at `tmp_path / "test.db"` before importing/initializing the app.

## Mocking the YOLO model

Find the single function that runs inference (e.g. `run_inference(image)` or a `model.predict(...)` call) and replace it. Prefer patching the *call site* — the name as it's used in the module under test — not the library's original location.

```python
def fake_detections(*args, **kwargs):
    return [
        {"label": "person", "confidence": 0.97, "box": [10, 20, 100, 200]},
        {"label": "dog", "confidence": 0.88, "box": [50, 60, 150, 160]},
    ]

def test_predict_returns_detections(client, monkeypatch):
    # patch where the name is looked up, e.g. app.routes.predict.run_inference
    monkeypatch.setattr("app.routes.predict.run_inference", fake_detections)

    files = {"file": ("test.jpg", b"\xff\xd8\xff\xe0fakejpeg", "image/jpeg")}
    resp = client.post("/predict", files=files)

    assert resp.status_code == 200          # status FIRST
    body = resp.json()
    assert "detections" in body             # then structure
    assert isinstance(body["detections"], list)
    assert body["detections"][0]["label"] == "person"
```

Respect `CONFIDENCE_THRESHOLD` if the service filters by it: set it explicitly with `monkeypatch.setenv("CONFIDENCE_THRESHOLD", "0.5")` so the test isn't at the mercy of a default that may change, and pick fake confidences that land clearly on the intended side of it.

## Asserting status code and body

Always assert the status code first, then the body. Two acceptable styles:

**Manual key checks** — quick, explicit:
```python
assert resp.status_code == 200
body = resp.json()
assert set(body.keys()) >= {"id", "detections"}
assert isinstance(body["detections"], list)
```

**Pydantic validation (optional, stronger)** — define a model mirroring the response contract and let validation do the work:
```python
from pydantic import BaseModel

class Detection(BaseModel):
    label: str
    confidence: float
    box: list[int]

class PredictResponse(BaseModel):
    id: int
    detections: list[Detection]

def test_predict_body_schema(client, monkeypatch):
    monkeypatch.setattr("app.routes.predict.run_inference", fake_detections)
    resp = client.post("/predict", files={"file": ("t.jpg", b"x", "image/jpeg")})
    assert resp.status_code == 200
    PredictResponse.model_validate(resp.json())  # raises if the shape is wrong
```

## Cover failure paths too

A contract isn't only the happy path. Add at least:
- **Missing/invalid input** → assert `422` (FastAPI's validation error) and that the error body has a `detail` field.
- **Empty or malformed file** → assert the status the API documents (e.g. `400`).
- **No detections** (model returns `[]`) → assert `200` with an empty `detections` list, not an error.

```python
def test_predict_missing_file(client):
    resp = client.post("/predict")          # no file at all
    assert resp.status_code == 422
    assert "detail" in resp.json()
```

## Checklist before finishing

- [ ] File named `test_*.py`, functions named `test_*`.
- [ ] App driven through `TestClient`, not by calling handlers directly.
- [ ] DB dependency overridden to a temporary database; real DB never touched.
- [ ] In-memory SQLite (if used) shares one connection via `StaticPool` + `check_same_thread=False`.
- [ ] Model inference mocked — no real weights, no real inference.
- [ ] Every test asserts status code **and** body structure.
- [ ] At least one happy-path and one failure-path test per endpoint.
- [ ] `dependency_overrides` cleared after each test so state doesn't leak.
- [ ] `pytest -q` passes from a clean checkout.