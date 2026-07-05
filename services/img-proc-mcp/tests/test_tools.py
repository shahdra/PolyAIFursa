import base64
import io

from PIL import Image

from app import add_noise, blur, crop, flip, mcp, paste, resize, rotate


def _encode(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_rotate_flips_and_resizes_images():
    original = Image.new("RGB", (8, 4), color=(10, 20, 30))
    b64 = _encode(original)

    rotated = Image.open(io.BytesIO(base64.b64decode(rotate(b64, angle=90.0))))
    assert rotated.size == (4, 8)

    flipped = Image.open(io.BytesIO(base64.b64decode(flip(b64, direction="horizontal"))))
    assert flipped.size == (8, 4)

    resized = Image.open(io.BytesIO(base64.b64decode(resize(b64, width=16, height=8))))
    assert resized.size == (16, 8)


def test_crop_and_blur_and_noise_transform_images():
    original = Image.new("RGB", (10, 10), color=(255, 0, 0))
    b64 = _encode(original)

    cropped = Image.open(io.BytesIO(base64.b64decode(crop(b64, left=2, top=2, right=8, bottom=8))))
    assert cropped.size == (6, 6)

    blurred = Image.open(io.BytesIO(base64.b64decode(blur(b64, radius=1.0))))
    assert blurred.size == (10, 10)

    noisy = Image.open(io.BytesIO(base64.b64decode(add_noise(b64, amount=0.5))))
    assert noisy.size == (10, 10)


def test_paste_composites_a_patch_onto_the_base_image():
    base = Image.new("RGB", (10, 10), color=(255, 0, 0))
    patch = Image.new("RGB", (4, 4), color=(0, 255, 0))

    result = Image.open(io.BytesIO(base64.b64decode(paste(_encode(base), _encode(patch), left=2, top=3))))

    assert result.size == (10, 10)
    assert result.getpixel((3, 4)) == (0, 255, 0)  # inside the pasted patch
    assert result.getpixel((0, 0)) == (255, 0, 0)  # untouched base pixel


def test_all_tools_are_registered_with_the_mcp_server():
    import asyncio

    tools = asyncio.run(mcp.list_tools())
    registered = {t.name for t in tools}
    assert registered == {"rotate", "flip", "blur", "resize", "crop", "add_noise", "paste"}
