import json
import subprocess
import sys
from pathlib import Path
import pytest


@pytest.mark.parametrize("installed", [False, True])
def test_registry_is_decided_once_at_import_and_works_outside_repo(tmp_path, installed):
    root = Path(__file__).resolve().parents[1]
    driver = r"""
import importlib.util, json, sys
sys.path.insert(0, sys.argv[1])
original = importlib.util.find_spec
installed = sys.argv[2] == "True"
importlib.util.find_spec = lambda name, *a, **kw: (object() if installed else None) if name == "openpyxl" else original(name, *a, **kw)
from excudo.tools import REGISTRY
from excudo.harness import Harness
harness = Harness(object(), "system")
before = list(harness._tools)
importlib.util.find_spec = lambda *a, **kw: None
assert list(harness._tools) == before
names = [name for name in REGISTRY if name.endswith("receipts") or name == "resolve_receipt"]
for name in names:
    assert REGISTRY[name].ui["title"] != name
print(json.dumps(names))
"""
    result = subprocess.run([sys.executable, "-I", "-c", driver, str(root), str(installed)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    names = json.loads(result.stdout)
    assert ("process_receipts" in names) is installed
    assert ("review_receipts" in names) is installed
    assert ("resolve_receipt" in names) is installed
    assert ("export_receipts" in names) is installed
    if not installed:
        assert names == []
