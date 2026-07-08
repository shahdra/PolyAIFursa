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
import ast

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
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import BaseModel

YOLO_SERVICE_URL = os.environ.get("YOLO_SERVICE_URL", "http://localhost:8080")
IMG_PROC_MCP_URL = os.environ.get("IMG_PROC_MCP_URL", "http://localhost:9000/mcp")
MODEL = os.environ.get("MODEL")

# Text-only models
ALLOWED_MODELS = {
    "openai:gpt-5.4-mini",
    "bedrock:anthropic:claude-haiku-4-5",
    "google_genai:gemini-2.5-flash",
    "bedrock:amazon.nova-lite-v1:0",
    "bedrock:anthropic.claude-opus-4-7",
    "bedrock:us.meta.llama3-1-8b-instruct-v1:0",
}
if MODEL not in ALLOWED_MODELS:
    allowed_list = "\n  ".join(sorted(ALLOWED_MODELS))
    raise SystemExit(
        f"\n[ERROR] MODEL='{MODEL}' is not allowed.\n"
        f"Set MODEL in your .env to one of the supported text-only models:\n  {allowed_list}\n"
    )

SYSTEM_PROMPT = (
    "You are an AI vision assistant. You help users understand, analyze, and edit images.\n\n"
    "Tools available to you:\n"
    "- detect_objects: runs YOLO object detection on the current image and returns the "
    "detected objects, each with a label, a confidence score, and a bounding box "
    "[left, top, right, bottom] in pixels.\n"
    "- Image-editing tools (rotate, flip, blur, resize, crop, add_noise): each edits the "
    "current image and produces a new image that any following edit builds on.\n\n"
    "Guidelines:\n"
    "- You never handle image data yourself. Do NOT put image contents in a tool call and "
    "do not ask the user for them - the image is supplied to the tools automatically. Only "
    "choose the operation and its parameters (angle, radius, width/height, box, ...).\n"
    "- When a request targets a specific object (e.g. 'the second dog from the right' or "
    "'the detected car'), call detect_objects first, then pick the object using this EXACT "
    "procedure:\n"
    "  1. Sort the detected objects by box[0] (the left edge), SMALLEST first.\n"
    "  2. This sorted list now runs left-to-right across the image.\n"
    "  3. 'first' / 'leftmost' = position 1. 'second from the left' = position 2. "
    "'rightmost' = the LAST position. 'second from the right' = second-to-last. "
    "'middle' = the center position (e.g. position 2 of 3).\n"
    "  4. Count from the correct end: 'from the left' starts at the SMALLEST"
    "box[0] (left edge); 'from the right' starts at the LARGEST box[0]"
    "(still the left edge - you rank every object by box[0] and just"
    "count from the opposite end)..\n"
    "  Do NOT assume the detected objects are already in left-to-right order - they are "
    "not, so you must sort by box[0] yourself first. Then use that object's box for the edit.\n"
    "- When the request does NOT name a specific object, apply the edit to the WHOLE "
    "image. Do not run detection and do not ask which object - just perform the operation "
    "on the entire image. Only target a specific object when the user clearly names one.\n"
    "- If a word looks like a misspelled operation (e.g. 'herozantily' for 'horizontally'), "
    "interpret it as the operation, not as an object name.\n"
    "- rotate, flip, blur and add_noise act on the whole current image by default, or on "
    "just one object if you pass its bounding box as `box` [left, top, right, bottom]. "
    "resize changes the whole image; crop extracts the given region.\n"
    "- Editing tools can be chained: each one operates on the result of the previous edit.\n"
    "- To edit an object you must call TWO tools in order: first detect_objects to get "
    "the box, then the edit tool (blur/flip/rotate/etc.) with that box. Calling only "
    "detect_objects does NOT edit anything. You have not blurred/flipped/rotated anything "
    "until you have called the actual edit tool in THIS turn. Never say an edit is done "
    "based on detection alone or on a previous turn - the edit tool must run this turn.\n"
    "- When you are done, briefly tell the user what you detected or changed. Keep it concise. "
    "Reply with plain text only. Do NOT wrap your answer in <thinking>, <response>, or any "
    "other tags - the user sees your reply directly."

)

# Per-request context: the uploaded image, the last YOLO prediction uid, and
# which tools were called. These flow AROUND the LLM (the model never sees image data).
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

        # Fetch the per-object bounding boxes so the model can target a specific
        # object (e.g. "the second dog from the right") when it asks for an edit.
        uid = result.get("prediction_uid")
        objects = []
        if uid:
            detail = client.get(f"{YOLO_SERVICE_URL}/prediction/{uid}")
            detail.raise_for_status()
            for obj in detail.json().get("detection_objects", []):
                # Round to whole pixels: the edit tools take integer coordinates.
                raw_box = obj["box"]
                if isinstance(raw_box, str):
                    raw_box = ast.literal_eval(raw_box)
                box = [round(float(c)) for c in raw_box]
                objects.append({"label": obj["label"], "score": obj["score"], "box": box})

    result["objects"] = objects
    return json.dumps(result)


# --- Image-processing tools (discovered over MCP) ---
# The image-processing tools (rotate, flip, blur, resize, crop, add_noise) are
# NOT defined here as local @tool functions; they are discovered from the
# img-proc MCP server over HTTP and combined with the local tools at startup.
mcp_client = MultiServerMCPClient(
    {"img_proc": {"url": IMG_PROC_MCP_URL, "transport": "streamable_http"}}
)
try:
    discovered = asyncio.run(mcp_client.get_tools())
    # Keep the single-image editing tools (those that take an `image_b64`
    # argument). Multi-image tools such as `paste` don't fit the "inject one
    # working image" model, so they are left out of the agent's toolset.
    mcp_image_tools = [t for t in discovered if "image_b64" in t.args]
    logging.info(
        "Discovered %d image tools over MCP from %s: %s",
        len(mcp_image_tools), IMG_PROC_MCP_URL, [t.name for t in mcp_image_tools],
    )
except Exception as e:
    logging.warning(
        "Could not reach the img-proc MCP server at %s (%s). "
        "Image-editing tools are disabled for this run.",
        IMG_PROC_MCP_URL, e,
    )
    mcp_image_tools = []

# The MCP image tools all take the working image as an `image_b64` argument. The
# model never sees or supplies image data, so we hide that argument from it (see
# _llm_facing_tools below) and inject the image ourselves at call time (see
# run_agent). These are the tool names and argument names that get that treatment.
MCP_IMAGE_TOOL_NAMES = {t.name for t in mcp_image_tools}
IMAGE_ARG_NAMES = {"image_b64"}

# Registry: map tool name -> tool object (local tools + MCP-discovered tools).
TOOLS = {detect_objects.name: detect_objects}
for _mcp_tool in mcp_image_tools:
    TOOLS[_mcp_tool.name] = _mcp_tool


def _extract_image(tool_message: ToolMessage) -> Optional[str]:
    """Pull the base64 PNG string out of an MCP tool's result message."""
    content = tool_message.content
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                return block["text"]
    return None


def _llm_facing_tools() -> list:
    """
    Build the tool list shown to the model: the local tools as-is, plus each
    MCP image tool with its `image_b64` argument stripped out. The model only
    chooses the operation and its parameters; the image is injected in run_agent,
    so image bytes never enter (or leave) the model's context.
    """
    tools = [detect_objects]
    for mcp_tool in mcp_image_tools:
        schema = convert_to_openai_tool(mcp_tool)
        params = schema["function"]["parameters"]
        params["properties"] = {
            name: prop
            for name, prop in params.get("properties", {}).items()
            if name not in IMAGE_ARG_NAMES
        }
        if "required" in params:
            params["required"] = [
                name for name in params["required"] if name not in IMAGE_ARG_NAMES
            ]
        tools.append(schema)
    return tools

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

llm_with_tools = llm.bind_tools(_llm_facing_tools())


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
    edit_nudges = 0

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

            # Guard: the model sometimes claims an edit without calling the tool.
            # If it says it edited but no image was produced, push it to do it.
            claims_edit = any(
                w in content.lower()
                for w in ("blurred", "flipped", "rotated", "resized", "cropped", "noise")
            )
            if claims_edit and processed_image_b64 is None and edit_nudges < 1 and i < max_iterations:
                edit_nudges += 1
                messages.append(HumanMessage(content=(
                    "You have NOT edited anything yet - you only detected. Do not call "
                    "detect_objects again. Call the edit tool now (blur/crop/flip/rotate) "
                    "and pass the target object's box. The box is already in the messages above."
                )))
                continue

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
            name = tool_call["name"]
            tools_called.append(name)
            tool_fn = TOOLS[name]

            if name in MCP_IMAGE_TOOL_NAMES:
                # Image-editing tool discovered over MCP. Inject the current
                # working image (the model never provides it), run it, then keep
                # the resulting image OUT of the model's context.
                working_image = _current_image_b64.get()
                if not working_image:
                    messages.append(ToolMessage(
                        content=json.dumps({"error": "No image was provided by the user."}),
                        tool_call_id=tool_call["id"],
                    ))
                    continue

                call = {**tool_call, "args": {**tool_call["args"], "image_b64": working_image}}
                # MCP tools are async-only; run the coroutine to completion here.
                tool_result = asyncio.run(tool_fn.ainvoke(call))   # returns a ToolMessage

                if getattr(tool_result, "status", "success") == "error":
                    # Let the model see the error text so it can recover.
                    messages.append(tool_result)
                    continue

                new_image = _extract_image(tool_result)
                if new_image:
                    processed_image_b64 = new_image
                    # Chain: the next edit operates on this result.
                    _current_image_b64.set(new_image)

                # Redact: the model gets a short confirmation, never image bytes.
                tool_result.content = json.dumps({"status": "ok", "operation": name})
                tool_result.artifact = None
                messages.append(tool_result)
                continue

            tool_result = tool_fn.invoke(tool_call)          # local tool -> ToolMessage
            messages.append(tool_result)

            # Capture the YOLO prediction uid from the tool's JSON result
            try:
                parsed = json.loads(tool_result.content)
                if isinstance(parsed, dict) and parsed.get("prediction_uid"):
                    prediction_uid = parsed["prediction_uid"]
            except (json.JSONDecodeError, AttributeError):
                pass

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
    processed_image: Optional[str] = None      # base64 result of an image-editing tool
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
        # Only show the annotated (boxes) image when the user just detected things.
        # If any edit tool ran, the edited result is what they want to see instead.
        edit_tools = {"rotate", "flip", "blur", "resize", "crop", "add_noise"}
        an_edit_ran = any(t in edit_tools for t in result.tools_called)
        annotated = None if an_edit_ran else (fetch_annotated_image(uid) if uid else None)

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