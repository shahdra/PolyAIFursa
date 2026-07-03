import base64
import io
import json
import logging
import os
import time
from contextvars import ContextVar
from typing import Optional
from langchain_core.rate_limiters import InMemoryRateLimiter

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logging.getLogger("langchain").setLevel(logging.DEBUG)
logging.getLogger("langchain_core").setLevel(logging.DEBUG)

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel

YOLO_SERVICE_URL = os.environ.get("YOLO_SERVICE_URL", "http://localhost:8080")
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
    "Use the available tools to extract information from images. "
)

# Per-request context: the uploaded image, the last YOLO prediction uid, and
# which tools were called. These flow AROUND the LLM (the model never sees image data).
_current_image_b64: ContextVar[Optional[str]] = ContextVar("current_image_b64", default=None)
_tools_called: ContextVar[list] = ContextVar("tools_called", default=[])


@tool
def detect_objects() -> str:
    """Detect and identify objects in the image provided by the user using YOLO object detection."""
    image_b64 = _current_image_b64.get()
    if not image_b64:
        return json.dumps({"error": "No image was provided by the user."})

    image_bytes = base64.b64decode(image_b64)
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{YOLO_SERVICE_URL}/predict",
            files={"file": ("image.jpg", io.BytesIO(image_bytes), "image/jpeg")},
        )
        response.raise_for_status()
    result = response.json()

    return json.dumps(result)


# Registry: map tool name -> tool function
TOOLS = {
    detect_objects.name: detect_objects
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
                tokens_used=tokens,
            )

        # Execute every tool the model requested
        for tool_call in response.tool_calls:
            tools_called.append(tool_call["name"])
            tool_fn = TOOLS[tool_call["name"]]
            tool_result = tool_fn.invoke(tool_call)          # returns a ToolMessage
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