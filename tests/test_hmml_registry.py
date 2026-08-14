import hashlib
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HMML_ROOT = REPO_ROOT / "method_library" / "hmml"


def _leaf_count(nodes) -> int:
    count = 0
    for node in nodes:
        if isinstance(node, dict) and node.get("method"):
            count += 1
        elif isinstance(node, dict) and isinstance(node.get("children"), list):
            count += _leaf_count(node["children"])
    return count


def test_authorized_hmml_source_lock_and_generated_registry_are_complete():
    source = json.loads((HMML_ROOT / "HMML.json").read_text(encoding="utf-8"))
    index = json.loads((HMML_ROOT / "index.json").read_text(encoding="utf-8"))
    lock = json.loads((HMML_ROOT / "SOURCE.json").read_text(encoding="utf-8"))

    assert _leaf_count(source) == 97
    assert len(index) == 97
    assert lock["generated_method_count"] == 97
    assert lock["source_commit"] == "8abc1300e378eb40fe85b1ffcba6820c1358610a"
    assert hashlib.sha256((HMML_ROOT / "HMML.json").read_bytes()).hexdigest() == lock["source_files"]["HMML.json"]
    assert hashlib.sha256((HMML_ROOT / "HMML.md").read_bytes()).hexdigest() == lock["source_files"]["HMML.md"]
    assert all((REPO_ROOT / entry["path"]).is_file() for entry in index)
