import os
import unittest
from unittest.mock import patch, MagicMock

# Make `app` importable without a live model:
# set dummy env so the MODEL allow-list check passes, and patch the
# model factory so import doesn't create a real Gemini client.
os.environ.setdefault("MODEL", "google_genai:gemini-2.5-flash")
os.environ.setdefault("GOOGLE_API_KEY", "test-key")

_init_patcher = patch("langchain.chat_models.init_chat_model")
_mock_init = _init_patcher.start()
_fake_llm = MagicMock()
_fake_llm.profile = {"tool_calling": True, "max_input_tokens": 1000000}
_fake_llm.bind_tools.return_value = MagicMock()
_mock_init.return_value = _fake_llm

from fastapi.testclient import TestClient
import app as app_module
from app import app, AgentResult


class TestHealth(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


class TestChat(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("app.run_agent")
    def test_chat_returns_structured_response(self, mock_run_agent):
        # Mock the whole agentic loop so no LLM/YOLO calls happen.
        mock_run_agent.return_value = AgentResult(
            text="There are 5 people in the image.",
            iterations=2,
            tools_called=["detect_objects"],
            prediction_uid=None,
            tokens_used={"input": 100, "output": 20, "total": 120},
        )

        response = self.client.post(
            "/chat",
            json={"messages": [{"role": "user", "content": "how many people"}]},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["response"], "There are 5 people in the image.")
        self.assertEqual(body["iterations"], 2)
        self.assertEqual(body["tools_called"], ["detect_objects"])
        self.assertEqual(body["tokens_used"]["total"], 120)

    @patch("app.run_agent")
    def test_chat_missing_messages_returns_422(self, mock_run_agent):
        # Missing the required "messages" field -> FastAPI validation error.
        response = self.client.post("/chat", json={})
        self.assertEqual(response.status_code, 422)