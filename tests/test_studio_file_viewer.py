"""Read-only previews of project files (the editor's プレビュー view): Excel, CSV, PDF, Word,
images, audio / video, text and anything else, bounded and through the shared file check."""

import os
import io
import zipfile
from datetime import date, datetime, time

from docx import Document
from openpyxl import Workbook
from PIL import Image
import pytest

from excudo.app import viewer
from excudo.app.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path)


@pytest.mark.parametrize("value,expected", [
    (date(2026, 10, 9), "2026-10-09"), (time(12, 30), "12:30:00"),
    (datetime(2026, 10, 9, 12, 30), "2026-10-09 12:30:00"),
])
def test_excel_date_and_time_cells_are_supported(value, expected):
    assert viewer._cell(value) == expected


def test_excel_workbook_with_time_cell_still_has_table_preview(ws, tmp_path):
    workbook = Workbook()
    workbook.active.append(["Time", time(12, 30)])
    workbook.save(tmp_path / "time.xlsx")
    result = ws.view("time.xlsx")
    assert result["kind"] == "table"
    assert result["sheets"][0]["rows"] == [["Time", "12:30:00"]]


def test_excel_closes_first_workbook_when_second_load_fails(monkeypatch):
    import openpyxl

    closed = []

    class WorkbookStub:
        def close(self):
            closed.append(True)

    def load(*args, data_only, **kwargs):
        if not data_only:
            raise ValueError("second load failed")
        return WorkbookStub()

    monkeypatch.setattr(viewer, "_check_office_archive", lambda data: None)
    monkeypatch.setattr(openpyxl, "load_workbook", load)
    with pytest.raises(ValueError, match="second load failed"):
        viewer.excel(b"fake")
    assert closed == [True]


@pytest.mark.parametrize("directory", [".GIT", ".AWS", ".CODEX", ".AGENTS", ".EXCUDO"])
def test_internal_directories_cannot_be_accessed_with_uppercase_names(ws, tmp_path, directory):
    folder = tmp_path / directory
    folder.mkdir()
    (folder / "config.txt").write_text("internal data", encoding="utf-8")
    with pytest.raises(ValueError):
        ws.view(f"{directory}/config.txt")
    assert ws.files() == []


def test_personal_records_cannot_be_accessed_with_uppercase_names(ws, tmp_path):
    folder = tmp_path / "KEICHAN_DATA" / "RECORDS"
    folder.mkdir(parents=True)
    (folder / "2026-10.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        ws.view("KEICHAN_DATA/RECORDS/2026-10.json")
    assert ws.files() == []


def test_excel_sheets_are_shown_as_tables(ws, tmp_path):
    wb = Workbook()
    wb.active.title = "10月"
    wb.active.append(["月日", "金額"])
    wb.active.append(["10/05", 819])
    wb.active["B3"] = "=SUM(B2:B2)"  # never calculated by Excel: the formula is shown
    wb.create_sheet("メモ").append(["note"])
    wb.save(tmp_path / "経費申請書_2026-10.xlsx")
    view = ws.view("経費申請書_2026-10.xlsx")
    assert view["kind"] == "table" and [s["name"] for s in view["sheets"]] == ["10月", "メモ"]
    assert view["sheets"][0]["rows"] == [["月日", "金額"], ["10/05", "819"], ["", "=SUM(B2:B2)"]]


def test_a_japanese_csv_saved_by_excel_is_read(ws, tmp_path):
    (tmp_path / "list.csv").write_bytes("名前,金額\n\"山田, 太郎\",1200\n".encode("cp932"))
    view = ws.view("list.csv")
    assert view["sheets"][0]["rows"] == [["名前", "金額"], ["山田, 太郎", "1200"]]


def test_pdf_pages_become_images(ws, tmp_path):
    pages = [Image.new("RGB", (600, 800), color) for color in ("white", "ivory")]
    pages[0].save(tmp_path / "doc.pdf", save_all=True, append_images=pages[1:])
    view = ws.view("doc.pdf")
    assert view["kind"] == "pages" and view["page_count"] == 2
    assert all(p.startswith("data:image/jpeg;base64,") for p in view["pages"])


@pytest.mark.parametrize("width,height", [
    (10, 1_000_000), (1_000_000, 10), (0, 100), (100, -1), (float("inf"), 100), (100, float("nan")),
])
def test_pdf_render_bounds_both_dimensions(monkeypatch, width, height):
    import math
    import pypdfium2

    closed = []

    class Bitmap:
        def to_pil(self):
            return Image.new("RGB", (1, 1))

        def close(self):
            closed.append("bitmap")

    class Page:
        def get_width(self):
            return width

        def get_height(self):
            return height

        def render(self, scale):
            assert width * scale <= 1100
            assert height * scale <= 1600
            return Bitmap()

        def close(self):
            closed.append("page")

    class Document:
        def __len__(self):
            return 1

        def __getitem__(self, index):
            return Page()

        def close(self):
            closed.append("document")

    monkeypatch.setattr(pypdfium2, "PdfDocument", lambda data: Document())
    if not all(math.isfinite(value) and value > 0 for value in (width, height)):
        with pytest.raises(ValueError, match="Invalid PDF page dimensions"):
            viewer.pdf(b"fake")
        assert closed == ["page", "document"]
        return
    assert viewer.pdf(b"fake")["page_count"] == 1
    assert closed == ["bitmap", "page", "document"]


def test_word_paragraphs_and_tables_in_order(ws, tmp_path):
    doc = Document()
    doc.add_heading("報告書", level=1)
    doc.add_paragraph("本文です。")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = "項目", "値"
    doc.save(tmp_path / "report.docx")
    blocks = ws.view("report.docx")["blocks"]
    assert blocks[0] == {"type": "heading", "level": 1, "text": "報告書"}
    assert blocks[1]["text"] == "本文です。" and blocks[2] == {"type": "table", "rows": [["項目", "値"]]}


def test_images_media_and_text(ws, tmp_path):
    Image.new("RGB", (40, 30), "white").save(tmp_path / "a.png")
    (tmp_path / "s.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', encoding="utf-8")
    (tmp_path / "v.mp3").write_bytes(b"ID3fake")
    (tmp_path / "big.log").write_bytes(b"x" * (viewer.MAX_TEXT + 10))
    assert ws.view("a.png")["kind"] == "image"
    assert ws.view("s.svg")["data_url"].startswith("data:image/svg+xml;base64,")  # shown as <img>: no script runs
    assert ws.view("v.mp3")["data_url"].startswith("data:audio/mpeg;base64,")
    log = ws.view("big.log")
    assert log["kind"] == "text" and log["truncated"] and len(log["text"]) == viewer.MAX_TEXT


def test_other_files_show_their_information_and_programs_are_never_opened(ws, tmp_path, monkeypatch):
    (tmp_path / "data.zip").write_bytes(b"PK\x03\x04\x00\x00binary")
    (tmp_path / "tool.exe").write_bytes(b"MZ\x00\x00")
    opened = []
    monkeypatch.setattr(os, "startfile", lambda path: opened.append(path), raising=False)
    zip_view = ws.view("data.zip")
    assert zip_view["kind"] == "info" and zip_view["external"] is True and zip_view["size"] == 12
    assert ws.view("tool.exe")["external"] is False
    with pytest.raises(ValueError):
        ws.open_externally("tool.exe")
    assert opened == []
    ws.open_externally("data.zip")
    assert opened == [tmp_path / "data.zip"]


def test_a_damaged_file_shows_its_information(ws, tmp_path):
    (tmp_path / "broken.xlsx").write_bytes(b"not a workbook")
    view = ws.view("broken.xlsx")
    assert view["kind"] == "info" and "表示できません" in view["note"]


@pytest.mark.parametrize("extension,module,loader", [
    (".xlsx", "openpyxl", "load_workbook"), (".docx", "docx", "Document"),
])
@pytest.mark.parametrize("limit", ["bytes", "entries"])
def test_office_archives_are_bounded_before_parsing(ws, tmp_path, monkeypatch, extension, module, loader, limit):
    import importlib

    def must_not_parse(*args, **kwargs):
        pytest.fail("oversized archive reached the document parser")

    monkeypatch.setattr(importlib.import_module(module), loader, must_not_parse)
    monkeypatch.setattr(viewer, "MAX_OFFICE_BYTES", 128)
    monkeypatch.setattr(viewer, "MAX_OFFICE_ENTRIES", 2)
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if limit == "bytes":
            archive.writestr("content.xml", b"x" * 129)
        else:
            for index in range(3):
                archive.writestr(f"{index}.xml", b"x")
    (tmp_path / f"large{extension}").write_bytes(data.getvalue())
    with pytest.raises(ValueError, match="expands beyond"):
        ws.view(f"large{extension}")


def test_sensitive_files_links_and_paths_outside_are_refused(ws, tmp_path):
    (tmp_path / ".env").write_text("API_KEY=secret", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}_outside.csv"
    outside.write_text("a,b", encoding="utf-8")
    os.link(outside, tmp_path / "linked.csv")
    with pytest.raises(ValueError):
        ws.view(".env")
    with pytest.raises((ValueError, PermissionError)):
        ws.view("linked.csv")
    with pytest.raises(ValueError):
        ws.view(f"../{outside.name}")


# Opening with the default application must be limited to document / media types: a long
# list of other types run code when opened (help files, add-ins, scriptlets, shortcuts,
# Office files with macros), and a deny list always misses some.
@pytest.mark.parametrize("suffix", [
    ".chm", ".xll", ".wsc", ".sct", ".msc", ".diagcab", ".settingcontent-ms", ".library-ms", ".search-ms",
    ".appref-ms", ".website", ".ins", ".isp", ".accde", ".mde", ".gadget", ".xlsm", ".docm", ".pptm", ".xlam",
    ".html", ".htm", ".svg", ".xml", ".ws", ".ps1xml", ".lnk", ".EXE", ".unknownext", "",
])
def test_only_document_and_media_types_open_with_their_default_application(ws, tmp_path, monkeypatch, suffix):
    opened = []
    monkeypatch.setattr(os, "startfile", lambda path: opened.append(path), raising=False)
    (tmp_path / f"file{suffix}").write_bytes(b"x")
    with pytest.raises(ValueError):
        ws.open_externally(f"file{suffix}")
    assert opened == []
    assert ws.view(f"file{suffix}")["external"] is False


@pytest.mark.parametrize("suffix", [".pdf", ".docx", ".xlsx", ".pptx", ".csv", ".txt", ".png", ".mp4", ".zip"])
def test_documents_and_media_open_with_their_default_application(ws, tmp_path, monkeypatch, suffix):
    opened = []
    monkeypatch.setattr(os, "startfile", lambda path: opened.append(path), raising=False)
    (tmp_path / f"file{suffix}").write_bytes(b"x")
    ws.open_externally(f"file{suffix}")
    assert opened == [tmp_path / f"file{suffix}"]


def _workbook(tmp_path, name="book.xlsx"):
    wb = Workbook()
    wb.active.append(["a", 1])
    wb.save(tmp_path / name)
    return name


# Excel / PDF / Word are parsed in a separate, memory-limited process: a file that needs too
# much memory or time ends that process only, and the preview says it cannot be shown.
@pytest.mark.skipif(os.name != "nt", reason="Job Object memory limits are Windows-only")
def test_a_parser_over_its_memory_limit_ends_only_its_own_process(ws, tmp_path, monkeypatch):
    monkeypatch.setattr(viewer, "WORKER_MEMORY", 24 * 1024 * 1024)  # far below what openpyxl needs
    view = ws.view(_workbook(tmp_path))
    assert view["kind"] == "info" and "表示できません" in view["note"]


def test_a_parser_that_takes_too_long_is_stopped(ws, tmp_path, monkeypatch):
    monkeypatch.setattr(viewer, "WORKER_TIMEOUT", 0.001)
    view = ws.view(_workbook(tmp_path))
    assert view["kind"] == "info" and "TimeoutError" in view["note"]


def test_the_parser_process_gets_no_secrets(ws, tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setenv("EXCUDO_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_leak")
    seen = {}
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        seen.update(kwargs.get("env") or {})
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    assert ws.view(_workbook(tmp_path))["kind"] == "table"
    assert seen and "EXCUDO_API_KEY" not in seen and "GITHUB_TOKEN" not in seen


def test_excel_xml_is_parsed_with_defusedxml():
    import openpyxl.xml

    assert openpyxl.xml.DEFUSEDXML is True
