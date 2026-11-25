"""Monthly company forms and the exception list; users mainly open exports/."""

from collections import Counter, defaultdict
from decimal import Decimal
import io
import os

from openpyxl import Workbook
from openpyxl.styles import Font

from .form import build_form
from .storage import atomic_bytes, inside, read_json, write_json
from .categories import load_categories
from .results import ReceiptResults


def _text_row(sheet, values):
    sheet.append(values)
    for cell in sheet[sheet.max_row]:
        if isinstance(cell.value, str):
            cell.data_type = "s"


def _save(book, path):
    stream = io.BytesIO()
    try:
        book.save(stream)
        atomic_bytes(path, stream.getvalue())
    finally:
        book.close()


def write_run_report(root, result=None, *, state):
    book = Workbook()
    sheet = book.active
    sheet.title = "現在の状態"
    counts = Counter(entry["status"] for entry in state["receipts"].values())
    _text_row(sheet, ["対象領収書（重複画像を除く）", len(state["receipts"])])
    for status, label in (("ok", "保存済み"), ("duplicate", "重複の疑い（要確認）"),
                          ("review", "その他の要確認"), ("error", "読み取りエラー"),
                          ("retry", "再読み取り待ち"), ("excluded", "除外済み"),
                          ("superseded", "差し替え済み")):
        _text_row(sheet, [label, counts[status]])
    sheet.column_dimensions["A"].width = 90
    # Retain batch accounting, but clearly distinguish it from the current state.
    result = result if result is not None else read_json(inside(root, "last_run.json"), {})
    if result:
        sheet = book.create_sheet("直近の読み取り時点")
        _batch_rows(sheet, result)
    _save(book, inside(root, "exports/処理結果.xlsx"))


def _batch_rows(sheet, result):
    for key, label in (("found", "対象画像（合計）"), ("processed", "保存済み"),
                       ("skipped", "処理済みのためスキップ"), ("duplicate_skipped", "重複のためスキップ"),
                       ("duplicates", "重複の疑い（要確認）"), ("review", "その他の要確認")):
        _text_row(sheet, [label, result[key]])
    _text_row(sheet, ["読み取りエラー", len(result["errors"])])
    for error in result["errors"]:
        _text_row(sheet, [error])
    sheet.column_dimensions["A"].width = 90


def export_reports_unlocked(root, state, applicant=""):
    categories = load_categories(root)
    months = defaultdict(list)
    review = []
    for receipt_id, entry in state["receipts"].items():
        if entry["status"] == "ok":
            months[entry["record"]["date"][:7]].append((receipt_id, entry))
        elif entry["status"] in {"review", "duplicate", "error", "retry"}:
            review.append({"id": receipt_id, **entry})
    forms = []
    included = ReceiptResults()
    for month, entries in sorted(months.items()):
        entries.sort(key=lambda pair: (pair[1]["record"]["date"], pair[0]))
        data = {"month": month, "days": {}}
        for receipt_id, entry in entries:
            record = {**entry["record"], "id": receipt_id, "source_image": entry["image"]}
            data["days"].setdefault(record["date"], []).append(record)
        write_json(inside(root, f"{month}/records.json"), data)
        if not applicant.strip():
            continue  # Keep extracted data, but never generate a form with a blank name.
        book, _ = build_form(month, data, applicant, categories=categories)
        sheet = book.create_sheet("明細")
        _text_row(sheet, ["日付", "店舗", "内容", "金額", "通貨", "画像"])
        totals = defaultdict(Decimal)
        for receipt_id, entry in entries:
            record = entry["record"]
            included.add(entry)
            _text_row(sheet, [record["date"], str(record.get("merchant", "N/A")),
                             record['category'], record["total_amount"], record["currency"], entry["name"]])
            target = inside(root, entry["image"])
            sheet.cell(sheet.max_row, 6).hyperlink = os.path.relpath(target, root / "exports").replace("\\", "/")
            sheet.cell(sheet.max_row, 6).font = Font(color="0563C1", underline="single")
            totals[record["currency"]] += Decimal(str(record["total_amount"]))
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for col, width in zip("ABCDEF", [14, 30, 22, 16, 10, 35]):
            sheet.column_dimensions[col].width = width
        summary = book.create_sheet("通貨別合計")
        _text_row(summary, ["通貨", "合計"])
        for currency, amount in sorted(totals.items()):
            _text_row(summary, [currency, float(amount)])
        out = inside(root, f"exports/経費申請書_{month}.xlsx")
        _save(book, out)
        forms.append(str(out))
    # If the last receipt of a month was excluded/corrected, remove its old report.
    for month in state.get("exported_months", []):
        if month not in months:
            inside(root, f"{month}/records.json").unlink(missing_ok=True)
            inside(root, f"exports/経費申請書_{month}.xlsx").unlink(missing_ok=True)
    write_json(inside(root, "review.json"), review)
    review_path = inside(root, "exports/要確認.xlsx")
    if review:
        book = Workbook()
        sheet = book.active
        sheet.title = "要確認"
        _text_row(sheet, ["画像", "状態", "確認事項", "読取日付", "店舗", "金額", "通貨"])
        for entry in review:
            record = entry.get("record") or {}
            _text_row(sheet, [entry["name"], entry["status"], "; ".join(entry.get("problems", [])),
                             str(record.get("date", "")), str(record.get("merchant", "")),
                             str(record.get("total_amount", "")), str(record.get("currency", ""))])
            sheet.cell(sheet.max_row, 1).hyperlink = entry["source"]
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for col, width in zip("ABCDEFG", [35, 16, 60, 16, 30, 18, 10]):
            sheet.column_dimensions[col].width = width
        _save(book, review_path)
    else:
        review_path.unlink(missing_ok=True)
    state["exported_months"] = sorted(months)
    write_run_report(root, state=state)
    saved_count = sum(len(entries) for entries in months.values())
    excluded_count = sum(entry["status"] == "excluded" for entry in state["receipts"].values())
    review_count = len(review)
    pending = ReceiptResults(max_chars=2000)
    for entry in review:
        pending.add(entry, include_record=False)
    needs_applicant = bool(months) and not applicant.strip()
    if forms:
        message = f"経費申請書を{len(forms)}件作成しました。"
        if review_count:
            message += f"要確認が{review_count}件あり、これらは申請書に含まれていません。"
    else:
        reasons = []
        if needs_applicant:
            reasons.append("申請者の氏名が未入力")
        if not months:
            reasons.append("申請書に計上できる保存済みの領収書がない")
        message = "・".join(reasons) + "ため、経費申請書を作成できませんでした。"
        if review_count:
            message += f"要確認が{review_count}件あるため、これらの領収書は申請書に含めていません。"
    message += f"現在の保存済みは{saved_count}件、要確認は{review_count}件、除外済みは{excluded_count}件です。"
    return {"forms": forms, "export_status": 'created' if forms else 'needs_applicant' if needs_applicant else 'no_saved_receipts',
            "included_receipts": included.items, "included_receipts_omitted": included.omitted,
            "pending_receipts": pending.items, "pending_receipts_omitted": pending.omitted,
            "available_categories": categories,
            "review_file": str(review_path) if review else None,
            "needs_applicant": needs_applicant, "saved_count": saved_count,
            "review_count": review_count, "excluded_count": excluded_count, "message": message}
