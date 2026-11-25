"""Short-lived clipboard reader. Reads only after the user requests image paste."""

import base64
import io
import json
import subprocess
import sys

from PIL import Image, ImageGrab

from .attachments import MAX_FILE_BYTES, MAX_PIXELS


def _mac_file_clipboard() -> bool:
    # Finder publishes file URLs and an icon PNG. Never interpret that PNG as the file.
    script = '''ObjC.import('AppKit');
var types = ObjC.deepUnwrap($.NSPasteboard.generalPasteboard.types) || [];
JSON.stringify(types.indexOf('public.file-url') !== -1 ||
               types.indexOf('NSFilenamesPboardType') !== -1);'''
    process = subprocess.run(
        ["/usr/bin/osascript", "-l", "JavaScript", "-e", script],
        capture_output=True, timeout=2, check=False,
    )
    result = process.stdout.strip()
    if process.returncode != 0 or result not in {b"true", b"false"}:
        raise ValueError("image_clipboard_kind_failed")
    return result == b"true"


def read() -> dict:
    if sys.platform == "darwin":
        try:
            if _mac_file_clipboard():
                return {"error": "image_clipboard_finder_file"}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            # Failing open here could silently attach Finder's icon instead of the image.
            return {"error": "image_clipboard_kind_failed"}
    image = ImageGrab.grabclipboard()
    if isinstance(image, list):
        return {"files": image}
    if not isinstance(image, Image.Image):
        return {"error": "image_clipboard_empty"}
    with image:
        if image.width * image.height > MAX_PIXELS:
            return {"error": "image_too_many_pixels"}
        output = io.BytesIO()
        # A new pixel image omits clipboard metadata, including EXIF.
        clean = Image.new("RGBA", image.size)
        clean.paste(image.convert("RGBA"))
        clean.save(output, format="PNG")
        data = output.getvalue()
        # image_attachment in the parent applies the same shrinking and sent-size
        # checks as /attach. This is only the input-size limit before shrinking.
        if len(data) > MAX_FILE_BYTES:
            return {"error": "image_too_large"}
        return {"image": base64.b64encode(data).decode("ascii")}


if __name__ == "__main__":
    try:
        result = read()
    except Exception:
        result = {"error": "image_clipboard_backend_failed"}
    print(json.dumps(result, ensure_ascii=True))
