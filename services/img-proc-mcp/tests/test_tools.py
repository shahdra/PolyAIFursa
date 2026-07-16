import io
import json
from unittest.mock import patch

import pytest
from PIL import Image

import app
from app import add_noise, blur, crop, flip, mcp, paste, resize, rotate


class _FakeS3:
    """In-memory stand-in for boto3's S3 client, keyed like the real bucket."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}


@pytest.fixture(autouse=True)
def fake_s3():
    fake = _FakeS3()
    with patch.object(app, "s3_client", fake):
        yield fake


def _put(fake_s3, key: str, img: Image.Image):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    fake_s3.objects[key] = buf.getvalue()


def _get(fake_s3, key: str) -> Image.Image:
    return Image.open(io.BytesIO(fake_s3.objects[key]))


def _key_of(result_json: str) -> str:
    return json.loads(result_json)["s3_key"]


def test_rotate_flips_and_resizes_images(fake_s3):
    original = Image.new("RGB", (8, 4), color=(10, 20, 30))
    _put(fake_s3, "img1/original/image.jpg", original)

    rotated_key = _key_of(rotate("img1/original/image.jpg", angle=90.0))
    assert rotated_key == "img1/working.png"
    assert _get(fake_s3, rotated_key).size == (4, 8)

    flipped_key = _key_of(flip("img1/original/image.jpg", direction="horizontal"))
    assert _get(fake_s3, flipped_key).size == (8, 4)

    resized_key = _key_of(resize("img1/original/image.jpg", width=16, height=8))
    assert _get(fake_s3, resized_key).size == (16, 8)


def test_crop_and_blur_and_noise_transform_images(fake_s3):
    original = Image.new("RGB", (10, 10), color=(255, 0, 0))
    _put(fake_s3, "img2/original/image.jpg", original)

    cropped_key = _key_of(crop("img2/original/image.jpg", left=2, top=2, right=8, bottom=8))
    assert _get(fake_s3, cropped_key).size == (6, 6)

    blurred_key = _key_of(blur("img2/original/image.jpg", radius=1.0))
    assert _get(fake_s3, blurred_key).size == (10, 10)

    noisy_key = _key_of(add_noise("img2/original/image.jpg", amount=0.5))
    assert _get(fake_s3, noisy_key).size == (10, 10)


def test_chained_edits_overwrite_the_same_working_key(fake_s3):
    """A multi-step chain should leave one working object per turn, not one
    per step - each edit reads and overwrites `{image_id}/working.png`."""
    original = Image.new("RGB", (8, 4), color=(1, 2, 3))
    _put(fake_s3, "img3/original/image.jpg", original)

    after_rotate = _key_of(rotate("img3/original/image.jpg", angle=90.0))
    after_blur = _key_of(blur(after_rotate, radius=1.0))

    assert after_rotate == after_blur == "img3/working.png"
    assert _get(fake_s3, after_blur).size == (4, 8)  # blur preserves rotate's new size


def test_paste_composites_a_patch_onto_the_base_image(fake_s3):
    base = Image.new("RGB", (10, 10), color=(255, 0, 0))
    patch_img = Image.new("RGB", (4, 4), color=(0, 255, 0))
    _put(fake_s3, "img4/original/image.jpg", base)
    _put(fake_s3, "img4/patch.png", patch_img)

    result_key = _key_of(paste("img4/original/image.jpg", "img4/patch.png", left=2, top=3))
    result = _get(fake_s3, result_key)

    assert result.size == (10, 10)
    assert result.getpixel((3, 4)) == (0, 255, 0)  # inside the pasted patch
    assert result.getpixel((0, 0)) == (255, 0, 0)  # untouched base pixel


def test_all_tools_are_registered_with_the_mcp_server():
    import asyncio

    tools = asyncio.run(mcp.list_tools())
    registered = {t.name for t in tools}
    assert registered == {"rotate", "flip", "blur", "resize", "crop", "add_noise", "paste"}
