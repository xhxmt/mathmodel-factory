from factory_core.paper_sources import (
    discover_paper_sources,
    primary_paper_source,
)
from scripts.verify_number_chain import collect_number_chain_metrics
from scripts.verify_numbers import collect_number_metrics
from scripts.verify_symbols import collect_symbol_metrics


def test_nested_paper_is_shared_by_active_structural_checkers(tmp_path):
    project = tmp_path / "demo"
    paper = project / "paper/paper.tex"
    paper.parent.mkdir(parents=True)
    paper.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\section{Notation} Let $x=1$.\n"
        "\\section{Conclusion} The result is 1.\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    (project / "symbol_table.md").write_text("| $x$ | value |\n", encoding="utf-8")

    assert discover_paper_sources(project, "demo") == (paper,)
    assert primary_paper_source(project, "demo") == paper
    assert collect_number_metrics(project, "demo") is not None
    assert collect_symbol_metrics(project, "demo") is not None
    assert collect_number_chain_metrics(project, "demo") is not None


def test_root_paper_keeps_primary_precedence_when_both_layouts_exist(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    root_paper = project / "demo_paper.tex"
    nested_paper = project / "paper/paper.tex"
    nested_paper.parent.mkdir()
    root_paper.write_text("root\n", encoding="utf-8")
    nested_paper.write_text("nested\n", encoding="utf-8")

    assert discover_paper_sources(project, "demo") == (root_paper, nested_paper)
    assert primary_paper_source(project, "demo") == root_paper
