"""Read-only previews of project files for the editor's プレビュー view.

Each function turns the bytes of one file into plain data the page renders (and escapes):
a table, pages as images, paragraphs, text. Nothing here writes a file or sends anything to
the model; sizes are bounded so a large file cannot freeze the app.
"""

import base64
import csv
import io
import math
import zipfile
from contextlib import ExitStack
from datetime import datetime

MAX_SHEETS = 20
MAX_ROWS = 1000
MAX_COLUMNS = 60
MAX_CELL = 500
MAX_PDF_PAGES = 30
MAX_TEXT = 1024 * 1024
MAX_BLOCKS = 2000
MAX_OFFICE_BYTES = 100 * 1024 * 1024
MAX_OFFICE_ENTRIES = 2000
# Limits of the separate process that parses Excel / PDF / Word (see isolated()).
WORKER_TIMEOUT = 30
WORKER_MEMORY = 768 * 1024 * 1024

# Extensions shown as images, tables, pages, documents, audio / video.
IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
EXCEL = {".xlsx", ".xlsm"}
TABLES = {".csv", ".tsv"}
MEDIA = {".mp4": "video/mp4", ".webm": "video/webm", ".mp3": "audio/mpeg", ".wav": "audio/wav",
         ".m4a": "audio/mp4", ".ogg": "audio/ogg"}
# The only types opened with their default Windows application: documents without macros,
# plain data, images, audio / video and archives (shown by Explorer). An allow list, because
# many other types run code when opened (programs, scripts, help files, add-ins, scriptlets,
# shortcuts, Office files with macros, the old .doc / .xls formats) and a deny list always
# misses some.
OPEN_EXTERNALLY = {
    ".pdf", ".docx", ".xlsx", ".pptx", ".csv", ".tsv", ".txt", ".md", ".log",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff",
    ".mp4", ".webm", ".mov", ".mp3", ".wav", ".m4a", ".ogg", ".zip",
}


# A cell value as short text.
def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        text = value.isoformat(sep=" ")
    else:
        text = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return text if len(text) <= MAX_CELL else text[:MAX_CELL] + "…"


# Office documents are ZIP containers: the compressed file size alone does not bound
# the memory their XML, shared strings and embedded files consume when parsed.
def _check_office_archive(data: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_OFFICE_ENTRIES or sum(item.file_size for item in entries) > MAX_OFFICE_BYTES:
            raise ValueError("Office document expands beyond the preview limit")


# The sheets of an Excel workbook: [{"name", "rows", "truncated"}]. Shows the values Excel
# last calculated (a formula without a saved value shows the formula).
def excel(data: bytes) -> dict:
    from openpyxl import load_workbook

    _check_office_archive(data)
    with ExitStack() as resources:
        values = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        resources.callback(values.close)
        formulas = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
        resources.callback(formulas.close)
        sheets = []
        for ws, raw in list(zip(values.worksheets, formulas.worksheets))[:MAX_SHEETS]:
            rows, truncated = [], False
            for row, raw_row in zip(ws.iter_rows(values_only=True), raw.iter_rows(values_only=True)):
                if len(rows) >= MAX_ROWS:
                    truncated = True
                    break
                cells = [_cell(v if v is not None else f) for v, f in zip(row, raw_row)]
                truncated = truncated or len(cells) > MAX_COLUMNS
                rows.append(cells[:MAX_COLUMNS])
            while rows and not any(rows[-1]):
                rows.pop()
            sheets.append({"name": ws.title, "rows": rows, "truncated": truncated})
        return {"kind": "table", "sheets": sheets, "more_sheets": len(values.sheetnames) > MAX_SHEETS}


# Text of a file: UTF-8 (with or without BOM), else Shift_JIS (cp932, common for Japanese
# CSV from Excel). None for binary data.
def decode(data: bytes) -> str | None:
    if b"\x00" in data[:8192]:
        return None
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


# A CSV / TSV file as one table.
def table(data: bytes, delimiter: str) -> dict:
    text = decode(data[:MAX_TEXT])
    if text is None:
        return info_only()
    rows, truncated = [], len(data) > MAX_TEXT
    for row in csv.reader(io.StringIO(text), delimiter=delimiter):
        if len(rows) >= MAX_ROWS:
            truncated = True
            break
        truncated = truncated or len(row) > MAX_COLUMNS
        rows.append([_cell(c) for c in row[:MAX_COLUMNS]])
    return {"kind": "table", "sheets": [{"name": "", "rows": rows, "truncated": truncated}], "more_sheets": False}


# The pages of a PDF as JPEG images.
def pdf(data: bytes) -> dict:
    import pypdfium2

    document = pypdfium2.PdfDocument(data)
    try:
        pages = []
        for index in range(min(len(document), MAX_PDF_PAGES)):
            page = document[index]
            try:
                width, height = page.get_width(), page.get_height()
                if not all(math.isfinite(value) and value > 0 for value in (width, height)):
                    raise ValueError("Invalid PDF page dimensions")
                # A narrow but very tall page must not allocate an unbounded bitmap.
                bitmap = page.render(scale=min(2.0, 1100 / width, 1600 / height))
                try:
                    image = bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()
            finally:
                page.close()
            out = io.BytesIO()
            image.save(out, "JPEG", quality=82)
            pages.append("data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii"))
        return {"kind": "pages", "pages": pages, "page_count": len(document)}
    finally:
        document.close()


# The paragraphs and tables of a Word document, in order (text only, no formatting).
def word(data: bytes) -> dict:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    _check_office_archive(data)
    document = Document(io.BytesIO(data))
    blocks, truncated = [], False
    for child in document.element.body.iterchildren():
        if len(blocks) >= MAX_BLOCKS:
            truncated = True
            break
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(child, document)
            style = (paragraph.style.name if paragraph.style is not None else "") or ""
            level = int(style.split()[-1]) if style.startswith("Heading") and style.split()[-1].isdigit() else 0
            blocks.append({"type": "heading" if level else "paragraph", "level": min(level, 4), "text": paragraph.text})
        elif tag == "tbl":
            rows = [[_cell(cell.text) for cell in row.cells[:MAX_COLUMNS]] for row in Table(child, document).rows[:MAX_ROWS]]
            blocks.append({"type": "table", "rows": rows})
    return {"kind": "document", "blocks": blocks, "truncated": truncated}


# Runs excel() / pdf() / word() in a separate process (viewer_worker.py): third-party parsers
# on a file from anywhere. Memory-limited Job Object (Windows), timeout, minimal environment
# (no API keys); a crash, a memory bomb or a hang ends only that process. Raises
# TimeoutError or RuntimeError, which the caller shows as "cannot be displayed".
def isolated(kind: str, data: bytes) -> dict:
    import json
    from pathlib import Path
    import subprocess
    import sys

    from ..security import subprocess_environment

    if kind in ("excel", "word"):
        _check_office_archive(data)  # an obvious zip bomb never starts a process
    script = Path(__file__).with_name("viewer_worker.py")
    job = None
    if sys.platform == "win32":
        from ..process_tree import ProcessJob

        job = ProcessJob(memory_limit=WORKER_MEMORY)
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=subprocess_environment(),
            creationflags=no_window | (4 if job is not None else 0),  # 4 = CREATE_SUSPENDED until in the job
        )
        try:
            if job is not None:
                job.attach_and_resume(process)
            header = json.dumps({"kind": kind}).encode("ascii") + b"\n"
            output, _ = process.communicate(header + data, timeout=WORKER_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise TimeoutError("preview took too long")
        except BaseException:
            process.kill()
            raise
        if process.returncode != 0:
            raise RuntimeError("the preview process failed")
        return json.loads(output)
    finally:
        if job is not None:
            job.close()


# Audio or video as a data URL the page plays.
def media(data: bytes, mime: str) -> dict:
    return {"kind": "media", "mime": mime, "data_url": f"data:{mime};base64," + base64.b64encode(data).decode("ascii")}


# Plain text (the first MAX_TEXT bytes).
def text(data: bytes, size: int) -> dict:
    decoded = decode(data[:MAX_TEXT])
    if decoded is None:
        return info_only()
    return {"kind": "text", "text": decoded, "truncated": size > MAX_TEXT}


# A file that cannot be shown: only its information is.
def info_only() -> dict:
    return {"kind": "info"}
