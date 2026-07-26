from pathlib import Path

import pytest

from web.backend.config import Settings
from web.backend.showcase import ShowcasePaperNotFound, list_showcase_papers, resolve_showcase_paper


def make_settings(tmp_path: Path, projects: tuple[str, ...]) -> Settings:
    return Settings(
        jwt_secret="x" * 32,
        admin_password="strong test password",
        factory_root=tmp_path,
        showcase_projects=projects,
    )


def write_paper(root: Path, base_name: str, content: bytes = b"%PDF-1.4\n") -> Path:
    project = root / "complete" / base_name
    project.mkdir(parents=True)
    paper = project / f"{base_name}_paper.pdf"
    paper.write_bytes(content)
    return paper


def test_showcase_lists_only_configured_completed_papers(tmp_path):
    expected = write_paper(tmp_path, "cumcm_2025_a", b"%PDF-demo-a")
    write_paper(tmp_path, "private_project", b"%PDF-private")
    settings = make_settings(tmp_path, ("cumcm_2025_a", "missing", "cumcm_2025_a"))

    papers = list_showcase_papers(settings)

    assert len(papers) == 1
    assert papers[0].base_name == "cumcm_2025_a"
    assert papers[0].title == "2025 全国大学生数学建模竞赛 A 题论文"
    assert papers[0].size_bytes == expected.stat().st_size
    assert papers[0].pdf_url == "/api/showcase/papers/cumcm_2025_a/pdf"


def test_showcase_rejects_unlisted_and_path_escaping_papers(tmp_path):
    write_paper(tmp_path, "public")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-secret")
    escaped_project = tmp_path / "complete" / "escaped"
    escaped_project.mkdir(parents=True)
    (escaped_project / "escaped_paper.pdf").symlink_to(outside)
    settings = make_settings(tmp_path, ("public", "escaped", "../outside"))

    assert [paper.base_name for paper in list_showcase_papers(settings)] == ["public"]
    assert resolve_showcase_paper(settings, "public").name == "public_paper.pdf"

    with pytest.raises(ShowcasePaperNotFound):
        resolve_showcase_paper(settings, "private")

    with pytest.raises(ShowcasePaperNotFound):
        resolve_showcase_paper(settings, "escaped")
