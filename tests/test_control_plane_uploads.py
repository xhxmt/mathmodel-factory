import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from web.backend.upload_service import ArchiveTraversalError, extract_archive, find_problem_file
from web.backend.project_api import _safe_upload_filename


def test_extract_archive_rejects_zip_traversal(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.md", "boom")

    with pytest.raises(ArchiveTraversalError):
        extract_archive(archive, tmp_path / "out")


def test_extract_archive_rejects_tar_traversal(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        info = tarfile.TarInfo("../escape.pdf")
        data = b"boom"
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))

    with pytest.raises(ArchiveTraversalError):
        extract_archive(archive, tmp_path / "out")


def test_find_problem_file_prefers_problem_named_pdf(tmp_path):
    (tmp_path / "附件").mkdir()
    (tmp_path / "题目_problem.pdf").write_text("pdf", encoding="utf-8")
    (tmp_path / "附件" / "notes.md").write_text("md", encoding="utf-8")

    found = find_problem_file(tmp_path)

    assert found.name == "题目_problem.pdf"


def test_upload_filename_is_reduced_to_safe_basename():
    assert _safe_upload_filename("../escape.pdf") == "escape.pdf"
    assert _safe_upload_filename("nested/../../problem.md") == "problem.md"


def test_upload_filename_rejects_empty_basename():
    with pytest.raises(ValueError):
        _safe_upload_filename("../")
