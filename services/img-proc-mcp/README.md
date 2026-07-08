# Image Processing MCP Server

A [FastMCP](https://gofastmcp.com) server that exposes basic image-editing operations
(rotate, flip, blur, resize, crop, add noise, paste) as MCP tools. The vision agent
(`services/agent`) connects to this server as an MCP client so it can edit images in
response to natural-language requests.

## Setup

Install dependencies (from `services/img-proc-mcp/`):

```bash
pip install -r requirements.txt
```

## Running

```bash
python app.py
```

The server starts at `http://localhost:9000/mcp` (streamable HTTP transport).

## Running tests

```bash
pytest tests/
```

## Tools

Every tool takes and returns a base64-encoded PNG string (`image_b64`).

| Tool | Arguments | Description |
|---|---|---|
| `rotate` | `image_b64`, `angle` | Rotate by `angle` degrees (counter-clockwise) |
| `flip` | `image_b64`, `direction` | Flip `"horizontal"` or `"vertical"` |
| `resize` | `image_b64`, `width`, `height` | Resize to exact pixel dimensions |
| `crop` | `image_b64`, `left`, `top`, `right`, `bottom` | Crop to a bounding box |
| `blur` | `image_b64`, `radius` | Gaussian blur |
| `add_noise` | `image_b64`, `amount` | Salt-and-pepper noise |
| `paste` | `base_image_b64`, `patch_b64`, `left`, `top` | Composite a patch onto a base image at the given top-left coordinates |

`paste` exists so a caller can edit a single region without touching the rest of the
image: crop the target region out, transform just that patch, then paste it back at
the same coordinates.

## Design notes: how the agent talks to this server

Two questions came up when wiring the agent up to this server:

**Base64 in the request vs. S3.** The agent already uses S3 to hand images to the
YOLO service, because YOLO persists predictions (original + annotated image, boxes,
labels) in a database that's queried later by uid/label/score — the image needs to
outlive the request. Transforms here are different: they're a synchronous,
in-process-feeling step within a single chat turn, with no need to persist anything
beyond that turn. Given that, this server keeps the simpler base64-in/base64-out
interface, and the agent passes image bytes directly in the MCP call rather than
introducing an S3 round trip that would just add latency with no benefit.

**Whole image vs. just the bounding box.** For object-specific edits (e.g. "blur the
second dog from the right"), the agent crops out only the detected object's bounding
box (via the `crop` tool) and sends just that small region through `blur`/`add_noise`,
then reassembles the full image with `paste`. This keeps each MCP payload as small as
the target region instead of the whole photo, and confines the edit to exactly the
pixels the user asked about.
