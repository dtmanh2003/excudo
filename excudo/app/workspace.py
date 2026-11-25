"""Local editor and persistent daily change journal."""
from contextlib import contextmanager
from datetime import datetime
import difflib
import hashlib
import json
from pathlib import Path
import threading
import uuid
import base64
import io
import os

from PIL import Image, ImageOps

from ..security import is_sensitive_path
from ..studio_storage import atomic_write_bytes, atomic_write_text, create_bytes_checked, file_lock, open_checked, read_text_checked

LIMIT = 1024 * 1024
# Largest file the プレビュー view reads (audio / video are sent to the page whole).
VIEW_LIMIT = 50 * 1024 * 1024
MEDIA_LIMIT = 30 * 1024 * 1024
EXCLUDED = {'.git', '.venv', '.excudo', '__pycache__', 'node_modules', '.aws', '.codex', '.agents', '.pytest_cache', '.mypy_cache', '.ruff_cache', 'keichan_data'}


# The project folder as seen by the editor: safe path resolution, file listing, reading/saving with
# conflict detection, and the change journal (.excudo/changes.json).
class Workspace:
    # Remember the project root, a lock, and the location of the change journal.
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.lock = threading.RLock()
        self.journal = self.root / '.excudo' / 'changes.json'
        self._journal_depth = 0

    @contextmanager
    def _journal_guard(self):
        """Serialize read/modify/write across instances and processes; allow nesting."""
        with self.lock:
            if self._journal_depth:
                yield
                return
            with file_lock(self.journal, root=self.root):
                self._journal_depth = 1
                try:
                    yield
                finally:
                    self._journal_depth = 0

    # Resolve a name inside the project; reject internal folders and sensitive files.
    def path(self, name):
        path = (self.root / name).resolve()
        relative = path.relative_to(self.root)
        if any(p.lower() in EXCLUDED for p in relative.parts) or is_sensitive_path(str(path), self.root):
            raise ValueError('このファイルはエディタの対象外です。')
        return path

    # List project files (max 2000), including large files and images. The editor's size
    # limit is enforced by read(), not by discovery. Skip links and sensitive paths.
    def files(self):
        result = []
        # Recursively collect files of one directory, folders first.
        def walk(directory):
            for item in sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                if len(result) >= 2000:
                    return
                if item.is_symlink() or item.name.lower() in EXCLUDED:
                    continue
                try:
                    self.path(str(item))
                    if item.is_dir():
                        walk(item)
                    elif item.is_file():
                        result.append(item.relative_to(self.root).as_posix())
                except (OSError, ValueError):
                    continue
        walk(self.root)
        return result

    # Read a UTF-8 text file (at most 1 MB, no binary) and return its content and a SHA-256
    # revision.
    def read(self, name):
        path = self.path(name)
        if path.stat().st_size > LIMIT:
            raise ValueError('1 MBを超えるファイルは開けません。')
        with open_checked(path, 'rb', root=self.root) as stream:
            raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError('1 MBを超えるファイルは開けません。')
        content = raw.decode('utf-8')
        if '\x00' in content:
            raise ValueError('バイナリファイルは開けません。')
        return {'path': path.relative_to(self.root).as_posix(), 'content': content,
                'revision': hashlib.sha256(raw).hexdigest()}

    # A read-only preview of any project file for the プレビュー view: a table (Excel, CSV),
    # pages (PDF), a document (Word), an image, audio / video, text, or only the file's
    # information. Read through the shared check, bounded, never sent to the AI.
    def view(self, name):
        from . import viewer

        path = self.path(name)
        if not path.is_file():
            raise ValueError('ファイルが見つかりません。')
        suffix = path.suffix.lower()
        # Formats that need the whole file get it up to their limit; text-like files are shown
        # from their first MAX_TEXT bytes.
        whole = {**dict.fromkeys(viewer.EXCEL, VIEW_LIMIT), '.pdf': VIEW_LIMIT, '.docx': VIEW_LIMIT,
                 '.svg': 2 * 1024 * 1024, **dict.fromkeys(viewer.MEDIA, MEDIA_LIMIT),
                 **dict.fromkeys(viewer.IMAGES, VIEW_LIMIT)}
        with open_checked(path, 'rb', root=self.root) as stream:
            stat = os.fstat(stream.fileno())
            if suffix in whole:
                data = stream.read(whole[suffix] + 1) if stat.st_size <= whole[suffix] else b''
            else:
                data = stream.read(viewer.MAX_TEXT + 1)
        info = {'path': path.relative_to(self.root).as_posix(), 'size': stat.st_size,
                'modified': datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec='minutes'),
                'external': suffix in viewer.OPEN_EXTERNALLY}
        if not data and stat.st_size:
            return {**info, **viewer.info_only(), 'note': 'ファイルが大きすぎるため、内容は表示できません。'}
        try:
            if suffix in viewer.IMAGES:
                return {**info, 'kind': 'image', **self.image(name)}
            if suffix == '.svg':
                # Shown as an <img>: scripts inside an SVG do not run there.
                return {**info, 'kind': 'image', 'data_url': 'data:image/svg+xml;base64,' + base64.b64encode(data).decode('ascii')}
            if suffix in viewer.EXCEL:
                return {**info, **viewer.isolated('excel', data)}
            if suffix in viewer.TABLES:
                return {**info, **viewer.table(data, '\t' if suffix == '.tsv' else ',')}
            if suffix == '.pdf':
                return {**info, **viewer.isolated('pdf', data)}
            if suffix == '.docx':
                return {**info, **viewer.isolated('word', data)}
            if suffix in viewer.MEDIA:
                return {**info, **viewer.media(data, viewer.MEDIA[suffix])}
            return {**info, **viewer.text(data, stat.st_size)}
        except ValueError:
            raise
        except Exception as error:  # a damaged or unusual file: show its information instead
            return {**info, **viewer.info_only(), 'note': f'内容を表示できませんでした（{type(error).__name__}）。'}

    # Open a project file with its default Windows application: only the document / media types
    # of viewer.OPEN_EXTERNALLY (never programs, scripts or files with macros).
    def open_externally(self, name):
        from . import viewer

        path = self.path(name)
        if not path.is_file() or path.suffix.lower() not in viewer.OPEN_EXTERNALLY:
            raise ValueError('このファイルは既定のアプリで開けません（文書・画像・音声・動画・zip のみ。'
                             'プログラム、スクリプト、マクロ付きの Office ファイルなどは開きません）。')
        with open_checked(path, 'rb', root=self.root):
            pass  # the shared check: no link, no hard link
        os.startfile(path)  # noqa: S606 — the user's click, on a checked file of the project
        return {'path': path.relative_to(self.root).as_posix()}

    # Return a bounded JPEG preview without exposing a local path or sending data to AI.
    def image(self, name):
        path = self.path(name)
        if path.suffix.lower() not in {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff', '.gif'}:
            raise ValueError('対応していない画像形式です。')
        with open_checked(path, 'rb', root=self.root) as stream:
            if os.fstat(stream.fileno()).st_size > 20 * 1024 * 1024:
                raise ValueError('20 MBを超える画像は開けません。')
            with Image.open(stream) as image:
                if image.width * image.height > 20_000_000:
                    raise ValueError('画像の解像度が大きすぎます。')
                width, height = image.size
                image = ImageOps.exif_transpose(image).convert('RGB')
                image.thumbnail((1600, 1600))
                output = io.BytesIO()
                image.save(output, 'JPEG', quality=85)
        return {'path': path.relative_to(self.root).as_posix(), 'width': width, 'height': height,
                'data_url': 'data:image/jpeg;base64,' + base64.b64encode(output.getvalue()).decode('ascii')}

    # Load the change journal, or an empty one (version 0.1.0).
    def data(self):
        with self._journal_guard():
            return self._read_data()

    def _read_data(self):
        if self.journal.exists():
            data = json.loads(read_text_checked(self.journal, root=self.root))
            changed = False
            for entry in data['entries']:
                if 'id' not in entry:
                    entry['id'] = uuid.uuid4().hex
                    changed = True
            if changed:
                self.persist(data)
            return data
        return {'version': '0.1.0', 'entries': [], 'releases': []}

    # Write the change journal atomically (temporary file, then replace).
    def persist(self, data):
        with self._journal_guard():
            atomic_write_text(self.journal, json.dumps(data, ensure_ascii=False, indent=2), root=self.root)

    # Append a change entry (time, path, source, summary, unified diff) to the journal; unchanged
    # content is ignored.
    def record(self, name, before, after, source, created=False):
        if before == after and not created:
            return
        with self._journal_guard():
            data = self.data()
            diff = ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                               fromfile=name + ' (before)', tofile=name + ' (after)'))
            data['entries'].append({'id': uuid.uuid4().hex, 'time': datetime.now().astimezone().isoformat(), 'path': name,
                                    'source': source, 'summary': f'{name} を新規作成' if created else f'{name} を更新', 'diff': diff,
                                    'verification': '未確認'})
            self.persist(data)

    # Save new content if the file still matches the revision the editor opened, then record the
    # change.
    def save(self, name, content, revision):
        with self.lock:
            current = self.read(name)
            if current['revision'] != revision:
                raise ValueError('ファイルが外部で更新されました。再読み込みして変更を確認してください。')
            if len(content.encode('utf-8')) > LIMIT or '\x00' in content:
                raise ValueError('UTF-8のテキスト（1 MB以下）を保存してください。')
            # Exact bytes, via temp file + replace: never writes into a hard-linked file.
            atomic_write_bytes(self.path(name), content.encode('utf-8'))
            self.record(current['path'], current['content'], content, '手動保存')
            return self.read(name)

    # Validate a new file name (non-empty, not existing, Windows-safe) and return an empty draft.
    def draft(self, name):
        if not isinstance(name, str) or not name.strip() or name.endswith(('/', '\\')):
            raise ValueError('ファイル名を入力してください。例: excudo/new_tool.py')
        path = self.path(name.strip())
        if path.exists():
            raise ValueError('同名のファイルが存在します。候補から開いてください。')
        if any(any(c in part for c in '<>:"|?*') or part.endswith((' ', '.'))
               or part.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *{f'COM{i}' for i in range(1,10)}, *{f'LPT{i}' for i in range(1,10)}}
               for part in path.relative_to(self.root).parts):
            raise ValueError('使用できないファイル名です。')
        return {'path': path.relative_to(self.root).as_posix(), 'content': '', 'revision': None, 'draft': True}

    # Create a new file exclusively (never overwrites), creating parent folders, and record it.
    def create(self, name, content):
        with self.lock:
            draft = self.draft(name)
            if len(content.encode('utf-8')) > LIMIT or '\x00' in content:
                raise ValueError('UTF-8のテキスト（1 MB以下）を保存してください。')
            path = self.path(draft['path'])
            try:
                create_bytes_checked(path, content.encode('utf-8'), root=self.root)
            except FileExistsError:
                raise ValueError('同名のファイルが作成されました。上書きせずに停止しました。') from None
            self.record(draft['path'], '', content, '手動保存', created=True)
            return self.read(draft['path'])

    # Return the change journal plus today's date and today's entry count.
    def report(self):
        with self._journal_guard():
            data = self.data()
            today = datetime.now().astimezone().date().isoformat()
            return {**data, 'today': today, 'today_count': sum(e['time'][:10] == today for e in data['entries']),
                    'undo_count': len(data.get('report_undo', [])),
                    'deleted_entries': [item['entry'] for group in reversed(data.get('report_undo', [])) for item in reversed(group)]}

    # Record a release marker with a newer major.minor.patch version.
    def release(self, version):
        import re
        if not re.fullmatch(r'\d+\.\d+\.\d+', version):
            raise ValueError('バージョンは 0.2.0 の形式で入力してください。')
        with self._journal_guard():
            data = self.data()
            if tuple(map(int, version.split('.'))) <= tuple(map(int, data['version'].split('.'))):
                raise ValueError('現在より新しいバージョンを指定してください。')
            data['version'] = version
            data['releases'].append({'version': version, 'time': datetime.now().astimezone().isoformat(),
                                     'entry_count': len(data['entries'])})
            self.persist(data)
            return self.report()

    def clear_report(self):
        """Remove change entries without touching project files or releases."""
        with self._journal_guard():
            data = self.data()
            if data['entries']:
                data.setdefault('report_undo', []).append([{'index': index, 'entry': entry} for index, entry in enumerate(data['entries'])])
            data['entries'] = []
            self.persist(data)
            return self.report()

    def delete_report_entry(self, entry_id):
        with self._journal_guard():
            data = self.data()
            index = next((i for i, entry in enumerate(data['entries']) if entry['id'] == entry_id), None)
            if index is None:
                raise ValueError('この変更記録は既に消去されています。再読み込みしてください。')
            entry = data['entries'].pop(index)
            data.setdefault('report_undo', []).append([{'index': index, 'entry': entry}])
            self.persist(data)
            return self.report()

    def undo_report_deletion(self, entry_id=None, restore_all=False):
        with self._journal_guard():
            data = self.data()
            undo = data.get('report_undo', [])
            if not undo:
                raise ValueError('戻せる消去操作はありません。')
            if restore_all:
                restored = [item for group in undo for item in group]
                undo.clear()
            elif entry_id is None:
                restored = undo.pop()
            else:
                found = next(((group, item) for group in undo for item in group if item['entry']['id'] == entry_id), None)
                if found is None:
                    raise ValueError('この記録は既に復元されています。再読み込みしてください。')
                group, item = found
                restored = [item]
                group.remove(item)
                if not group:
                    undo.remove(group)
            existing = {entry['id'] for entry in data['entries']}
            for item in sorted(restored, key=lambda item: item['index']):
                if item['entry']['id'] not in existing:
                    data['entries'].insert(min(item['index'], len(data['entries'])), item['entry'])
                    existing.add(item['entry']['id'])
            data['entries'].sort(key=lambda entry: entry['time'])
            self.persist(data)
            return self.report()
