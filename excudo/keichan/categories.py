"""User-editable accounting categories; receipt descriptions are not account names."""
from pathlib import Path
import re

from .storage import atomic_bytes, inside, locked


def load_categories(root=None, *, create=False):
    default = Path(__file__).with_name('default_categories.txt')
    path = inside(root, 'categories.txt') if root is not None else default
    if not path.exists():
        data = default.read_bytes()
        if create:
            atomic_bytes(path, data)
    else:
        with path.open('rb') as stream:
            data = stream.read(16_385)
    if len(data) > 16_384:
        raise ValueError('費目の設定は16 KiB以下にしてください。')
    values = list(dict.fromkeys(line.strip() for line in data.decode('utf-8-sig').splitlines() if line.strip()))
    if not values or len(values) > 100 or any(len(value) > 80 for value in values):
        raise ValueError('費目は1行に1件、各80文字以下で1〜100件指定してください。')
    return tuple(values)


def clean_category(name):
    """The category as it is saved: one line, at most 80 characters, not a placeholder."""
    value = ' '.join(str(name or '').split())
    if not value or len(value) > 80 or re.search(r'[\x00-\x1f\x7f]', value) or value.upper() == 'N/A':
        raise ValueError('費目は1〜80文字の1行で指定してください。')
    return value


def add_category(root, name):
    """Adds a category the user named to categories.txt; returns (category, added, all categories)."""
    value = clean_category(name)
    with locked(Path(root)) as folder:
        categories = load_categories(folder, create=True)
        if value in categories:
            return value, False, categories
        if len(categories) >= 100:
            raise ValueError('費目は100件までです。categories.txtを整理してください。')
        categories = (*categories, value)
        atomic_bytes(inside(folder, 'categories.txt'), ('\n'.join(categories) + '\n').encode('utf-8'))
        return value, True, categories


def category_problem(record, categories):
    if record.get('category') not in categories or record.get('category_needs_review', False) is not False:
        return 'category_requires_confirmation'
    return None
