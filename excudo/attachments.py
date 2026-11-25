"""User-selected images, kept in memory until the next question is sent."""

import base64
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import warnings

from PIL import Image, UnidentifiedImageError

from .i18n import t


MAX_IMAGE_BYTES = 4 * 1024 * 1024  # per image as sent (after shrinking)
MAX_FILE_BYTES = 20 * 1024 * 1024  # per file read: a large photo is shrunk before the limit above
# A full input image encoded as base64, plus the helper's JSON envelope.
MAX_CLIPBOARD_OUTPUT_BYTES = 4 * ((MAX_FILE_BYTES + 2) // 3) + 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_IMAGES = 4
MAX_PIXELS = 16_000_000
# Longer side sent to the model. Its time and tokens depend on the pixels, not the file size.
MAX_SIDE = 1280
FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


@dataclass(frozen=True)
class Attachment:
    name: str
    data: bytes
    mime: str

    def part(self) -> dict:
        encoded = base64.b64encode(self.data).decode("ascii")
        # "name" stays in the history only (to_api drops it): older images are replaced by it.
        return {"type": "image_url", "image_url": {"url": f"data:{self.mime};base64,{encoded}"},
                "name": self.name}


def image_attachment(name: str, data: bytes) -> Attachment:
    if not data or len(data) > MAX_FILE_BYTES:
        raise ValueError(t("image_too_large"))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                mime = FORMATS.get(image.format)
                if mime is None or getattr(image, "is_animated", False):
                    raise ValueError(t("image_unsupported"))
                if image.width * image.height > MAX_PIXELS:
                    raise ValueError(t("image_too_many_pixels"))
                image.verify()
            # verify() alone does not decode JPEG pixels or reject every truncated image.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as error:
        raise ValueError(t("image_broken")) from error
    data = _shrunk(data, mime)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(t("image_shrunk_too_large"))
    return Attachment(name, data, mime)


# The image with its longer side reduced to MAX_SIDE, in the same format; unchanged if smaller.
def _shrunk(data: bytes, mime: str) -> bytes:
    with Image.open(io.BytesIO(data)) as image:
        if max(image.width, image.height) <= MAX_SIDE:
            return data
        image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        if mime == "image/jpeg":
            image.convert("RGB").save(output, "JPEG", quality=85)
        elif mime == "image/webp":
            image.save(output, "WEBP", quality=85)
        else:
            image.save(output, "PNG", optimize=True)
        return output.getvalue()


def _read_image(path: str, root: Path) -> Attachment:
    # The same checks as the file tools: inside the workspace, not a sensitive file, a regular
    # file with a single link (tools.workspace_path / regular_file).
    from .security import SENSITIVE_FILE_REFUSAL
    from .tools import regular_file, workspace_path

    try:
        target = regular_file(workspace_path(str(Path(path).expanduser())))
    except ValueError as error:
        if str(error) == SENSITIVE_FILE_REFUSAL:
            raise ValueError(t("image_sensitive_file")) from None
        if "outside the workspace" in str(error):
            raise ValueError(t("image_outside_workspace")) from None
        raise ValueError(t("image_regular_file")) from None
    if target.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(t("image_too_large"))
    with target.open("rb") as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    return image_attachment(target.name, data)


def split_paths(text: str) -> list[str]:
    """Accept quoted drag-and-drop paths without interpreting shell expressions."""
    text = text.strip()
    windows = (bool(re.match(r"[\"']?[a-zA-Z]:[\\/]", text))
               or text.lstrip('\"\'').startswith("\\\\")
               or (os.name == "nt" and not text.startswith("/")))
    try:
        if windows:
            # Explorer may insert a quoted path directly after the previous drop,
            # without a separating space. Quotes delimit paths even mid-input.
            paths = []
            for match in re.finditer(r'''"[^"]*"|'[^']*'|[^\s"']+''', text):
                token = match.group()
                if token[0] in "\"'":
                    paths.append(token[1:-1])
                else:
                    # Two unquoted absolute drive paths can also be adjacent.
                    # A drive marker cannot occur inside a normal Windows filename.
                    paths.extend(re.split(r"(?<=[^\\/])(?=[a-zA-Z]:[\\/])", token))
            # Check uncovered characters too: an unmatched quote must not be ignored.
            remainder = re.sub(r'''"[^"]*"|'[^']*'|[^\s"']+''', "", text)
            if remainder.strip():
                raise ValueError("unclosed quote")
            return paths
        return [part.strip('\"\'') for part in shlex.split(text, posix=not windows)]
    except ValueError as error:
        raise ValueError(t("image_path_quotes")) from error


def clipboard_images() -> dict:
    # The helper isolates native clipboard failures and bounds waits for Linux helpers.
    try:
        process = subprocess.run(
            [sys.executable, "-m", "excudo.clipboard_image"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, timeout=5, check=False,
        )
        if process.returncode != 0 or len(process.stdout) > MAX_CLIPBOARD_OUTPUT_BYTES:
            raise ValueError(t("image_clipboard_failed"))
        result = json.loads(process.stdout)
        if "error" in result:
            try:
                message = t(result["error"])
            except (KeyError, TypeError):
                message = t("image_clipboard_failed")
            raise ValueError(message)
        return result
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        raise ValueError(t("image_clipboard_unavailable")) from error


class Attachments:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.items: list[Attachment] = []

    def _extend(self, additions: list[Attachment]) -> None:
        if len(self.items) + len(additions) > MAX_IMAGES:
            raise ValueError(t("image_too_many"))
        if sum(len(item.data) for item in self.items + additions) > MAX_TOTAL_BYTES:
            raise ValueError(t("image_total_too_large"))
        self.items.extend(additions)

    def add_data(self, name: str, data: bytes) -> None:
        # Image bytes without a path (the desktop app's file picker, drop or paste):
        # the same checks and shrinking as a file chosen with /attach.
        self._extend([image_attachment(name, data)])

    def add_paths(self, paths: list[str]) -> None:
        if not paths or len(paths) + len(self.items) > MAX_IMAGES:
            raise ValueError(t("image_paths_required"))
        # Validate every selection before changing the pending list.
        self._extend([self._read_path(path) for path in paths])

    def _read_path(self, path: str) -> Attachment:
        try:
            return _read_image(path, self.root)
        except (OSError, ValueError) as error:
            reason = str(error) if isinstance(error, ValueError) else t("image_read_failed")
            raise ValueError(f"{path}: {reason}") from error

    def paste(self) -> None:
        result = clipboard_images()
        if "files" in result:
            paths = result["files"]
            if len(paths) != 1:
                raise ValueError(t("image_clipboard_single"))
            image = _read_image(paths[0], self.root)
        else:
            image = image_attachment("clipboard.png", base64.b64decode(result["image"], validate=True))
        # Paste selects one current image. Validate before replacing the previous selection.
        self.items[:] = [image]

    def content(self, question: str) -> str | list[dict]:
        if not self.items:
            return question
        return [{"type": "text", "text": question}, *[item.part() for item in self.items]]

    def clear(self) -> None:
        self.items.clear()
