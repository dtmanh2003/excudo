"""Folder → extraction → duplicate check → monthly JSON and photos."""

import hashlib
from pathlib import Path
import re

from .reports import export_reports_unlocked, write_run_report
from .reader import MAX_BYTES, SUFFIXES, ReceiptReader, image_url
from .storage import checked, inside, locked, read_json, write_json, atomic_bytes
from .validation import check_record, validate_record, corrected_record, _match_key
from .categories import category_problem, load_categories
from .results import receipt_result

MONTH = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")
STATUSES = {"ok", "review", "duplicate", "excluded", "error", "retry", "superseded"}


def load_state(root):
    state = read_json(inside(root, ".index.json"), {"version": 1, "receipts": {}, "exported_months": []})
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("receipts"), dict):
        raise ValueError("保存済みデータの形式が不正です。上書きせず停止しました。")
    for month in [*state.get("exported_months", []), *state.get("saved_months", [])]:
        if not isinstance(month, str) or not MONTH.fullmatch(month):
            raise ValueError("保存済みデータの月が不正です")
    for receipt_id, entry in state["receipts"].items():
        if not re.fullmatch(r"[0-9a-f]{64}", receipt_id) or not isinstance(entry, dict) or entry.get("status") not in STATUSES:
            raise ValueError("保存済み領収書の形式が不正です")
        if not all(isinstance(entry.get(key), str) for key in ("name", "source")):
            raise ValueError("保存済み画像の情報が不正です")
        if entry.get("record") is not None and not isinstance(entry["record"], dict):
            raise ValueError("保存済みの読取結果が不正です")
        if "copy" in entry:
            if not isinstance(entry["copy"], str):
                raise ValueError("保存済み画像の情報が不正です")
            inside(root, entry["copy"])
        if entry["status"] == "ok":
            record, error = validate_record(entry.get("record") or {})
            if error:
                raise ValueError("保存済み領収書の値が不正です: " + error)
            entry["record"] = record
            inside(root, entry["image"])
    return state


def _save(root, state):
    write_json(inside(root, ".index.json"), state)


def _fingerprint(record):
    return (record["date"], _match_key(record.get("merchant")),
            str(record["total_amount"]), record["currency"])


def _archive(root, receipt_id, entry, data):
    month = entry["record"]["date"][:7]
    suffix = Path(entry["name"]).suffix.lower()
    relative = f"{month}/receipts/{receipt_id}{suffix}"
    target = inside(root, relative)
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != receipt_id:
            raise ValueError("保存済み画像の内容が変更されています")
    else:
        atomic_bytes(target, data)
    entry["image"] = relative
    entry["status"] = "ok"
    entry["problems"] = []
    _drop_copy(root, entry)


def _keep_copy(root, receipt_id, entry, data):
    """Keeps the read image under receipt_data, so a later decision never needs the original."""
    relative = f"pending/{receipt_id}{Path(entry['name']).suffix.lower()}"
    atomic_bytes(inside(root, relative), data)
    entry["copy"] = relative


def _drop_copy(root, entry):
    if entry.get("copy"):
        inside(root, entry.pop("copy")).unlink(missing_ok=True)


def _report_and_save(root, state, applicant):
    # Persist extraction first, so export failures never require another model call.
    if applicant:
        state["applicant"] = applicant.strip()[:60]
    categories = load_categories(root, create=True)
    for entry in state['receipts'].values():
        if entry['status'] == 'ok' and category_problem(entry['record'], categories):
            entry['status'] = 'review'
            entry['problems'] = ['category_requires_confirmation']
    _save(root, state)
    months = {}
    review = []
    for receipt_id, entry in state["receipts"].items():
        if entry["status"] == "ok":
            record = {**entry["record"], "id": receipt_id, "source_image": entry["image"]}
            month = record["date"][:7]
            months.setdefault(month, {}).setdefault(record["date"], []).append(record)
        elif entry["status"] in {"review", "duplicate", "error", "retry"}:
            review.append({"id": receipt_id, **entry})
    paths = []
    for month, days in sorted(months.items()):
        path = inside(root, f"{month}/records.json")
        write_json(path, {"month": month, "days": days})
        paths.append(str(path))
    for month in state.get("saved_months", []):
        if month not in months:
            inside(root, f"{month}/records.json").unlink(missing_ok=True)
    state["saved_months"] = sorted(months)
    write_json(inside(root, "review.json"), review)
    _save(root, state)
    result = {"records": paths, "review_file": str(inside(root, "review.json"))}
    result.update(export_reports_unlocked(root, state, applicant or state.get("applicant", "")))
    _save(root, state)
    return result



def _managed_image(path, root):
    try:
        first = path.resolve().relative_to(root.resolve()).parts[0]
    except (ValueError, IndexError):
        return False
    return bool(MONTH.fullmatch(first)) or first in {"exports", "pending"}


def process_folder(folder, data_dir="receipt_data", *, read=None, applicant="", expected_month=None, progress=None, stop=None, selected_paths=None, stage=None, item_done=None):
    folder = checked(Path(folder))
    if not folder.is_dir():
        raise ValueError("入力フォルダーがありません")
    if expected_month is not None and not MONTH.fullmatch(expected_month):
        raise ValueError("対象月はYYYY-MMで指定してください")
    owned_reader = None
    counts = {"found": 0, "processed": 0, "skipped": 0, "duplicate_skipped": 0, "duplicates": 0, "review": 0, "errors": []}
    with locked(Path(data_dir)) as root:
        if folder.resolve() == root.resolve():
            raise ValueError("入力と保存先は別のフォルダーを指定してください")
        state = load_state(root)
        categories = load_categories(root, create=True)
        if read is not None and isinstance(read, ReceiptReader):
            read.categories = categories
        if applicant:
            state["applicant"] = applicant.strip()[:60]
        images = sorted(p for p in folder.rglob("*") if p.suffix.lower() in SUFFIXES
                        and not _managed_image(p, root))
        if selected_paths is not None:
            selected = {str(Path(p).absolute()) for p in selected_paths}
            images = [p for p in images if str(p.absolute()) in selected]
        if len(images) > 500:
            raise ValueError("1回500画像までです。入力フォルダーを分けてください。")
        counts["found"] = len(images)
        try:
            for number, path in enumerate(images, 1):
                if stop is not None and stop.is_set():
                    counts["stopped"] = True
                    break
                if progress:
                    progress(number, len(images), path.name)
                try:
                    path = checked(path)
                    if not path.is_file():
                        raise ValueError("画像ファイルが見つかりません")
                    with path.open("rb") as handle:
                        data = handle.read(MAX_BYTES + 1)
                    if len(data) > MAX_BYTES:
                        raise ValueError("画像は20 MiB以下にしてください")
                    receipt_id = hashlib.sha256(data).hexdigest()
                    previous = state["receipts"].get(receipt_id)
                    if previous and previous["status"] not in {"retry", "error"}:
                        # Disjoint outcomes: an identical copy is not also a cached skip.
                        counts["duplicate_skipped" if previous["source"] != str(path) else "skipped"] += 1
                        if item_done:
                            item_done({"name": path.name, "status": "skipped",
                                       "reason": "duplicate" if previous["source"] != str(path) else "cached"})
                        continue
                    entry = {"name": path.name, "source": str(path), "status": "review", "problems": []}
                    try:
                        image_url(data)  # Validate pixels, limits and formats before any reader.
                        found, problems = None, []
                        for attempt in (1, 2):
                            if read is None and owned_reader is None:
                                from .. import config
                                owned_reader = ReceiptReader(config.BASE_URL, config.MODEL_NAME, config.API_KEY)
                                owned_reader.categories = categories
                            found = (read or owned_reader)(data, path.name)
                            if stop is not None and stop.is_set():
                                break
                            record, issues = check_record(found)
                            problems = [issue["code"] for issue in issues]
                            if record and category_problem(record, categories):
                                problems.append('category_requires_confirmation')
                            entry.update(record=record or (found if isinstance(found, dict) else None), reads=attempt)
                            if not problems or 'category_requires_confirmation' in problems:
                                break
                        if stop is not None and stop.is_set():
                            entry.update(status="retry", problems=["stopped_by_user"])
                            state["receipts"][receipt_id] = entry
                            counts["stopped"] = True
                            _save(root, state)
                            if item_done:
                                item_done(dict(entry))
                            break
                        month = expected_month
                        if month is None:
                            month = next((p.name for p in [path.parent, *path.parents]
                                          if MONTH.fullmatch(p.name) and (p == folder or p.is_relative_to(folder))), None)
                        if record and month and record["date"][:7] != month:
                            problems.append(f"month_mismatch: {month} → {record['date'][:7]}")
                        if any(e["source"] == str(path) and e["status"] == "ok" for e in state["receipts"].values()):
                            problems.append("changed_source")
                        if record and not problems:
                            similar = next((key for key, e in state["receipts"].items()
                                            if e["status"] == "ok" and _fingerprint(e["record"]) == _fingerprint(record)
                                            and _match_key(record.get("merchant"))), None)
                            if similar:
                                entry.update(status="duplicate", problems=[f"possible_duplicate: {similar}"])
                                _keep_copy(root, receipt_id, entry, data)
                                counts["duplicates"] += 1
                            else:
                                _archive(root, receipt_id, entry, data)
                                counts["processed"] += 1
                        else:
                            entry["problems"] = problems
                            _keep_copy(root, receipt_id, entry, data)
                            counts["review"] += 1
                    except Exception as error:
                        entry.update(status="error", problems=[str(error)[:250]])
                        counts["errors"].append(f"{path.name}: {error}")
                    state["receipts"][receipt_id] = entry
                    _save(root, state)
                    if item_done:
                        item_done(dict(entry))
                except (ValueError, OSError) as error:
                    counts["errors"].append(f"{path.name}: {error}")
                    if item_done:
                        item_done({"name": path.name, "status": "error"})
            if stage:
                stage("saving")
            counts.update(_report_and_save(root, state, applicant))
            write_json(inside(root, "last_run.json"), counts)
            write_run_report(root, counts, state=state)
        finally:
            if owned_reader is not None:
                owned_reader.close()
    return counts


def receipt_entry(state, receipt_id):
    matches = [key for key, entry in state["receipts"].items()
               if isinstance(receipt_id, str) and (key.startswith(receipt_id) or entry["name"] == receipt_id)]
    if not receipt_id or len(matches) != 1:
        raise ValueError("領収書を一意に特定できません。対象のファイル名と保存状態を確認してください。")
    key = matches[0]
    return key, state["receipts"][key]


def resolve_receipt(data_dir, receipt_id, action, *, record=None, applicant="", expected_entry=None):
    with locked(Path(data_dir)) as root:
        state = load_state(root)
        key, entry = receipt_entry(state, receipt_id)
        if expected_entry is not None and entry != expected_entry:
            raise ValueError("承認待ちの間に領収書が変更されました。内容を確認してやり直してください。")
        previous_record = receipt_result(entry)['record']
        if action == "exclude":
            entry["status"] = "excluded"
            _drop_copy(root, entry)
        elif action == "retry":
            entry["status"] = "retry"
        elif action == "keep":
            # A correction changes only the fields given (often just the category); the rest stays as read.
            # Only known field names are accepted, so a misspelled field is an error, not a silent no-op.
            candidate, error = validate_record(corrected_record(entry.get("record") or {}, record))
            if error:
                raise ValueError(error)
            if candidate.get('category') not in load_categories(root):
                raise ValueError('設定済みの費目をユーザーに確認して指定してください。ユーザーが一覧にない費目を指定した場合は、'
                                 'add_receipt_categoryで追加してから保存してください。')
            candidate['category_needs_review'] = False
            # The copy kept at reading time (pending), or the saved month copy: the original may
            # have been moved since, or be in another folder. Only old data falls back to the original.
            managed = entry.get("copy") or entry.get("image")
            path = inside(root, managed) if managed else checked(Path(entry["source"]))
            if not path.is_file():
                raise ValueError("保存用の画像が見つかりません。フォルダーを再処理してください")
            with path.open("rb") as handle:
                data = handle.read(MAX_BYTES + 1)
            if hashlib.sha256(data).hexdigest() != key:
                raise ValueError("保存用の画像が変わっています。フォルダーを再処理してください")
            image_url(data)
            previous, _ = validate_record(entry.get("record") or {})
            fields = ("date", "total_amount", "merchant", "currency")
            changed = any(candidate.get(field) != previous.get(field) for field in fields)
            candidate.pop("entered_by", None)  # Do not accept this marker from tool arguments.
            if changed or previous.get("entered_by") == "manual":
                candidate["entered_by"] = "manual"
            entry["record"] = candidate
            _archive(root, key, entry, data)
            for other_key, other in state["receipts"].items():
                if other_key != key and other["source"] == entry["source"] and other["status"] == "ok":
                    other["status"] = "superseded"
        else:
            raise ValueError("actionはkeep / exclude / retryを指定してください")
        result = _report_and_save(root, state, applicant)
        result.update(receipt=receipt_result(entry), previous_record=previous_record,
                      reread_pending=action == 'retry')
        return result



def export_reports(data_dir="receipt_data", applicant=""):
    with locked(Path(data_dir)) as root:
        return _report_and_save(root, load_state(root), applicant)
