import os
import pytest
from fastapi.testclient import TestClient

# Set environment variable for confidence threshold before importing the app to ensure it is read correctly
#when app.py was first imported, it read the env var and set CONFIDENCE_THRESHOLD to 0.7
os.environ.setdefault("CONFIDENCE_THRESHOLD", "0.7")

from app import app, init_db

TEST_IMAGE = os.path.join(os.path.dirname(__file__), "data", "beatles.jpeg")


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch): # Use a temporary database file for testing
    db_file = str(tmp_path / "test_predictions.db") # Builds a path to a temp file unique to this test run. The file doesn't exist yet — just the path string.
    monkeypatch.setattr("app.DB_PATH", db_file) # Override the DB_PATH in the app to point to the temporary database file, ensuring that tests do not interfere with the production database, so now DB_PATH = db_file ,local variable does nothing to app.py
                                                # Every sqlite3.connect(DB_PATH) call inside the app now hits the temp DB instead of production
    init_db()                                    # Creates the tables and indexes in the temp database so it's ready to use                            


@pytest.fixture
def client():
    return TestClient(app)


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

def test_confidence_threshold_value():
    from app import CONFIDENCE_THRESHOLD
    assert CONFIDENCE_THRESHOLD == 0.7