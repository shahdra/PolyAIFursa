import os
import unittest
from unittest.mock import patch, MagicMock

# Same import-safe setup as test_api: dummy env + patched model factory
# so importing `app` doesn't build a real Gemini client.
os.environ.setdefault("MODEL", "google_genai:gemini-2.5-flash")
os.environ.setdefault("GOOGLE_API_KEY", "test-key")

_init_patcher = patch("langchain.chat_models.init_chat_model")
_mock_init = _init_patcher.start()
_fake_llm = MagicMock()
_fake_llm.profile = {"tool_calling": True, "max_input_tokens": 1000000}
_fake_llm.bind_tools.return_value = MagicMock()
_mock_init.return_value = _fake_llm

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
import app as app_module
from app import run_agent


class TestRunAgent(unittest.TestCase):
    def test_no_tool_call_returns_final_answer(self):
        """If the LLM answers directly (no tool calls), run_agent returns that text."""
        final = AIMessage(content="Hello! How can I help?")
        final.usage_metadata = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

        fake_llm_with_tools = MagicMock()
        fake_llm_with_tools.invoke.return_value = final

        with patch.object(app_module, "llm_with_tools", fake_llm_with_tools):
            result = run_agent([HumanMessage(content="hi")])

        self.assertEqual(result.text, "Hello! How can I help?")
        self.assertEqual(result.iterations, 1)
        self.assertEqual(result.tools_called, [])
        self.assertEqual(result.tokens_used["total"], 15)

    def test_tool_call_then_final_answer(self):
        """LLM requests a tool, gets the result, then gives a final answer."""
        # First response: a tool call. Second: the final text.
        tool_request = AIMessage(
            content="",
            tool_calls=[{"name": "detect_objects", "args": {}, "id": "call_1"}],
        )
        tool_request.usage_metadata = {"input_tokens": 20, "output_tokens": 8, "total_tokens": 28}

        final = AIMessage(content="There are 5 people in the image.")
        final.usage_metadata = {"input_tokens": 30, "output_tokens": 12, "total_tokens": 42}

        fake_llm_with_tools = MagicMock()
        fake_llm_with_tools.invoke.side_effect = [tool_request, final]

        # Fake the tool: when invoked, return a ToolMessage with YOLO-like JSON.
        fake_tool = MagicMock()
        fake_tool.invoke.return_value = ToolMessage(
            content='{"prediction_uid": "abc-123", "detection_count": 5}',
            tool_call_id="call_1",
        )

        with patch.object(app_module, "llm_with_tools", fake_llm_with_tools), \
             patch.dict(app_module.TOOLS, {"detect_objects": fake_tool}):
            result = run_agent([HumanMessage(content="how many people")])

        self.assertEqual(result.text, "There are 5 people in the image.")
        self.assertEqual(result.iterations, 2)
        self.assertEqual(result.tools_called, ["detect_objects"])
        self.assertEqual(result.prediction_uid, "abc-123")
        # tokens accumulate across both calls: 28 + 42
        self.assertEqual(result.tokens_used["total"], 70)

    def test_max_iterations_guard(self):
        """If the LLM keeps requesting tools, run_agent stops at max_iterations."""
        forever_tool_call = AIMessage(
            content="",
            tool_calls=[{"name": "detect_objects", "args": {}, "id": "call_x"}],
        )
        forever_tool_call.usage_metadata = {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}

        fake_llm_with_tools = MagicMock()
        fake_llm_with_tools.invoke.return_value = forever_tool_call  # always a tool call

        fake_tool = MagicMock()
        fake_tool.invoke.return_value = ToolMessage(content="{}", tool_call_id="call_x")

        with patch.object(app_module, "llm_with_tools", fake_llm_with_tools), \
             patch.dict(app_module.TOOLS, {"detect_objects": fake_tool}):
            result = run_agent([HumanMessage(content="loop forever")], max_iterations=3)

        self.assertTrue(result.context_limit_exceeded)
        self.assertEqual(result.iterations, 3)