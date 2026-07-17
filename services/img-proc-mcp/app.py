# services/img-proc-mcp/app.py
#
# Task 4 - Part I: Image processing MCP server
#
# This is a standalone server that gives the agent a toolbox of image edits:
# rotate, flip, blur, resize, crop, paste, add_noise. It speaks the MCP
# (Model Context Protocol) - a standard way for an AI agent to discover a
# list of tools and call them, without the agent needing any image-editing
# code of its own.
#
# How an edit happens, end to end:
#   1. The agent already has the image sitting in an S3 bucket (uploaded
#      earlier, e.g. by the user or by Yolo's detection step).
#   2. The agent calls one of the tools below, passing only the S3 *key*
#      (a short string like "abc123/original.png") - never the raw image
#      bytes. This keeps requests small and fast even for big images.
#   3. The tool downloads the image from S3, edits it with Pillow (PIL),
#      uploads the result back to S3 under a new key, and returns that key.
#   4. The agent can chain several tools together this way, each one
#      picking up where the last left off.
#
# For "edit just this object" requests (e.g. "blur the second dog"), the
# agent first asks Yolo for that object's bounding box, then passes it as
# the optional `box` argument so only that region of the image is changed.
import io
import json
import os
import random

import boto3
from fastmcp import FastMCP
from PIL import Image, ImageFilter, ImageOps

mcp = FastMCP("img-proc")

S3_BUCKET = os.environ.get("AWS_S3_BUCKET", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
s3_client = boto3.client("s3", region_name=AWS_REGION)


def _image_id(s3_key: str) -> str:
    """Every image's S3 keys share the same first path segment, e.g.
    "abc123/original/image.jpg" and "abc123/working.png" both belong to
    image "abc123". This pulls that shared id back out of any key."""
    return s3_key.split("/", 1)[0]


def _download_from_s3(s3_key: str) -> Image.Image:
    """Fetch the image bytes for `s3_key` and load them as a Pillow image."""
    obj = s3_client.get_object(Bucket=S3_BUCKET, Key=s3_key) # Get the object from S3 using the provided s3_key. This returns a dictionary containing metadata and the object's body (the image bytes).
    return Image.open(io.BytesIO(obj["Body"].read())).convert("RGB") # decode the image bytes into a Pillow Image object. The image is converted to RGB mode to ensure consistent color handling.


def _upload_to_s3(img: Image.Image, s3_key: str) -> str:
    """Save the edited image back to S3 as this image's single "working"
    copy - one shared key per image (not a brand-new file per edit), so a
    chain of several edits doesn't pile up leftover files in the bucket.
    Returns the S3 key the image was saved under."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    key = f"{_image_id(s3_key)}/working.png" # The S3 key under which the edited image will be saved. It uses the same image ID as the original image, but with a fixed path segment indicating that this is the working copy of the image.
    s3_client.put_object(Bucket=S3_BUCKET, Key=key, Body=buf.getvalue(), ContentType="image/png") # Upload the edited image bytes to S3 under the specified key. The content type is set to "image/png" for proper handling.
    return key # Return the S3 key under which the edited image was saved. This allows the agent to reference the new image in subsequent tool calls or responses.


def _result(img: Image.Image, s3_key: str) -> str:
    """The shared return shape for every tool below: upload the edited
    image and hand the agent back its new S3 key as JSON."""
    return json.dumps({"s3_key": _upload_to_s3(img, s3_key)}) # Upload the edited image to S3 and return a JSON string containing the new S3 key. The _upload_to_s3 function handles the upload and returns the S3 key under which the image was saved.


def _apply_to_region(img: Image.Image, box, transform) -> Image.Image:
    """Shared helper for "edit just one object" requests: crop out `box`,
    run `transform` on only that piece, then paste it back into the full
    image at the same spot."""
    left, top, right, bottom = box
    region = img.crop((left, top, right, bottom))
    edited = transform(region)
    # Some transforms change the region's size (e.g. rotating a rectangle) -
    # resize it back so it still fits the hole it came out of.
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
    img = _download_from_s3(s3_key) # Download the image from S3 using the provided s3_key. This returns a Pillow Image object. 
    fn = lambda im: im.filter(ImageFilter.GaussianBlur(radius))
    result = _apply_to_region(img, box, fn) if box else fn(img) # Apply the blur filter to the specified region of the image if a box is provided, otherwise apply it to the whole image. The result is a new Pillow Image object with the blur applied.
    return _result(result, s3_key) # Upload the edited image back to S3 and return the new S3 key as JSON. The _result function handles the upload and returns a JSON string containing the new S3 key.


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
    # allowed_hosts="*": FastMCP normally rejects requests whose Host header
    # doesn't match "localhost", to block a browser-based attack called DNS
    # rebinding. This server is never exposed to the public internet - only
    # the agent container reaches it, over Docker's internal network, using
    # the hostname "img-proc-mcp" - so that protection is safely relaxed here.
    mcp.run(transport="http", host="0.0.0.0", port=9000, allowed_hosts=["*"])
