# services/img-proc-mcp/app.py
import base64
import io
import random

from fastmcp import FastMCP
from PIL import Image, ImageFilter, ImageOps

mcp = FastMCP("img-proc")


def _decode(b64: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")


def _encode(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()

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
def rotate(image_b64: str, angle: float = 90.0, box: list[int] | None = None) -> str:
    """Rotate an image, or just the region `box` [left, top, right, bottom]. Returns base64 PNG."""
    img = _decode(image_b64)
    fn = lambda im: im.rotate(-angle, expand=True)
    result = _apply_to_region(img, box, fn) if box else fn(img)
    return _encode(result)


@mcp.tool()
def flip(image_b64: str, direction: str = "horizontal", box: list[int] | None = None) -> str:
    """Flip an image, or just the region `box`, horizontally or vertically."""
    if direction == "horizontal":
        fn = ImageOps.mirror
    elif direction == "vertical":
        fn = ImageOps.flip
    else:
        raise ValueError("direction must be 'horizontal' or 'vertical'")
    img = _decode(image_b64)
    result = _apply_to_region(img, box, fn) if box else fn(img)
    return _encode(result)


@mcp.tool()
def blur(image_b64: str, radius: float = 2.0, box: list[int] | None = None) -> str:
    """Blur an image, or just the region `box` [left, top, right, bottom]."""
    img = _decode(image_b64)
    fn = lambda im: im.filter(ImageFilter.GaussianBlur(radius))
    result = _apply_to_region(img, box, fn) if box else fn(img)
    return _encode(result)


@mcp.tool()
def resize(image_b64: str, width: int, height: int) -> str:
    """Resize an image to the given width and height."""
    img = _decode(image_b64).resize((width, height))
    return _encode(img)


@mcp.tool()
def crop(image_b64: str, left: int, top: int, right: int, bottom: int) -> str:
    """Crop an image using bounding-box coordinates."""
    img = _decode(image_b64)
    return _encode(img.crop((left, top, right, bottom)))


@mcp.tool()
def paste(base_image_b64: str, patch_b64: str, left: int, top: int) -> str:
    """Paste a patch image onto a base image at the given top-left coordinates.

    Used to composite a transformed region (e.g. a blurred bounding-box crop)
    back into the full-size image it was cropped from.
    """
    base = _decode(base_image_b64)
    patch = _decode(patch_b64)
    base.paste(patch, (left, top))
    return _encode(base)


@mcp.tool()
def add_noise(image_b64: str, amount: float = 0.1, box: list[int] | None = None) -> str:
    """Add salt-and-pepper noise to an image, or just the region `box` [left, top, right, bottom]."""
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

    img = _decode(image_b64)
    result = _apply_to_region(img, box, fn) if box else fn(img)
    return _encode(result)


if __name__ == "__main__":
    # allowed_hosts="*": this server is only reachable inside the compose
    # network (never exposed publicly), and the agent talks to it via the
    # service name "img-proc-mcp", which FastMCP's Host-header guard would
    # otherwise reject as a DNS-rebinding attempt.
    mcp.run(transport="http", host="0.0.0.0", port=9000, allowed_hosts=["*"])