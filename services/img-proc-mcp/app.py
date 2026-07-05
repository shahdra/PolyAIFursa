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


@mcp.tool()
def rotate(image_b64: str, angle: float = 90.0) -> str:
    """Rotate an image by a given angle. Returns base64-encoded PNG."""
    img = _decode(image_b64).rotate(angle, expand=True)
    return _encode(img)


@mcp.tool()
def flip(image_b64: str, direction: str = "horizontal") -> str:
    """Flip an image horizontally or vertically."""
    img = _decode(image_b64)
    if direction == "horizontal":
        transformed = ImageOps.mirror(img)
    elif direction == "vertical":
        transformed = ImageOps.flip(img)
    else:
        raise ValueError("direction must be 'horizontal' or 'vertical'")
    return _encode(transformed)


@mcp.tool()
def blur(image_b64: str, radius: float = 2.0) -> str:
    """Apply Gaussian blur to an image. Returns base64-encoded PNG."""
    img = _decode(image_b64).filter(ImageFilter.GaussianBlur(radius))
    return _encode(img)


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
def add_noise(image_b64: str, amount: float = 0.1) -> str:
    """Add salt-and-pepper noise to an image."""
    img = _decode(image_b64)
    pixels = []
    for pixel in img.getdata():
        if random.random() < amount:
            pixels.append((255, 255, 255) if random.random() < 0.5 else (0, 0, 0))
        else:
            pixels.append(pixel)

    noisy_img = Image.new("RGB", img.size)
    noisy_img.putdata(pixels)
    return _encode(noisy_img)


if __name__ == "__main__":
    mcp.run(transport="http", port=9000)