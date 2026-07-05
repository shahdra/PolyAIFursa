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
import app as app_module
from app import (
    TOOLS,
    _apply_transform,
    _box_from_args,
    add_noise_image,
    blur_image,
    crop_image,
    detect_objects,
    flip_image,
    resize_image,
    rotate_image,
    run_agent,
)


class TestToolRegistry(unittest.TestCase):
    def test_all_expected_tools_are_registered(self):
        self.assertEqual(
            set(TOOLS.keys()),
            {
                "detect_objects",
                "rotate_image",
                "flip_image",
                "resize_image",
                "crop_image",
                "blur_image",
                "add_noise_image",
            },
        )


class TestDetectObjectsAugmentsWithBoxes(unittest.TestCase):
    def setUp(self):
        image_token = app_module._current_image_b64.set("ZmFrZS1pbWFnZQ==")
        self.addCleanup(app_module._current_image_b64.reset, image_token)

    @patch.object(app_module, "s3_client")
    @patch("app.httpx.Client")
    def test_objects_list_is_added_with_label_score_and_box(self, mock_client_cls, mock_s3):
        predict_response = MagicMock()
        predict_response.json.return_value = {"prediction_uid": "uid-1", "detection_count": 2}
        predict_response.raise_for_status.return_value = None

        detail_response = MagicMock()
        detail_response.json.return_value = {
            "detection_objects": [
                {"label": "dog", "score": 0.9, "box": [0.2, 0.4, 10.5, 10.6]},
                {"label": "dog", "score": 0.8, "box": [50.0, 0.0, 60.0, 10.0]},
            ]
        }
        detail_response.raise_for_status.return_value = None

        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.post.return_value = predict_response
        mock_client.get.return_value = detail_response

        result = json.loads(detect_objects.invoke({}))

        self.assertEqual(result["prediction_uid"], "uid-1")
        # Box coordinates are rounded to whole pixels so the model can copy them
        # straight into blur_image/add_noise_image's integer arguments.
        self.assertEqual(
            result["objects"],
            [
                {"label": "dog", "score": 0.9, "box": [0, 0, 10, 11]},
                {"label": "dog", "score": 0.8, "box": [50, 0, 60, 10]},
            ],
        )

    def test_no_image_returns_error(self):
        token = app_module._current_image_b64.set(None)
        try:
            result = json.loads(detect_objects.invoke({}))
            self.assertIn("error", result)
        finally:
            app_module._current_image_b64.reset(token)


class TestBoxFromArgs(unittest.TestCase):
    def test_all_none_means_whole_image(self):
        self.assertIsNone(_box_from_args(None, None, None, None))

    def test_all_given_returns_tuple(self):
        self.assertEqual(_box_from_args(1, 2, 3, 4), (1, 2, 3, 4))

    def test_partial_args_raise(self):
        with self.assertRaises(ValueError):
            _box_from_args(1, None, 3, 4)


class TestApplyTransform(unittest.TestCase):
    def setUp(self):
        image_token = app_module._current_image_b64.set("ZmFrZS1pbWFnZQ==")
        self.addCleanup(app_module._current_image_b64.reset, image_token)

    def test_whole_image_transform_makes_a_single_mcp_call(self):
        with patch.object(app_module, "_call_img_proc_tool", return_value="rotated-b64") as mock_call:
            result = _apply_transform("rotate", angle=90.0)

        self.assertEqual(result, "rotated-b64")
        mock_call.assert_called_once_with("rotate", image_b64="ZmFrZS1pbWFnZQ==", angle=90.0)

    def test_object_targeted_transform_crops_transforms_then_pastes(self):
        with patch.object(app_module, "_call_img_proc_tool") as mock_call:
            mock_call.side_effect = ["patch-b64", "blurred-patch-b64", "final-b64"]
            result = _apply_transform("blur", (10, 20, 30, 40), radius=5.0)

        self.assertEqual(result, "final-b64")
        self.assertEqual(mock_call.call_count, 3)

        crop_call, transform_call, paste_call = mock_call.call_args_list
        self.assertEqual(crop_call.args, ("crop",))
        self.assertEqual(
            crop_call.kwargs,
            {"image_b64": "ZmFrZS1pbWFnZQ==", "left": 10, "top": 20, "right": 30, "bottom": 40},
        )

        self.assertEqual(transform_call.args, ("blur",))
        self.assertEqual(transform_call.kwargs, {"image_b64": "patch-b64", "radius": 5.0})

        self.assertEqual(paste_call.args, ("paste",))
        self.assertEqual(
            paste_call.kwargs,
            {"base_image_b64": "ZmFrZS1pbWFnZQ==", "patch_b64": "blurred-patch-b64", "left": 10, "top": 20},
        )

    def test_missing_image_raises(self):
        token = app_module._current_image_b64.set(None)
        try:
            with self.assertRaises(ValueError):
                _apply_transform("rotate", angle=90.0)
        finally:
            app_module._current_image_b64.reset(token)


class TestTransformToolsNeverLeakPixelsInTheirTextSummary(unittest.TestCase):
    def test_rotate_image_reports_status_and_includes_result_for_the_caller(self):
        with patch.object(app_module, "_apply_transform", return_value="huge-base64-blob") as mock_apply:
            tool_message = rotate_image.invoke({"angle": 180.0})

        mock_apply.assert_called_once_with("rotate", angle=180.0)
        parsed = json.loads(tool_message)
        self.assertEqual(parsed["status"], "ok")
        self.assertEqual(parsed["image_b64"], "huge-base64-blob")

    def test_blur_image_passes_box_through(self):
        with patch.object(app_module, "_apply_transform", return_value="blurred-b64") as mock_apply:
            tool_message = blur_image.invoke(
                {"radius": 4.0, "left": 1, "top": 2, "right": 3, "bottom": 4}
            )

        mock_apply.assert_called_once_with("blur", (1, 2, 3, 4), radius=4.0)
        parsed = json.loads(tool_message)
        self.assertEqual(parsed["box"], [1, 2, 3, 4])

    def test_add_noise_image_defaults_to_whole_image(self):
        with patch.object(app_module, "_apply_transform", return_value="noisy-b64") as mock_apply:
            tool_message = add_noise_image.invoke({"amount": 0.2})

        mock_apply.assert_called_once_with("add_noise", None, amount=0.2)
        parsed = json.loads(tool_message)
        self.assertIsNone(parsed["box"])

    def test_transform_tool_surfaces_value_errors_as_json(self):
        with patch.object(app_module, "_apply_transform", side_effect=ValueError("no image")):
            tool_message = flip_image.invoke({"direction": "vertical"})

        self.assertEqual(json.loads(tool_message), {"error": "no image"})

    def test_resize_and_crop_are_whole_image_only(self):
        with patch.object(app_module, "_apply_transform", return_value="b64") as mock_apply:
            resize_image.invoke({"width": 100, "height": 50})
            crop_image.invoke({"left": 0, "top": 0, "right": 10, "bottom": 10})

        self.assertEqual(mock_apply.call_args_list[0].args, ("resize",))
        self.assertEqual(mock_apply.call_args_list[1].args, ("crop",))


class TestCallImgProcTool(unittest.TestCase):
    def test_calls_the_mcp_client_and_returns_its_data(self):
        fake_result = MagicMock()
        fake_result.data = "processed-b64"

        fake_client = MagicMock()
        fake_client.__aenter__.return_value = fake_client
        fake_client.__aexit__.return_value = False

        async def fake_call_tool(name, kwargs):
            self.assertEqual(name, "rotate")
            self.assertEqual(kwargs, {"image_b64": "abc", "angle": 90.0})
            return fake_result

        fake_client.call_tool = fake_call_tool

        with patch.object(app_module, "MCPClient", return_value=fake_client):
            result = app_module._call_img_proc_tool("rotate", image_b64="abc", angle=90.0)

        self.assertEqual(result, "processed-b64")


class TestRunAgentRedactsImageDataBeforeItReenterstheLlmContext(unittest.TestCase):
    def test_processed_image_is_extracted_and_stripped_from_the_tool_message(self):
        tool_request = AIMessage(
            content="",
            tool_calls=[{"name": "rotate_image", "args": {"angle": 90.0}, "id": "call_1"}],
        )
        tool_request.usage_metadata = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}

        final = AIMessage(content="Done, I rotated the image.")
        final.usage_metadata = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}

        fake_llm_with_tools = MagicMock()
        fake_llm_with_tools.invoke.side_effect = [tool_request, final]

        fake_tool = MagicMock()
        fake_tool.invoke.return_value = ToolMessage(
            content=json.dumps(
                {"status": "ok", "operation": "rotate", "angle": 90.0, "image_b64": "huge-base64-blob"}
            ),
            tool_call_id="call_1",
        )

        with patch.object(app_module, "llm_with_tools", fake_llm_with_tools), \
             patch.dict(app_module.TOOLS, {"rotate_image": fake_tool}):
            result = run_agent([HumanMessage(content="rotate it")])

        self.assertEqual(result.processed_image_b64, "huge-base64-blob")

        # The message list handed back to the LLM must never carry the raw pixels.
        second_call_messages = fake_llm_with_tools.invoke.call_args_list[1].args[0]
        tool_messages = [m for m in second_call_messages if isinstance(m, ToolMessage)]
        self.assertEqual(len(tool_messages), 1)
        self.assertNotIn("huge-base64-blob", tool_messages[0].content)
        redacted = json.loads(tool_messages[0].content)
        self.assertNotIn("image_b64", redacted)
        self.assertEqual(redacted["operation"], "rotate")


if __name__ == "__main__":
    unittest.main()
