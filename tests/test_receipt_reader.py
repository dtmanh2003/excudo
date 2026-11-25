import base64
import io
from types import SimpleNamespace

from PIL import Image
import pytest

from excudo.keichan.reader import extract_record, image_url, ReceiptReader
from excudo.keichan.validation import check_record, _amount


def test_image_is_rotated_and_shrunk():
    image = Image.new("RGB", (2500, 1800), "white")
    exif = image.getexif()
    exif[274] = 6
    output = io.BytesIO()
    image.save(output, "JPEG", exif=exif)
    encoded = image_url(output.getvalue())
    with Image.open(io.BytesIO(base64.b64decode(encoded.split(",", 1)[1]))) as result:
        assert max(result.size) == 1600
        assert result.height > result.width


@pytest.mark.parametrize("value", [b"broken", b"", b"x" * (20 * 1024 * 1024 + 1)], ids=["broken", "empty", "oversize"])
def test_invalid_image_not_sent(value):
    with pytest.raises((OSError, ValueError)):
        image_url(value)


def test_extract_last_json_without_following_text():
    assert extract_record('ignore me\n```json\n{"date":"2026-10-01","items":[{"name":"x"}]}\n```')["items"][0]["name"] == "x"
    with pytest.raises(ValueError):
        extract_record("no data")


def test_model_request_is_bounded_and_non_thinking():
    calls = []
    class API:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"date":"2026-10-01"}'))])
    reader = object.__new__(ReceiptReader)
    reader.model, reader.prompt = "test-model", "prompt"
    reader.client = SimpleNamespace(chat=SimpleNamespace(completions=API()))
    stream = io.BytesIO()
    Image.new("RGB", (500, 700)).save(stream, "PNG")
    assert reader(stream.getvalue(), "receipt.png")["date"] == "2026-10-01"
    assert calls[0]["max_tokens"] == 2000
    assert calls[0]["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
    assert calls[0]["messages"][1]["content"][1]["type"] == "image_url"


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "N/A"])
def test_bad_amounts(value):
    assert _amount(value) is None
