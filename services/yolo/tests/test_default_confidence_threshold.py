import importlib

import app


def test_default_confidence_threshold(monkeypatch):
    """
    Test that the default confidence threshold is used when the environment variable is not set.
    """

    # Remove the environment variable if it exists
    monkeypatch.delenv("CONFIDENCE_THRESHOLD", raising=False)
    # Reload the app module to re-evaluate the CONFIDENCE_THRESHOLD
    importlib.reload(app)

    # Assert that the default value is used
    from app import CONFIDENCE_THRESHOLD
    assert CONFIDENCE_THRESHOLD == 0.5
