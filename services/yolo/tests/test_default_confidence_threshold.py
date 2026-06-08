import importlib

import app


def test_default_confidence_threshold(monkeypatch):
    """
    Test that the default confidence threshold is used when the environment variable is not set.
    """

    # delete the environment variable if it exists to simulate it not being set
    # monkeypatch.delenv will not raise an error if the variable does not exist, so it's safe to call even if it was never set
    monkeypatch.delenv("CONFIDENCE_THRESHOLD", raising=False)
    # Reload the app module to re-evaluate the CONFIDENCE_THRESHOLD
    # This is necessary because the CONFIDENCE_THRESHOLD is set at the module level when the app is imported, so we need to reload the module to ensure that it picks up the change in the environment variable.
    importlib.reload(app)

    # Assert that the default value is used
    from app import CONFIDENCE_THRESHOLD
    assert CONFIDENCE_THRESHOLD == 0.5
