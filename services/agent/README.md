# Vision Agent

A LangChain-powered AI vision agent with a manual ReAct loop. Accepts text and base64-encoded images, and can call tools (YOLO object detection plus image editing) to answer questions and edit images.

## Prerequisites

- Python 3.10+
- A running YOLO service (optional - only needed for `detect_objects`)
- A running img-proc MCP service (optional - only needed for the image-editing tools; see `services/img-proc-mcp`)


## Setup

Install dependencies (from `services/agent/`):

```bash
pip install -r requirements.txt
```

Configure environment:

```bash
cp .env.example .env
# Edit .env and set at least OPENAI_API_KEY (or another provider key) and MODEL
```

`.env` variables:

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | - | Required for OpenAI models |
| `ANTHROPIC_API_KEY` | - | Required for Anthropic models |
| `GOOGLE_API_KEY` | - | Required for Google models |
| `MODEL` | `claude-sonnet-4-6` | Any model string supported by `init_chat_model` |
| `YOLO_SERVICE_URL` | `http://localhost:8080` | URL of the YOLO microservice |
| `IMG_PROC_MCP_URL` | `http://localhost:9000/mcp` | URL of the img-proc MCP server |

## Tools

The agent combines two kinds of tools at startup:

- **Local tools** defined in `app.py` — currently `detect_objects`, which uploads the
  image to S3 and runs YOLO object detection, returning each object's label, score, and
  bounding box.
- **Image-editing tools discovered over MCP** — `rotate`, `flip`, `blur`, `resize`,
  `crop`, and `add_noise` are **not** defined in `app.py`. They are discovered from the
  [img-proc MCP server](../img-proc-mcp) over HTTP (`IMG_PROC_MCP_URL`) when the agent
  starts, and merged into the tool registry. If the MCP server is unreachable at startup,
  the agent logs a warning and runs with only the local tools.

The LLM never handles image bytes: the `image_b64` argument is hidden from the tool
schema the model sees, injected from the current working image at call time, and the
resulting image is stripped from the tool result before it re-enters the model's context.
The edited image is returned to the caller in the `processed_image` response field.

## Running

```bash
cd services/agent
python app.py
```

The server starts at `http://localhost:8000`.

## Testing with curl

### Health check

```bash
curl http://localhost:8000/health
```

Expected response:
```json
{"status": "ok"}
```

### Plain text message

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello! What can you do?"}'
```

### Send a message with an image

```bash
echo "{\"message\": \"What objects are in this image?\", \"image_base64\": \"$(base64 -w0 beatles.jpeg)\"}" \
  | curl -X POST http://localhost:8000/chat \
         -H "Content-Type: application/json" \
         -d @-
```

## API Reference

### `POST /chat`

Request body:

```json
{
  "message": "string (optional, defaults to 'What's in this image?')",
  "image_base64": "string (optional, base64-encoded JPEG or PNG)"
}
```

Response:

```json
{
  "response": "string",
  "prediction_id": "string | null",
  "annotated_image": "string | null (base64 of the YOLO bounding-box image)",
  "processed_image": "string | null (base64 result of an image-editing tool)",
  "agent_loop_time_s": "number | null",
  "iterations": "number | null",
  "tools_called": ["string"],
  "tokens_used": {"input": 0, "output": 0, "total": 0},
  "context_limit_exceeded": false
}
```

### `GET /health`

Returns `{"status": "ok"}` when the service is running.
