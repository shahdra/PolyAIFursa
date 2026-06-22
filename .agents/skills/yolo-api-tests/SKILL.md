---
name: yolo-api-tests
description: Write automated HTTP API tests for the YOLO object-detection service (FastAPI + SQLAlchemy). Use this skill whenever the user asks to add, fix, or expand tests for the YOLO service's API — e.g. "write tests for the /predict endpoint", "add an integration test", "test the YOLO service", or whenever new routes/handlers are added to the YOLO service and need coverage. Trigger even if the user only says "test this endpoint" while working inside the YOLO service.
---

# YOLO API Tests

Write tests that exercise the YOLO service's HTTP layer end-to-end — request in, response out — without running real model inference and without touching the real database. A good test here proves the API *contract* holds: the right status code, the right response shape, and the right side effects on a throwaway database.

This service uses **FastAPI + SQLAlchemy**. Match the patterns below to the real code rather than inventing an idiomatic-but-wrong structure.

## The actual code layout (read this first)

The service is a **flat module layout**, not an `app/` package:

- `services/yolo/app.py` — the FastAPI `app`, all route handlers, the loaded `model`, `CONFIDENCE_THRESHOLD`, `DB_PATH`, and `init_db()`.
- `services/yolo/database.py` — SQLAlchemy `Base`, the `PredictionSession` and `DetectionObject` models, the module-level `engine` / `SessionLocal`, `get_db()`, `init_db(db_path)`, and the write helpers `save_prediction_session(...)` / `save_detection_object(...)`.

Correct imports in tests:

```python
from app import app, init_db          # NOT "from app.main import app"
import app as app_module              # to seed data and patch app.model / app.DB_PATH
from database import PredictionSession, DetectionObject, get_db, Base
```

## Core rules

These are non-negotiable. Each exists for a reason, given inline so you understand the intent.

1. **Drive the app through `TestClient`** (`from fastapi.testclient import TestClient`) so requests go through real routing, validation, and serialization — not by calling handler functions directly.
2. **Always use a temporary SQLite database — never `predictions.db`.** Tests must be repeatable and must never mutate real data. See the database section for the *one correct seam* in this codebase.
3. **Assert both the HTTP status code AND the response body structure.** Check the status first, then the shape and key fields of the JSON.
4. **Name test files `test_*.py` and functions `test_*`.** That is how pytest discovers them.
5. **Never run real inference.** Patch `app.model` with a fake. Real weights are slow, non-deterministic, and pull in heavy deps. The mock must mimic the *ultralytics Result* structure (see the mocking section) — this is the part that breaks most often.

## Temporary database — the correct seam for THIS service

The intuitive FastAPI approach (`app.dependency_overrides[get_db] = ...`) is **not sufficient here**, and using it alone will give you tests that silently write to the wrong database. Reason: only the **read** endpoints depend on `get_db`. The **write** helpers `save_prediction_session` and `save_detection_object` open their own session from the module-level `database.SessionLocal` and never go through `get_db`. Overriding `get_db` would redirect reads but leave writes pointing at whatever `init_db()` last configured.

The single seam that redirects **both** reads and writes is `app.DB_PATH` + `init_db()`. Setting `app.DB_PATH` and calling `init_db()` rebuilds `database.engine` and `database.SessionLocal` against the temp file, so every read *and* every write in that test hits the throwaway DB.

```python
import pytest
from fastapi.testclient import TestClient
from app import app, init_db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    # Point the app at a unique throwaway SQLite file, then rebuild the
    # SQLAlchemy engine/SessionLocal against it. This covers BOTH the
    # Depends(get_db) read path and the save_*() write helpers.
    db_file = str(tmp_path / "test.db")
    monkeypatch.setattr("app.DB_PATH", db_file)
    init_db()                       # init_db() -> init_db_impl(app.DB_PATH)
    yield


@pytest.fixture
def client():
    return TestClient(app)
```

**Seed data the way the existing tests do** — call the real write helpers directly instead of going through `/predict`. This lets you test every retrieval endpoint without touching the model at all:

```python
import app as app_module

uid = "test-uid-123"
app_module.save_prediction_session(uid, "orig.jpg", "pred.jpg")
app_module.save_detection_object(uid, "person", 0.95, [10, 20, 30, 40])
app_module.save_detection_object(uid, "car", 0.85, [50, 60, 70, 80])
```

## Mocking the YOLO model (the ultralytics Result shape)

`/predict` calls `model(path, device="cpu", conf=CONFIDENCE_THRESHOLD)` and then reads an **ultralytics-style Result**. A plain list of dicts will not work. The handler does, in order:

- `results = model(...)` then `results[0]` — so the call must return a **list** whose first element is the result.
- `results[0].plot()` — must return a NumPy array (it gets passed to `Image.fromarray(...).save(...)`).
- `for box in results[0].boxes:` then `int(box.cls[0].item())`, `float(box.conf[0])`, `box.xyxy[0].tolist()`.
- `model.names[label_idx]` — must be a real dict mapping class index → label. (It is also used by `/predictions/label/{label}` for validation, so keep it real.)

Build the mock with `return_value` set to a one-element list — `model(...)` must yield `[fake_result]` because the app reads `results[0]`:

```python
import numpy as np
from unittest.mock import MagicMock


def make_box(cls_idx, conf, xyxy):
    box = MagicMock()
    box.cls = [MagicMock(item=lambda i=cls_idx: i)]   # box.cls[0].item() -> idx
    box.conf = [conf]                                  # float(box.conf[0])
    box.xyxy = [MagicMock(tolist=lambda c=xyxy: c)]    # box.xyxy[0].tolist()
    return box


def fake_model():
    result = MagicMock()
    result.plot.return_value = np.zeros((8, 8, 3), dtype=np.uint8)
    result.boxes = [make_box(0, 0.97, [10, 20, 100, 200])]
    m = MagicMock()
    m.return_value = [result]            # model(...) -> [result]  (the results[0] gotcha)
    m.names = {0: "person", 16: "dog"}   # model.names[idx] -> label
    return m


def test_predict_returns_contract(client, monkeypatch):
    monkeypatch.setattr("app.model", fake_model())
    files = {"file": ("test.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")}
    resp = client.post("/predict", files=files)

    assert resp.status_code == 200                # status FIRST
    body = resp.json()
    assert set(body) == {"prediction_uid", "detection_count", "labels", "time_took"}
    assert body["detection_count"] == 1
    assert body["labels"] == ["person"]
```

`CONFIDENCE_THRESHOLD` is read **at import time** in `app.py`. To test a specific value, set the env var *before* importing `app` (`os.environ.setdefault("CONFIDENCE_THRESHOLD", "0.7")` at the top of the test module). To test the default (`0.5`), delete the env var and `importlib.reload(app)`. Because these tests mutate import-time state, keep them in their own module (or run them isolated, e.g. `pytest-forked`) so they don't bleed into other tests.

## The real response contracts (assert against these exactly)

| Endpoint | Success | Body keys / shape | Documented errors |
|---|---|---|---|
| `POST /predict` | 200 | `prediction_uid` (str), `detection_count` (int), `labels` (list[str]), `time_took` (float) | 400 `"Only image files are supported"` for non `.jpg/.jpeg/.png`; 422 if `file` missing |
| `GET /prediction/{uid}` | 200 | `uid`, `timestamp`, `original_image`, `predicted_image`, `detection_objects: [{id, label, score, box}]` | 404 `"Prediction not found"` |
| `GET /prediction/{uid}/image` | 200 | `FileResponse` (the annotated image) | 404 `"Image not found"` if session missing **or** file not on disk |
| `GET /predictions/label/{label}` | 200 | list of `{uid, timestamp, detection_objects: [{id, label, score, box}]}` | 400 `"Label cannot be empty"`; 400 `"Invalid label. ..."` if not in `model.names` |
| `GET /predictions/score/{min_score}` | 200 | list of `{id, prediction_uid, label, score, box}` | 400 `"min_score must be between 0.0 and 1.0"` |
| `GET /health` | 200 | `{"status": "ok"}` | — |
| `GET /ready` | 200 | `{"status": "ready"}` | 503 `"Service is shutting down"` when `is_shutting_down` |
| `GET /print_hello` | 200 | `{"message": "Hello from YOLO service!"}` | — |

Two shape gotchas to assert correctly:

- **`box` comes back as a string**, not a list. The write helper stores `str(box)`, so a saved `[10, 20, 30, 40]` is returned as the string `"[10, 20, 30, 40]"`. Assert `isinstance(obj["box"], str)`.
- **`timestamp` is a serialized datetime** (ISO-8601 string). Assert presence/type, not an exact format.

## Cover failure paths too

A contract isn't only the happy path. Add at least:

- **Bad extension** → `POST /predict` with `name="x.txt"` → assert 400 and `detail == "Only image files are supported"`.
- **Missing file** → `POST /predict` with no file → assert 422 and a `detail` field.
- **Not found** → `GET /prediction/nope` → assert 404 and `detail == "Prediction not found"`.
- **Empty / invalid label** → `GET /predictions/label/ ` → 400 `"Label cannot be empty"`; `GET /predictions/label/not_a_class` → 400 starting with `"Invalid label"`.
- **Score out of range** → `GET /predictions/score/1.1` (and `-0.1`) → 400 `"min_score must be between 0.0 and 1.0"`.

## Workflow

1. Read `services/yolo/app.py` and `services/yolo/database.py` to confirm the endpoint paths, the response keys, and the `save_*` / `init_db` / `model` names (don't assume — they may have changed).
2. Set up the `temp_db` (autouse) and `client` fixtures shown above. There is **no** `conftest.py` in this repo today; either add one or keep fixtures per-file to match the existing style.
3. For retrieval endpoints, **seed via `save_prediction_session` / `save_detection_object`** and assert the read contract.
4. For `/predict`, **patch `app.model`** with the ultralytics-shaped fake and assert the `{prediction_uid, detection_count, labels, time_took}` contract.
5. Add the failure-path tests above.
6. Run `pytest services/yolo/tests/ -q`, confirm all pass, and check the coverage didn't regress.

## Checklist before finishing

- [ ] Imports use the flat layout (`from app import app, init_db`, `from database import ...`).
- [ ] DB redirected via `monkeypatch.setattr("app.DB_PATH", ...)` + `init_db()` — **not** `dependency_overrides[get_db]` alone (writes bypass `get_db`).
- [ ] Real DB (`predictions.db`) never touched.
- [ ] `app.model` patched; `model(...)` returns a **list** (`return_value=[result]`) and `model.names` is a real dict.
- [ ] `/predict` asserts exactly `{prediction_uid, detection_count, labels, time_took}`.
- [ ] `box` asserted as a string; `timestamp` asserted by presence/type only.
- [ ] Every test asserts status code **and** body.
- [ ] At least one happy-path and one failure-path test per endpoint.
- [ ] Import-time `CONFIDENCE_THRESHOLD` tests isolated from the rest.
- [ ] `pytest services/yolo/tests/ -q` passes from a clean checkout.
