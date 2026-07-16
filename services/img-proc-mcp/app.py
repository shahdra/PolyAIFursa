# services/img-proc-mcp/app.py
import io
import json
import os
import random

import boto3
from fastmcp import FastMCP
from PIL import Image, ImageFilter, ImageOps
from starlette.requests import Request
from starlette.responses import JSONResponse

mcp = FastMCP("img-proc")


# Plain HTTP health check for Kubernetes liveness/readiness probes. The only
# other HTTP surface is the MCP endpoint at /mcp, which speaks the MCP protocol
# rather than answering a simple GET, so probes target this route instead.
@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})

S3_BUCKET = os.environ.get("AWS_S3_BUCKET", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
s3_client = boto3.client("s3", region_name=AWS_REGION)


def _image_id(s3_key: str) -> str:
    """The turn's image id is the first path segment of any of its S3 keys
    (e.g. "abc123/original/image.jpg" or "abc123/working.png" -> "abc123")."""
    return s3_key.split("/", 1)[0]


def _download_from_s3(s3_key: str) -> Image.Image:
    obj = s3_client.get_object(Bucket=S3_BUCKET, Key=s3_key)
    return Image.open(io.BytesIO(obj["Body"].read())).convert("RGB")


def _upload_to_s3(img: Image.Image, s3_key: str) -> str:
    """Overwrite this turn's single working image (rather than minting a new
    object per edit step) so a multi-step chain doesn't leave orphaned S3
    objects behind. Returns the key that was written."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    key = f"{_image_id(s3_key)}/working.png"
    s3_client.put_object(Bucket=S3_BUCKET, Key=key, Body=buf.getvalue(), ContentType="image/png")
    return key


def _result(img: Image.Image, s3_key: str) -> str:
    return json.dumps({"s3_key": _upload_to_s3(img, s3_key)})


def _apply_to_region(img: Image.Image, box, transform) -> Image.Image:
    """Crop `box`, run `transform` on just that region, paste it back."""
    left, top, right, bottom = box
    region = img.crop((left, top, right, bottom))
    edited = transform(region)
    # A region edit must fit back in the same slot (e.g. rotate can resize it).
    if edited.size != region.size:
        edited = edited.resize(region.size)
    out = img.copy()
    out.paste(edited, (left, top))
    return out

@mcp.tool()
def rotate(s3_key: str, angle: float = 90.0, box: list[int] | None = None) -> str:
    """Rotate the image at `s3_key`, or just the region `box` [left, top, right, bottom].
    Returns JSON {"s3_key": <new key>}."""
    img = _download_from_s3(s3_key)
    fn = lambda im: im.rotate(-angle, expand=True)
    result = _apply_to_region(img, box, fn) if box else fn(img)
    return _result(result, s3_key)


@mcp.tool()
def flip(s3_key: str, direction: str = "horizontal", box: list[int] | None = None) -> str:
    """Flip the image at `s3_key`, or just the region `box`, horizontally or vertically.
    Returns JSON {"s3_key": <new key>}."""
    if direction == "horizontal":
        fn = ImageOps.mirror
    elif direction == "vertical":
        fn = ImageOps.flip
    else:
        raise ValueError("direction must be 'horizontal' or 'vertical'")
    img = _download_from_s3(s3_key)
    result = _apply_to_region(img, box, fn) if box else fn(img)
    return _result(result, s3_key)


@mcp.tool()
def blur(s3_key: str, radius: float = 2.0, box: list[int] | None = None) -> str:
    """Blur the image at `s3_key`, or just the region `box` [left, top, right, bottom].
    Returns JSON {"s3_key": <new key>}."""
    img = _download_from_s3(s3_key)
    fn = lambda im: im.filter(ImageFilter.GaussianBlur(radius))
    result = _apply_to_region(img, box, fn) if box else fn(img)
    return _result(result, s3_key)


@mcp.tool()
def resize(s3_key: str, width: int, height: int) -> str:
    """Resize the image at `s3_key` to the given width and height. Returns JSON {"s3_key": <new key>}."""
    img = _download_from_s3(s3_key).resize((width, height))
    return _result(img, s3_key)


@mcp.tool()
def crop(s3_key: str, left: int, top: int, right: int, bottom: int) -> str:
    """Crop the image at `s3_key` using bounding-box coordinates. Returns JSON {"s3_key": <new key>}."""
    img = _download_from_s3(s3_key)
    return _result(img.crop((left, top, right, bottom)), s3_key)


@mcp.tool()
def paste(base_s3_key: str, patch_s3_key: str, left: int, top: int) -> str:
    """Paste the patch image at `patch_s3_key` onto the base image at `base_s3_key`,
    at the given top-left coordinates. Returns JSON {"s3_key": <new key>}.

    Used to composite a transformed region (e.g. a blurred bounding-box crop)
    back into the full-size image it was cropped from.
    """
    base = _download_from_s3(base_s3_key)
    patch = _download_from_s3(patch_s3_key)
    base.paste(patch, (left, top))
    return _result(base, base_s3_key)


@mcp.tool()
def add_noise(s3_key: str, amount: float = 0.1, box: list[int] | None = None) -> str:
    """Add salt-and-pepper noise to the image at `s3_key`, or just the region `box`
    [left, top, right, bottom]. Returns JSON {"s3_key": <new key>}."""
    def fn(im: Image.Image) -> Image.Image:
        pixels = []
        for pixel in im.getdata():
            if random.random() < amount:
                pixels.append((255, 255, 255) if random.random() < 0.5 else (0, 0, 0))
            else:
                pixels.append(pixel)
        noisy = Image.new("RGB", im.size)
        noisy.putdata(pixels)
        return noisy

    img = _download_from_s3(s3_key)
    result = _apply_to_region(img, box, fn) if box else fn(img)
    return _result(result, s3_key)


if __name__ == "__main__":
    # allowed_hosts="*": this server is only reachable inside the compose
    # network (never exposed publicly), and the agent talks to it via the
    # service name "img-proc-mcp", which FastMCP's Host-header guard would
    # otherwise reject as a DNS-rebinding attempt.
    mcp.run(transport="http", host="0.0.0.0", port=9000, allowed_hosts=["*"])
