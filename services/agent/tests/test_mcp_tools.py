import json
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("MODEL", "google_genai:gemini-2.5-flash")
os.environ.setdefault("GOOGLE_API_KEY", "test-key")
os.environ.setdefault("AWS_S3_BUCKET", "test-bucket")

_init_patcher = patch("langchain.chat_models.init_chat_model")
_mock_init = _init_patcher.start()
_fake_llm = MagicMock()
_fake_llm.profile = {"tool_calling": True, "max_input_tokens": 1000000}
_fake_llm.bind_tools.return_value = MagicMock()
_mock_init.return_value = _fake_llm

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel
import app as app_module
from app import _extract_s3_key, _llm_facing_tools, detect_objects, run_agent


# A stand-in for a tool discovered over MCP: it carries the same `s3_key`
# argument the real img-proc tools expose, plus one operation parameter.
class _BlurArgs(BaseModel):
    s3_key: str
    radius: float = 2.0


def _fake_mcp_blur():
    return StructuredTool.from_function(
        func=lambda s3_key, radius=2.0: "unused",
        name="blur",
        description="Apply Gaussian blur to an image.",
        args_schema=_BlurArgs,
    )


class _FakeAsyncMCPTool:
    """Minimal async-only tool, like the ones langchain-mcp-adapters returns."""

    def __init__(self, name, result_key):
        self.name = name
        self._result_key = result_key
        self.received_call = None

    async def ainvoke(self, call):
        self.received_call = call
        return ToolMessage(
            content=[{"type": "text", "text": json.dumps({"s3_key": self._result_key}), "id": "blk"}],
            tool_call_id=call["id"],
        )


class TestLlmFacingToolsHideImageArg(unittest.TestCase):
    def test_s3_key_is_stripped_from_the_schema_the_model_sees(self):
        with patch.object(app_module, "mcp_image_tools", [_fake_mcp_blur()]):
            tools = _llm_facing_tools()

        # detect_objects (a real local tool object) is always first.
        self.assertIs(tools[0], detect_objects)

        blur_schema = next(
            t for t in tools if isinstance(t, dict) and t["function"]["name"] == "blur"
        )
        params = blur_schema["function"]["parameters"]
        self.assertNotIn("s3_key", params["properties"])
        self.assertIn("radius", params["properties"])
        self.assertNotIn("s3_key", params.get("required", []))


class TestExtractS3Key(unittest.TestCase):
    def test_pulls_s3_key_from_content_block_list(self):
        msg = ToolMessage(
            content=[{"type": "text", "text": json.dumps({"s3_key": "img1/working.png"})}],
            tool_call_id="c",
        )
        self.assertEqual(_extract_s3_key(msg), "img1/working.png")

    def test_handles_plain_string_content(self):
        msg = ToolMessage(content=json.dumps({"s3_key": "img1/working.png"}), tool_call_id="c")
        self.assertEqual(_extract_s3_key(msg), "img1/working.png")

    def test_returns_none_when_no_text(self):
        msg = ToolMessage(content=[], tool_call_id="c")
        self.assertIsNone(_extract_s3_key(msg))

    def test_returns_none_on_malformed_json(self):
        msg = ToolMessage(content="not json", tool_call_id="c")
        self.assertIsNone(_extract_s3_key(msg))


class TestRunAgentImageToolBranch(unittest.TestCase):
    def _drive(self, tool_call_args, s3_key="original/image.jpg", tool=None):
        tool = tool or _FakeAsyncMCPTool("blur", "img1/working.png")

        blur_call = AIMessage(
            content="",
            tool_calls=[{"name": "blur", "args": tool_call_args, "id": "c1", "type": "tool_call"}],
        )
        blur_call.usage_metadata = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        final = AIMessage(content="Done.")
        final.usage_metadata = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}

        fake_llm = MagicMock()
        fake_llm.invoke.side_effect = [blur_call, final]

        token = app_module._current_s3_key.set(s3_key)
        try:
            with patch.object(app_module, "llm_with_tools", fake_llm), \
                 patch.object(app_module, "MCP_IMAGE_TOOL_NAMES", {"blur"}), \
                 patch.dict(app_module.TOOLS, {"blur": tool}):
                result = run_agent([HumanMessage(content="blur it")])
        finally:
            app_module._current_s3_key.reset(token)
        return result, tool, fake_llm

    def test_injects_working_key_and_captures_result(self):
        result, tool, _ = self._drive({"radius": 3.0})

        # The model never passed s3_key; run_agent injected it.
        self.assertEqual(tool.received_call["args"]["s3_key"], "original/image.jpg")
        self.assertEqual(tool.received_call["args"]["radius"], 3.0)
        # The processed key is captured for the API response boundary.
        self.assertEqual(result.processed_s3_key, "img1/working.png")

    def test_result_key_is_redacted_before_it_reenters_the_model(self):
        _, _, fake_llm = self._drive({"radius": 3.0})

        # Messages the LLM saw on its second (final) call.
        second_call_messages = fake_llm.invoke.call_args_list[1].args[0]
        tool_messages = [m for m in second_call_messages if isinstance(m, ToolMessage)]
        self.assertEqual(len(tool_messages), 1)
        self.assertNotIn("img1/working.png", str(tool_messages[0].content))
        self.assertEqual(json.loads(tool_messages[0].content), {"status": "ok", "operation": "blur"})

    def test_chaining_feeds_each_edit_the_previous_result(self):
        blur = _FakeAsyncMCPTool("blur", "img1/working.png")
        flip = _FakeAsyncMCPTool("flip", "img1/working.png")

        blur_call = AIMessage(
            content="", tool_calls=[{"name": "blur", "args": {"radius": 2.0}, "id": "c1", "type": "tool_call"}]
        )
        blur_call.usage_metadata = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        flip_call = AIMessage(
            content="", tool_calls=[{"name": "flip", "args": {"direction": "vertical"}, "id": "c2", "type": "tool_call"}]
        )
        flip_call.usage_metadata = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        final = AIMessage(content="Done.")
        final.usage_metadata = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}

        fake_llm = MagicMock()
        fake_llm.invoke.side_effect = [blur_call, flip_call, final]

        token = app_module._current_s3_key.set("img1/original/image.jpg")
        try:
            with patch.object(app_module, "llm_with_tools", fake_llm), \
                 patch.object(app_module, "MCP_IMAGE_TOOL_NAMES", {"blur", "flip"}), \
                 patch.dict(app_module.TOOLS, {"blur": blur, "flip": flip}):
                result = run_agent([HumanMessage(content="blur then flip")])
        finally:
            app_module._current_s3_key.reset(token)

        self.assertEqual(blur.received_call["args"]["s3_key"], "img1/original/image.jpg")
        # flip must operate on blur's output key, not the original upload.
        self.assertEqual(flip.received_call["args"]["s3_key"], "img1/working.png")
        self.assertEqual(result.processed_s3_key, "img1/working.png")

    def test_missing_image_returns_error_without_calling_the_tool(self):
        result, tool, fake_llm = self._drive({"radius": 3.0}, s3_key=None)

        self.assertIsNone(tool.received_call)          # tool never invoked
        self.assertIsNone(result.processed_s3_key)
        second_call_messages = fake_llm.invoke.call_args_list[1].args[0]
        tool_messages = [m for m in second_call_messages if isinstance(m, ToolMessage)]
        self.assertIn("error", json.loads(tool_messages[0].content))


class TestDetectObjectsReturnsBoxes(unittest.TestCase):
    def setUp(self):
        token = app_module._current_s3_key.set("img1/original/image.jpg")
        self.addCleanup(app_module._current_s3_key.reset, token)

    @patch("app.httpx.Client")
    def test_objects_with_rounded_boxes_are_added(self, mock_client_cls):
        predict = MagicMock()
        predict.json.return_value = {"prediction_uid": "uid-1", "detection_count": 1}
        predict.raise_for_status.return_value = None

        detail = MagicMock()
        detail.json.return_value = {
            "detection_objects": [{"label": "dog", "score": 0.9, "box": [0.2, 0.4, 10.6, 20.5]}]
        }
        detail.raise_for_status.return_value = None

        client = mock_client_cls.return_value.__enter__.return_value
        client.post.return_value = predict
        client.get.return_value = detail

        result = json.loads(detect_objects.invoke({}))
        self.assertEqual(result["prediction_uid"], "uid-1")
        self.assertEqual(result["objects"], [{"label": "dog", "score": 0.9, "box": [0, 0, 11, 20]}])

        # detect_objects should call Yolo with the already-uploaded key, not
        # re-upload the image itself.
        client.post.assert_called_once_with(
            f"{app_module.YOLO_SERVICE_URL}/predict",
            json={"image_s3_key": "img1/original/image.jpg"},
        )

    def test_no_image_returns_error(self):
        token = app_module._current_s3_key.set(None)
        try:
            self.assertIn("error", json.loads(detect_objects.invoke({})))
        finally:
            app_module._current_s3_key.reset(token)


class TestChatUploadsImageOncePerTurn(unittest.TestCase):
    """The S3 upload of a user-submitted image must happen in the shared
    /chat path, not only inside detect_objects - otherwise an edit-only turn
    (no detection) never uploads the image at all."""

    def test_upload_happens_even_when_only_an_edit_tool_runs(self):
        from fastapi.testclient import TestClient

        fake_s3 = MagicMock()
        edit_tool = _FakeAsyncMCPTool("blur", "generated-id/working.png")

        blur_call = AIMessage(
            content="", tool_calls=[{"name": "blur", "args": {"radius": 2.0}, "id": "c1", "type": "tool_call"}]
        )
        blur_call.usage_metadata = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        final = AIMessage(content="Blurred it.")
        final.usage_metadata = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}

        fake_llm = MagicMock()
        fake_llm.invoke.side_effect = [blur_call, final]

        with patch.object(app_module, "s3_client", fake_s3), \
             patch.object(app_module, "llm_with_tools", fake_llm), \
             patch.object(app_module, "MCP_IMAGE_TOOL_NAMES", {"blur"}), \
             patch.dict(app_module.TOOLS, {"blur": edit_tool}), \
             patch.object(app_module, "fetch_annotated_image", return_value=None):
            client = TestClient(app_module.app)
            response = client.post("/chat", json={
                "messages": [
                    {"role": "user", "content": "blur it", "image_base64": "ZmFrZS1pbWFnZQ=="},
                ]
            })

        self.assertEqual(response.status_code, 200)
        fake_s3.put_object.assert_called_once()
        _, kwargs = fake_s3.put_object.call_args
        self.assertTrue(kwargs["Key"].endswith("/original/image.jpg"))
        # The edit tool received the freshly-uploaded key, not a stale one.
        uploaded_key = kwargs["Key"]
        self.assertEqual(edit_tool.received_call["args"]["s3_key"], uploaded_key)


if __name__ == "__main__":
    unittest.main()
