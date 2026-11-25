"""Validation reused from the existing Keichan, with no guessed currency."""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import re
import unicodedata
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_MAX_AGE = timedelta(days=400)

# The model must know canonical correction keys; otherwise a typo can look like a no-op success.
CORRECTION_PROPERTIES = {
    "date": {"type": "string", "description": "User-confirmed transaction date, YYYY-MM-DD."},
    "merchant": {"type": "string", "description": "User-confirmed merchant name."},
    "category": {"type": "string", "description": "Exact user-confirmed accounting category."},
    "currency": {"type": "string", "description": "User-confirmed three-letter currency code."},
    "total_amount": {"type": "number", "description": "Corrected total paid. Use this field for a change to the receipt amount."},
    "items": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "quantity": {"type": ["number", "string"]},
        "price": {"type": ["number", "string"]}, "line_total": {"type": ["number", "string"]}}}},
    "tax": {"type": ["number", "string", "null"]},
    "discount": {"type": ["number", "string", "null"]},
    "service_fee": {"type": ["number", "string", "null"]},
    "payment_method": {"type": "string"},
    "summary": {"type": "string"},
    "notes": {"type": "string"},
    "category_needs_review": {"type": "boolean"},
}


def corrected_record(original, changes):
    if changes is None:
        return dict(original)
    if not isinstance(changes, dict):
        raise ValueError("修正する内容の形式が不正です。")
    # Old callers may copy these read-only fields. Never use them as corrections.
    read_only = {"entered_by", "id", "source_image"}
    if set(changes) - CORRECTION_PROPERTIES.keys() - read_only:
        raise ValueError("修正項目の名前が不正です。定義済みの項目だけを使用してください（合計金額: total_amount）。")
    return {**original, **{key: value for key, value in changes.items() if key not in read_only}}


def _amount(value) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    text = value.strip().replace(",", "") if isinstance(value, str) else str(value)
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number < 0:
        return None
    return int(number) if number == number.to_integral_value() else float(number)

def validate_record(record: dict) -> tuple[dict, str]:
    record = dict(record)
    day = str(record.get("date", ""))
    try:
        if not _DATE_RE.match(day):
            raise ValueError(day)
        date.fromisoformat(day)
    except ValueError:
        return record, (
            f"エラー: 取引日が不明または不正です ({day or 'N/A'})。"
            "ユーザーに取引日 (YYYY-MM-DD) を確認してから再度保存してください。"
        )

    currency = str(record.get("currency") or "").strip().upper()
    if currency in ("", "N/A"):
        return record, "通貨を確認してください（推測でJPYにはしません）。"
    if not _CURRENCY_RE.match(currency):
        return record, (
            f"エラー: 通貨コードが不正です ({currency})。JPY / USD などの3文字で指定してください。"
        )
    record["currency"] = currency

    total = _amount(record.get("total_amount"))
    if total is None:
        return record, (
            f"エラー: 合計金額 (total_amount) が数値ではありません ({record.get('total_amount')!r})。"
            "ユーザーに金額を確認してください。"
        )
    record["total_amount"] = total
    return record, ""

def _match_key(value) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = re.sub(r"[\s・、。,.()（）「」\[\]【】/／-]", "", text)
    return "" if text in ("", "n/a", "na", "none", "null", "不明") else text

def check_record(record: dict | None, today: date | None = None) -> tuple[dict | None, list[dict]]:
    if not isinstance(record, dict):
        return None, [{"code": "unreadable"}]
    today = today or date.today()
    day = str(record.get("date", ""))
    problems = []
    try:
        if not _DATE_RE.match(day):
            raise ValueError(day)
        when = date.fromisoformat(day)
    except ValueError:
        when = None
        problems.append({"code": "no_date"})
    total = _amount(record.get("total_amount"))
    if total is None:
        problems.append({"code": "no_amount"})
    if problems:
        return None, problems
    checked, error = validate_record(record)
    if error:
        return None, [{"code": "bad_currency", "currency": str(record.get("currency"))}]
    if when > today:
        problems.append({"code": "future_date", "date": day})
    elif today - when > _MAX_AGE:
        problems.append({"code": "old_date", "date": day})
    if total == 0:
        problems.append({"code": "zero_amount"})
    tax = _amount(record.get("tax"))
    before_discount = total + (_amount(record.get("discount")) or 0)
    if tax is not None and tax > before_discount * 10 / 110 + 1:  # more than 10% tax included
        problems.append({"code": "tax_too_large", "tax": tax, "total": total})
    items = _items_total(record.get("items"))
    if items is not None and not _adds_up(items, total, record):
        problems.append({"code": "items_mismatch", "items": items, "total": total})
    return checked, problems

def _items_total(items) -> int | float | None:
    if not isinstance(items, list) or not items:
        return None
    values = []
    for item in items:
        if not isinstance(item, dict):
            return None
        value = _amount(item.get("line_total"))
        if value is None:
            price, quantity = _amount(item.get("price")), _amount(item.get("quantity"))
            if price is None or quantity is None:
                return None
            value = price * quantity
        values.append(value)
    return sum(values)

def _adds_up(items: float, total: float, record: dict) -> bool:
    adjustments = [_amount(record.get(key)) or 0 for key in ("tax", "service_fee")]
    discount = _amount(record.get("discount")) or 0
    for add_tax in (0, adjustments[0]):
        for add_fee in (0, adjustments[1]):
            for minus in (0, discount):
                if abs(items + add_tax + add_fee - minus - total) <= 1:
                    return True
    return False
