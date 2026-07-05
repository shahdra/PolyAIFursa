import asyncio
import base64
import json
import logging
import os
import time
import uuid
from contextvars import ContextVar
from typing import Optional
from langchain_core.rate_limiters import InMemoryRateLimiter
from fastmcp import Client as MCPClient


from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logging.getLogger("langchain").setLevel(logging.DEBUG)
logging.getLogger("langchain_core").setLevel(logging.DEBUG)

import httpx
import boto3

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel

YOLO_SERVICE_URL = os.environ.get("YOLO_SERVICE_URL", "http://localhost:8080")
IMG_PROC_MCP_URL = os.environ.get("IMG_PROC_MCP_URL", "http://localhost:9000/mcp")
MODEL = os.environ.get("MODEL")

# Text-only models
ALLOWED_MODELS = {
    "openai:gpt-5.4-mini",
    "anthropic:claude-haiku-4-5",
    "google_genai:gemini-2.5-flash",
    "bedrock:amazon.nova-lite-v1:0",

}
if MODEL not in ALLOWED_MODELS:
    allowed_list = "\n  ".join(sorted(ALLOWED_MODELS))
    raise SystemExit(
        f"\n[ERROR] MODEL='{MODEL}' is not allowed.\n"
        f"Set MODEL in your .env to one of the supported text-only models:\n  {allowed_list}\n"
    )

SYSTEM_PROMPT = (
    "You are an AI vision assistant. You help users understand and analyze images. "
    "Use the available tools to extract information from images and to edit them. "
    "Call detect_objects first when a request refers to a specific object (e.g. 'the second "
    "dog from the right', 'the detected car'). Its result includes an 'objects' list where each "
    "entry has a label, score and box=[left, top, right, bottom] in pixels — use those "
    "coordinates to work out which object the user means (e.g. sort by the box's left "
    "coordinate to rank objects left-to-right), then pass that exact box as the left/top/right/"
    "bottom arguments to blur_image or add_noise_image to affect only that object. Omit all four "
    "box arguments (or call rotate_image / flip_image / resize_image / crop_image, which always "
    "act on the whole image) to affect the entire image."
)

# Per-request context: the uploaded image and which tools were called. This flows
# AROUND the LLM (the model never sees image data, only text describing it).
S3_BUCKET = os.environ.get("AWS_S3_BUCKET", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
s3_client = boto3.client("s3", region_name=AWS_REGION)
_current_image_b64: ContextVar[Optional[str]] = ContextVar("current_image_b64", default=None)
_tools_called: ContextVar[list] = ContextVar("tools_called", default=[])


@tool
def detect_objects() -> str:
    """Detect and identify objects in the image provided by the user using YOLO object detection."""
    image_b64 = _current_image_b64.get()
    if not image_b64:
        return json.dumps({"error": "No image was provided by the user."})
    image_bytes = base64.b64decode(image_b64)

    # Upload the original image to S3 and pass only the key to Yolo.
    image_id = str(uuid.uuid4())
    s3_key = f"{image_id}/original/image.jpg"
    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=s3_key,
        Body=image_bytes,
        ContentType="image/jpeg",
    )

    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{YOLO_SERVICE_URL}/predict",
            json={"image_s3_key": s3_key},
        )
        response.raise_for_status()
        result = response.json()

        uid = result.get("prediction_uid")
        objects = []
        if uid:
            detail_response = client.get(f"{YOLO_SERVICE_URL}/prediction/{uid}")
            detail_response.raise_for_status()
            for obj in detail_response.json().get("detection_objects", []):
                # Round to whole pixels: the edit tools take integer box coordinates,
                # and the model just copies this "box" back into its next tool call.
                box = [round(c) for c in obj["box"]]
                objects.append({"label": obj["label"], "score": obj["score"], "box": box})

    result["objects"] = objects
    return json.dumps(result)


def _call_img_proc_tool(tool_name: str, **kwargs) -> str:
    """Call a tool on the img-proc-mcp server over MCP and return its string result."""
    async def _call() -> str:
        async with MCPClient(IMG_PROC_MCP_URL) as client:
            result = await client.call_tool(tool_name, kwargs)
            return result.data
    return asyncio.run(_call())


def _box_from_args(
    left: Optional[int], top: Optional[int], right: Optional[int], bottom: Optional[int]
) -> Optional[tuple]:
    """Validate a set of optional box coordinates: all four or none."""
    coords = (left, top, right, bottom)
    if all(c is None for c in coords):
        return None
    if any(c is None for c in coords):
        raise ValueError("Provide all four of left, top, right, bottom, or none of them.")
    return coords


def _apply_transform(tool_name: str, box: Optional[tuple] = None, **params) -> str:
    """
    Run an img-proc-mcp transform on the current image and return the resulting
    base64 PNG.

    If box is None, the transform is applied to the whole image in a single MCP
    call. If box is given (left, top, right, bottom), only that region is sent for
    transformation (crop -> transform -> paste), so the edit is localized to the
    target object and the MCP payload stays as small as the target region rather
    than the full image.
    """
    image_b64 = _current_image_b64.get()
    if not image_b64:
        raise ValueError("No image was provided by the user.")

    if box is None:
        return _call_img_proc_tool(tool_name, image_b64=image_b64, **params)

    left, top, right, bottom = box
    patch_b64 = _call_img_proc_tool(
        "crop", image_b64=image_b64, left=left, top=top, right=right, bottom=bottom
    )
    transformed_patch_b64 = _call_img_proc_tool(tool_name, image_b64=patch_b64, **params)
    return _call_img_proc_tool(
        "paste", base_image_b64=image_b64, patch_b64=transformed_patch_b64, left=left, top=top
    )


@tool
def rotate_image(angle: float = 90.0) -> str:
    """Rotate the entire image by the given angle in degrees (counter-clockwise)."""
    try:
        image_b64 = _apply_transform("rotate", angle=angle)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    return json.dumps({"status": "ok", "operation": "rotate", "angle": angle, "image_b64": image_b64})


@tool
def flip_image(direction: str = "horizontal") -> str:
    """Flip the entire image. direction must be 'horizontal' or 'vertical'."""
    try:
        image_b64 = _apply_transform("flip", direction=direction)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    return json.dumps({"status": "ok", "operation": "flip", "direction": direction, "image_b64": image_b64})


@tool
def resize_image(width: int, height: int) -> str:
    """Resize the entire image to the given width and height in pixels."""
    try:
        image_b64 = _apply_transform("resize", width=width, height=height)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    return json.dumps(
        {"status": "ok", "operation": "resize", "width": width, "height": height, "image_b64": image_b64}
    )


@tool
def crop_image(left: int, top: int, right: int, bottom: int) -> str:
    """Crop the entire image to the given bounding-box coordinates (pixels)."""
    try:
        image_b64 = _apply_transform("crop", left=left, top=top, right=right, bottom=bottom)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    return json.dumps(
        {"status": "ok", "operation": "crop", "box": [left, top, right, bottom], "image_b64": image_b64}
    )


@tool
def blur_image(
    radius: float = 2.0,
    left: Optional[int] = None,
    top: Optional[int] = None,
    right: Optional[int] = None,
    bottom: Optional[int] = None,
) -> str:
    """
    Apply Gaussian blur to the image. To blur a single detected object, pass its
    box coordinates exactly as returned in detect_objects' 'objects' list (left,
    top, right, bottom); omit all four to blur the whole image.
    """
    try:
        box = _box_from_args(left, top, right, bottom)
        image_b64 = _apply_transform("blur", box, radius=radius)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    return json.dumps({"status": "ok", "operation": "blur", "box": box, "image_b64": image_b64})


@tool
def add_noise_image(
    amount: float = 0.1,
    left: Optional[int] = None,
    top: Optional[int] = None,
    right: Optional[int] = None,
    bottom: Optional[int] = None,
) -> str:
    """
    Add salt-and-pepper noise to the image. To affect a single detected object,
    pass its box coordinates exactly as returned in detect_objects' 'objects' list
    (left, top, right, bottom); omit all four to affect the whole image.
    """
    try:
        box = _box_from_args(left, top, right, bottom)
        image_b64 = _apply_transform("add_noise", box, amount=amount)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    return json.dumps({"status": "ok", "operation": "add_noise", "box": box, "image_b64": image_b64})


# Registry: map tool name -> tool function
TOOLS = {
    detect_objects.name: detect_objects,
    rotate_image.name: rotate_image,
    flip_image.name: flip_image,
    resize_image.name: resize_image,
    crop_image.name: crop_image,
    blur_image.name: blur_image,
    add_noise_image.name: add_noise_image,
}

# --- Rate limiter (Exercise: LLM API rate limits) ---
# Throttle outgoing LLM requests so we stay under provider limits and avoid 429s.
rate_limiter = InMemoryRateLimiter(
    requests_per_second=0.5,     # ~1 request every 2 seconds
    check_every_n_seconds=0.1,   # how often to check if a request can proceed
    max_bucket_size=5,           # allow short bursts up to 5 requests
)

model_kwargs = {"temperature": 0, "rate_limiter": rate_limiter}
if MODEL.startswith("bedrock:"):
    model_kwargs["region_name"] = "us-east-1"

llm = init_chat_model(MODEL, **model_kwargs)
# --- Capability check (Exerc
# ise: Model profiles) ---
# Verify the chosen model supports the features the agent needs.
try:
    profile = llm.profile or {}
except Exception:
    profile = {}

if profile:
    if not profile.get("tool_calling", False):
        raise SystemExit(
            f"\n[ERROR] MODEL='{MODEL}' does not support tool calling, "
            f"which this agent requires.\n"
        )
    MAX_INPUT_TOKENS = profile.get("max_input_tokens")
    logging.info(
        f"Model '{MODEL}' profile OK "
        f"(tool_calling=True, max_input_tokens={MAX_INPUT_TOKENS})"
    )
else:
    MAX_INPUT_TOKENS = None
    logging.warning(
        f"No capability profile available for MODEL='{MODEL}'. "
        f"Skipping capability check."
    )

llm_with_tools = llm.bind_tools(list(TOOLS.values()))


class AgentResult(BaseModel):
    text: str
    iterations: int
    tools_called: list[str]
    prediction_uid: Optional[str] = None
    processed_image_b64: Optional[str] = None
    tokens_used: dict = {"input": 0, "output": 0, "total": 0}
    context_limit_exceeded: bool = False


def run_agent(history: list, max_iterations: int = 10) -> AgentResult:
    """
    Simple ReAct loop:
      1. Send messages to the LLM.
      2. If the LLM requests tool calls, execute them and append results.
      3. Repeat until the LLM returns a plain text response.
    Returns an AgentResult with the final text plus loop metadata.
    """
    messages = [SystemMessage(content=SYSTEM_PROMPT)] + history
    tools_called: list[str] = []
    prediction_uid: Optional[str] = None
    processed_image_b64: Optional[str] = None
    tokens = {"input": 0, "output": 0, "total": 0}

    for i in range(1, max_iterations + 1):
        response: AIMessage = llm_with_tools.invoke(messages)
        messages.append(response)
        # Accumulate token usage from this LLM call
        usage = getattr(response, "usage_metadata", None) or {}
        tokens["input"] += usage.get("input_tokens", 0)
        tokens["output"] += usage.get("output_tokens", 0)
        tokens["total"] += usage.get("total_tokens", 0)
        if MAX_INPUT_TOKENS and tokens["input"] >= MAX_INPUT_TOKENS:
            logging.warning(
                f"Approaching input token limit: {tokens['input']}/{MAX_INPUT_TOKENS}"
            )

         # No tool calls, the model produced its final answer
        if not response.tool_calls:
            content = response.content
            if isinstance(content, list):
                content = "".join(
                    block.get("text", "") for block in content
                    if isinstance(block, dict)
                )
            return AgentResult(
                text=content,
                iterations=i,
                tools_called=tools_called,
                prediction_uid=prediction_uid,
                processed_image_b64=processed_image_b64,
                tokens_used=tokens,
            )

        # Execute every tool the model requested
        for tool_call in response.tool_calls:
            tools_called.append(tool_call["name"])
            tool_fn = TOOLS[tool_call["name"]]
            tool_result = tool_fn.invoke(tool_call)          # returns a ToolMessage
            messages.append(tool_result)

            # Capture the YOLO prediction uid and any processed-image result from
            # the tool's JSON result. The processed image is stripped out of the
            # ToolMessage content before it re-enters the LLM's context on the next
            # iteration — the model reasons about images, it never sees their bytes.
            try:
                parsed = json.loads(tool_result.content)
            except (json.JSONDecodeError, AttributeError):
                parsed = None

            if isinstance(parsed, dict):
                if parsed.get("prediction_uid"):
                    prediction_uid = parsed["prediction_uid"]
                if parsed.get("image_b64"):
                    processed_image_b64 = parsed["image_b64"]
                    redacted = {k: v for k, v in parsed.items() if k != "image_b64"}
                    tool_result.content = json.dumps(redacted)

    # Hit the iteration cap without a final answer
    return AgentResult(
        text="I couldn't complete the request within the allowed number of steps.",
        iterations=max_iterations,
        tools_called=tools_called,
        prediction_uid=prediction_uid,
        processed_image_b64=processed_image_b64,
        tokens_used=tokens,
        context_limit_exceeded=True,
    )


def fetch_annotated_image(uid: str) -> Optional[str]:
    """Fetch the annotated (bounding-box) image from YOLO and return it base64-encoded."""
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(f"{YOLO_SERVICE_URL}/prediction/{uid}/image")
            resp.raise_for_status()
        return base64.b64encode(resp.content).decode("utf-8")
    except Exception as e:
        logging.warning(f"Could not fetch annotated image for {uid}: {e}")
        return None


app = FastAPI(title="Vision Agent")

ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS", "http://localhost:3000"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type"],
)


class ChatMessage(BaseModel):
    role: str                           # "user" or "assistant"
    content: str
    image_base64: Optional[str] = None  # only on user messages that carry an image


class ChatRequest(BaseModel):
    messages: list[ChatMessage]         # full conversation thread, oldest first


class ChatResponse(BaseModel):
    response: str
    prediction_id: Optional[str] = None
    annotated_image: Optional[str] = None      # base64 of the bounding-box image
    processed_image: Optional[str] = None      # base64 result of an img-proc-mcp edit
    agent_loop_time_s: Optional[float] = None
    iterations: Optional[int] = None
    tools_called: list[str] = []
    tokens_used: dict = {"input": 0, "output": 0, "total": 0}
    context_limit_exceeded: bool = False


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    lc_messages = []
    latest_image = None

    for msg in request.messages:
        if msg.role == "user":
            if msg.image_base64:
                latest_image = msg.image_base64          # saved for detect_objects tool
                content = msg.content + "\n[An image was uploaded. Use existing tools to analyze it according to user instructions.]"
            else:
                content = msg.content
            lc_messages.append(HumanMessage(content=content))
        else:
            lc_messages.append(AIMessage(content=msg.content))

    image_token = _current_image_b64.set(latest_image)
    try:
        start = time.time()
        result = run_agent(lc_messages)
        elapsed = round(time.time() - start, 2)

        uid = result.prediction_uid
        annotated = fetch_annotated_image(uid) if uid else None

        return ChatResponse(
            response=result.text,
            prediction_id=uid,
            annotated_image=annotated,
            processed_image=result.processed_image_b64,
            agent_loop_time_s=elapsed,
            iterations=result.iterations,
            tools_called=result.tools_called,
            tokens_used=result.tokens_used,
            context_limit_exceeded=result.context_limit_exceeded,
        )
    finally:
        _current_image_b64.reset(image_token)

@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)