"""Company expense form reused from the existing Keichan."""
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from .validation import validate_record
from .categories import category_problem, load_categories
MIN_TABLE_ROWS = 15  # blank rows keep the printed form looking like the template

_FONT = "BIZ UDPGothic"
_THIN = Side(style="thin", color="000000")
_GRID = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_UNDERLINE = Border(bottom=_THIN)
_CENTER = Alignment(horizontal="center", vertical="center")



def _rows(data: dict, categories) -> tuple[list[tuple[str, str, str, object]], list[str]]:
    rows, problems = [], []
    for day, entries in sorted(data.get("days", {}).items()):
        for r in entries:
            category = r.get('category') if not category_problem(r, categories) else '要確認'
            merchant = str(r.get("merchant", ""))
            items = '・'.join(str(item['name']) for item in r.get('items', [])
                             if isinstance(item, dict) and item.get('name'))
            remarks = merchant + (f'（{items}）' if items else '')
            checked, error = validate_record({"date": day, **r})
            amount, note = None, f"{remarks}（要確認）"
            if error:
                problems.append(f"{day} {merchant}: {error}")
            elif category == '要確認':
                problems.append(f'{day} {merchant}: 費目を確認してください')
            elif checked["currency"] != "JPY":
                note = f"{remarks}（{checked['currency']} {checked['total_amount']:,}・要確認）"
                problems.append(f"{day} {merchant}: 外貨 ({checked['currency']}) のため合計に含めていません")
            else:
                amount, note = checked["total_amount"], remarks
            if r.get("entered_by") == "manual":
                note += "（手入力）"
            rows.append((f"{day[5:7]}/{day[8:10]}", category, note, amount))
    return rows, problems

def build_form(month: str, data: dict, applicant: str, *, categories=None) -> tuple[Workbook, list[str]]:
    rows, problems = _rows(data, categories if categories is not None else load_categories())

    wb = Workbook()
    ws = wb.active
    ws.title = f"{int(month[5:7])}月"
    for col, width in zip("ABCDE", (13, 26, 26, 19, 11)):
        ws.column_dimensions[col].width = width

    # Title
    ws.merge_cells("A1:E1")
    ws["A1"] = "経費申請書"
    ws["A1"].font = Font(name=_FONT, size=24)
    ws["A1"].alignment = _CENTER
    ws.row_dimensions[1].height = 48

    ws.merge_cells("A3:C3")
    ws["A3"] = f"{int(month[5:7])}　月分"
    ws["A3"].font = Font(name=_FONT, size=16)
    ws["A3"].alignment = Alignment(horizontal="right", vertical="center", indent=6)
    ws.merge_cells("A5:C5")
    ws["A5"] = f"氏名：　{applicant}"
    ws["A5"].data_type = "s"
    ws["A5"].font = Font(name=_FONT, size=16)
    ws["A5"].alignment = Alignment(vertical="center")
    for row in (3, 5):
        ws.row_dimensions[row].height = 30
        for col in "ABC":
            ws[f"{col}{row}"].border = _UNDERLINE

    # Table header
    header_row = 7
    for col, title in zip("ABCDE", ("月日", "内容", "備考", "金額", "受領印")):
        c = ws[f"{col}{header_row}"]
        c.value = title
        c.font = Font(name=_FONT, size=12, bold=True)
        c.fill = PatternFill("solid", fgColor="D9D9D9")
        c.alignment = _CENTER
        c.border = _GRID
    ws.row_dimensions[header_row].height = 30

    # Body
    first = header_row + 1
    for i in range(max(len(rows), MIN_TABLE_ROWS)):
        r = first + i
        values = rows[i] if i < len(rows) else ("", "", "", None)
        for col, value in zip("ABCDE", (*values, "")):
            c = ws[f"{col}{r}"]
            c.value = value if value != "N/A" else None
            if isinstance(c.value, str):
                c.data_type = "s"  # text from a receipt ("=1+1") stays text, never a formula
            c.font = Font(name=_FONT, size=12)
            c.border = _GRID
            c.alignment = Alignment(horizontal="left", vertical="center", indent=1) if col == "C" else _CENTER
        ws[f"D{r}"].number_format = "#,##0"
        ws.row_dimensions[r].height = 28
    last = first + max(len(rows), MIN_TABLE_ROWS) - 1

    # Total
    total = last + 1
    ws.merge_cells(f"A{total}:C{total}")
    ws[f"A{total}"] = "合計"
    ws[f"D{total}"] = f"=SUM(D{first}:D{last})"
    ws[f"D{total}"].number_format = "#,##0"
    for col in "ABCDE":
        c = ws[f"{col}{total}"]
        c.font = Font(name=_FONT, size=12, bold=True)
        c.border = _GRID
        c.alignment = _CENTER
    ws.row_dimensions[total].height = 30

    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_area = f"A1:E{total}"
    return wb, problems
