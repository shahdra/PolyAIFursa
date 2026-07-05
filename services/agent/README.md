# Vision Agent

A LangChain-powered AI vision agent with a manual ReAct loop. Accepts text and base64-encoded images, and can call tools (e.g. YOLO object detection, image editing via MCP) to answer questions and edit images.

## Prerequisites

- Python 3.10+
- A running YOLO service (optional - only needed for `detect_objects`)
- A running img-proc-mcp service (optional - only needed for image-editing tools; see `services/img-proc-mcp`)


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
| `IMG_PROC_MCP_URL` | `http://localhost:9000/mcp` | URL of the img-proc-mcp server |

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
  "annotated_image": "string | null",
  "processed_image": "string | null",
  "agent_loop_time_s": "number | null",
  "iterations": "number | null",
  "tools_called": ["string"],
  "tokens_used": {"input": 0, "output": 0, "total": 0},
  "context_limit_exceeded": false
}
```

`processed_image` is a base64 PNG, set whenever an image-editing tool ran (see below).

## Image-editing tools (img-proc-mcp)

In addition to `detect_objects`, the agent has tools that call the
[img-proc-mcp](../img-proc-mcp) server to edit the uploaded image:

| Tool | Arguments | Scope |
|---|---|---|
| `rotate_image` | `angle` | Whole image |
| `flip_image` | `direction` (`horizontal`/`vertical`) | Whole image |
| `resize_image` | `width`, `height` | Whole image |
| `crop_image` | `left`, `top`, `right`, `bottom` | Whole image |
| `blur_image` | `radius`, optional `left`/`top`/`right`/`bottom` | Whole image, or one detected object |
| `add_noise_image` | `amount`, optional `left`/`top`/`right`/`bottom` | Whole image, or one detected object |

To target a specific object (e.g. "blur the second dog from the right"), the model
first calls `detect_objects`, which returns an `objects` list of
`{label, score, box: [left, top, right, bottom]}`. The model works out which object
is meant from those coordinates and passes that exact box to `blur_image` /
`add_noise_image`. Omitting the box arguments (or using `rotate_image` /
`flip_image` / `resize_image` / `crop_image`, which always act on the whole image)
affects the entire image instead.

The image itself never enters the LLM's context — tool results only ever describe
*what* was done (status, operation, box), never image bytes. The actual base64
result is threaded back to `/chat` through `AgentResult.processed_image_b64` and
returned to the caller as `processed_image`.

### `GET /health`

Returns `{"status": "ok"}` when the service is running.
