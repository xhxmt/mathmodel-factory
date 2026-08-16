from factory_core.paper_sources import (
    LatexDependencyError,
    discover_paper_dependencies,
    discover_paper_sources,
    expand_latex_document,
    primary_paper_source,
    require_safe_latex_dependencies,
    resolve_latex_dependency_graph,
    verify_latex_recorder_inputs,
)
from factory_core.bibliography import verify_bibliography_receipt
from factory_core.submission_bundle import submission_bundle_paths
from factory_core.finalization import (
    FinalizationSnapshotChanged,
    build_final_input_manifest,
    verify_final_input_snapshot,
)
from factory_core.human_decisions import decision_fingerprints
from scripts.submission_fingerprint import submission_files
from scripts.judge_packet import packet_payloads
from scripts.verify_number_chain import collect_number_chain_metrics
from scripts.verify_numbers import collect_number_metrics
from scripts.verify_symbols import collect_symbol_metrics

import pytest
import shutil
import subprocess
from pathlib import Path


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


def test_root_paper_keeps_exclusive_primary_precedence_when_both_layouts_exist(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    root_paper = project / "demo_paper.tex"
    nested_paper = project / "paper/paper.tex"
    nested_paper.parent.mkdir()
    root_paper.write_text("root\n", encoding="utf-8")
    nested_paper.write_text("nested\n", encoding="utf-8")

    assert discover_paper_sources(project, "demo") == (root_paper,)
    assert primary_paper_source(project, "demo") == root_paper


def test_recursive_latex_dependencies_are_shared_by_release_consumers(tmp_path):
    project = tmp_path / "demo"
    paper = project / "paper" / "paper.tex"
    section = project / "paper" / "sections" / "results.tex"
    bibliography = project / "paper" / "refs" / "library.bib"
    paper.parent.mkdir(parents=True)
    section.parent.mkdir(parents=True)
    bibliography.parent.mkdir(parents=True)
    paper.write_text(
        "\\documentclass{article}\n"
        "\\addbibresource{refs/library.bib}\n"
        "\\begin{document}\n"
        "\\input{sections/results}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    section.write_text("The verified value is 42.5 and $z=42.5$.\n", encoding="utf-8")
    bibliography.write_text("@article{demo, title={Demo}}\n", encoding="utf-8")
    (project / "symbol_table.md").write_text("| $x$ | value |\n", encoding="utf-8")
    logs = project / "logs"
    logs.mkdir()
    (logs / "solver.log").write_text("objective=42.5\n", encoding="utf-8")

    graph = resolve_latex_dependency_graph(project, "demo")

    assert graph.sources == (paper, section)
    assert graph.bibliographies == (bibliography,)
    assert graph.diagnostics == ()
    assert discover_paper_dependencies(project, "demo") == (
        paper,
        section,
        bibliography,
    )
    assert section in submission_files(project, "demo")
    assert bibliography in submission_files(project, "demo")
    number_metrics = collect_number_metrics(project, "demo")
    assert number_metrics is not None
    assert number_metrics["numbers_unmatched"] == 0
    assert {
        item["source"] for item in number_metrics["_paper_numbers"]
    } == {"paper/sections/results.tex"}
    symbol_metrics = collect_symbol_metrics(project, "demo")
    assert symbol_metrics is not None
    assert "z" in symbol_metrics["_undefined_list"]
    packets = packet_payloads(project, "demo")
    for role in ("paper", "math", "execution"):
        manifest = packets[role]["manifest"]
        included_paths = {
            item["path"]
            for item in manifest["files"]
            if item["status"] == "included"
        }
        assert "paper/sections/results.tex" in included_paths
        final_requirement = next(
            item
            for item in manifest["completeness"]["requirements"]
            if item["id"] == "final_paper"
        )
        assert "paper/sections/results.tex" in final_requirement["paths"]


def test_recursive_source_changes_invalidate_freeze_and_final_snapshot(tmp_path):
    project = tmp_path / "demo"
    paper = project / "paper" / "paper.tex"
    section = project / "paper" / "sections" / "conclusion.tex"
    paper.parent.mkdir(parents=True)
    section.parent.mkdir(parents=True)
    paper.write_text(
        "\\begin{document}\\input{sections/conclusion}\\end{document}\n",
        encoding="utf-8",
    )
    section.write_text("initial conclusion\n", encoding="utf-8")
    before_subject, before_options = decision_fingerprints(project, "content_freeze")
    snapshot = build_final_input_manifest(project)

    section.write_text("repaired conclusion\n", encoding="utf-8")

    after_subject, after_options = decision_fingerprints(project, "content_freeze")
    assert after_subject != before_subject
    assert after_options == before_options
    with pytest.raises(FinalizationSnapshotChanged) as raised:
        verify_final_input_snapshot(project, snapshot)
    assert raised.value.changed_paths == ["paper/sections/conclusion.tex"]


def test_unreferenced_drafts_are_excluded_and_dependency_errors_are_reported(tmp_path):
    project = tmp_path / "demo"
    paper = project / "paper" / "paper.tex"
    included = project / "paper" / "included.tex"
    draft = project / "paper" / "draft.tex"
    paper.parent.mkdir(parents=True)
    paper.write_text(
        "\\begin{document}\\input{included}\\input{missing}\\end{document}\n",
        encoding="utf-8",
    )
    included.write_text("\\input{paper}\n", encoding="utf-8")
    draft.write_text("not active\n", encoding="utf-8")

    graph = resolve_latex_dependency_graph(project, "demo")

    assert graph.sources == (paper, included)
    assert draft not in graph.files
    assert {diagnostic.code for diagnostic in graph.diagnostics} == {
        "dependency_cycle",
        "missing_dependency",
    }


def test_duplicate_dependency_names_follow_compile_contract(tmp_path):
    project = tmp_path / "demo"
    root = project / "paper/paper.tex"
    nested = project / "paper/sections/a.tex"
    compile_choice = project / "paper/shared.tex"
    wrong_choice = project / "paper/sections/shared.tex"
    nested.parent.mkdir(parents=True)
    root.write_text("\\input{sections/a}\n", encoding="utf-8")
    nested.write_text("\\input{shared}\n", encoding="utf-8")
    compile_choice.write_text("compiler choice\n", encoding="utf-8")
    wrong_choice.write_text("old parser choice\n", encoding="utf-8")

    graph = require_safe_latex_dependencies(project, "demo")

    assert graph.sources == (root, nested, compile_choice)
    assert wrong_choice not in graph.files


def test_parent_section_context_propagates_into_included_file(tmp_path):
    project = tmp_path / "demo"
    root = project / "paper/paper.tex"
    conclusion = project / "paper/sections/conclusion.tex"
    conclusion.parent.mkdir(parents=True)
    root.write_text(
        "\\begin{document}\n"
        "\\section{Conclusion}\n"
        "\\input{sections/conclusion}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    conclusion.write_text("The optimal value is 123.45.\n", encoding="utf-8")
    results = project / "results/final.json"
    results.parent.mkdir()
    results.write_text(
        '{"is_key":true,"optimal_value":123.45,"name":"objective"}\n',
        encoding="utf-8",
    )

    metrics = collect_number_chain_metrics(project, "demo")

    assert metrics["_chains"][0]["in_conclusion"] is True
    expanded = expand_latex_document(project, "demo")
    assert expanded.lines[2].source == conclusion


def test_include_filename_digits_are_not_paper_numbers(tmp_path):
    project = tmp_path / "demo"
    root = project / "paper/paper.tex"
    chapter = project / "paper/sections/chapter21.tex"
    chapter.parent.mkdir(parents=True)
    root.write_text(
        "\\begin{document}\\include{sections/chapter21}\\end{document}\n",
        encoding="utf-8",
    )
    chapter.write_text("No reported values here.\n", encoding="utf-8")

    metrics = collect_number_metrics(project, "demo")

    assert metrics is not None
    assert all(item["value"] != 21 for item in metrics["_paper_numbers"])


def test_symbol_definition_order_follows_expanded_document(tmp_path):
    project = tmp_path / "demo"
    root = project / "paper/paper.tex"
    use = project / "paper/sections/use.tex"
    notation = project / "paper/sections/notation.tex"
    use.parent.mkdir(parents=True)
    root.write_text(
        "\\begin{document}\n"
        "\\section{Model}\\input{sections/use}\n"
        "\\section{Notation}\\input{sections/notation}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    use.write_text("The objective is $x+1$.\n" + "\n" * 6, encoding="utf-8")
    notation.write_text("The symbol table follows.\n", encoding="utf-8")
    (project / "symbol_table.md").write_text("| $x$ | value |\n", encoding="utf-8")

    metrics = collect_symbol_metrics(project, "demo")

    assert metrics["_first_use"]["x"] < metrics["_table_line"]
    assert metrics["_use_before_def_list"] == ["x"]


def test_expanded_stream_cycle_fails_closed(tmp_path):
    project = tmp_path / "demo"
    root = project / "paper/paper.tex"
    child = project / "paper/child.tex"
    root.parent.mkdir(parents=True)
    root.write_text("\\input{child}\n", encoding="utf-8")
    child.write_text("\\input{paper}\n", encoding="utf-8")

    with pytest.raises(LatexDependencyError, match="dependency_cycle"):
        expand_latex_document(project, "demo")


def test_dynamic_latex_dependency_fails_content_freeze(tmp_path):
    project = tmp_path / "demo"
    root = project / "paper/paper.tex"
    root.parent.mkdir(parents=True)
    root.write_text(
        "\\newcommand{\\chapterfile}{sections/result}\n"
        "\\begin{document}\\input{\\chapterfile}\\end{document}\n",
        encoding="utf-8",
    )

    with pytest.raises(LatexDependencyError, match="dynamic_dependency"):
        decision_fingerprints(project, "content_freeze")


@pytest.mark.latex
@pytest.mark.skipif(shutil.which("pdflatex") is None, reason="pdflatex unavailable")
def test_fls_observed_inputs_match_dependency_manifest(tmp_path):
    project = tmp_path / "demo"
    root = project / "paper/paper.tex"
    nested = project / "paper/sections/a.tex"
    shared = project / "paper/shared.tex"
    nested.parent.mkdir(parents=True)
    root.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\\input{sections/a}\\end{document}\n",
        encoding="utf-8",
    )
    nested.write_text("\\input{shared}\n", encoding="utf-8")
    shared.write_text("compiler-aligned input\n", encoding="utf-8")
    repository = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [str(repository / "compile_paper.sh"), str(project), "demo"],
        cwd=repository,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    verification = __import__("json").loads(
        (project / "logs/compilation/latex_inputs.json").read_text(encoding="utf-8")
    )
    assert verification["status"] == "PASS"
    assert verification["declared_latex_inputs"] == verification["observed_latex_inputs"]
    assert verification["all_passes_share_input_identity"] is True
    assert len(verification["passes"]) == 3


@pytest.mark.latex
@pytest.mark.skipif(shutil.which("pdflatex") is None, reason="pdflatex unavailable")
def test_compiler_cannot_read_unfingerprinted_project_source(tmp_path):
    project = tmp_path / "demo"
    root = project / "paper/paper.tex"
    secret = project / "paper/secret.tex"
    root.parent.mkdir(parents=True)
    root.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\\csname input\\endcsname{secret}\\end{document}\n",
        encoding="utf-8",
    )
    secret.write_text("unfingerprinted input\n", encoding="utf-8")
    repository = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [str(repository / "compile_paper.sh"), str(project), "demo"],
        cwd=repository,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "实际读取的项目文件" in result.stderr


def test_recorder_reports_forbidden_external_input(tmp_path):
    project = tmp_path / "demo"
    root = project / "paper/paper.tex"
    root.parent.mkdir(parents=True)
    root.write_text("\\begin{document}ok\\end{document}\n", encoding="utf-8")
    fls = project / "demo_paper.fls"
    fls.write_text(
        f"INPUT {root}\nINPUT /tmp/outside-secret.txt\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="external_forbidden"):
        verify_latex_recorder_inputs(project, "demo", fls)


def test_allowed_texmf_runtime_inputs_pass(tmp_path):
    runtime = Path("/usr/share/texlive/texmf-dist/tex/latex/base/article.cls")
    if not runtime.is_file():
        pytest.skip("system TeX runtime fixture unavailable")
    project = tmp_path / "demo"
    root = project / "paper/paper.tex"
    root.parent.mkdir(parents=True)
    root.write_text("\\begin{document}ok\\end{document}\n", encoding="utf-8")
    fls = project / "demo_paper.fls"
    fls.write_text(f"INPUT {root}\nINPUT {runtime}\n", encoding="utf-8")

    verification = verify_latex_recorder_inputs(project, "demo", fls)

    assert verification["status"] == "PASS"
    assert any(
        item["category"] == "external_tex_runtime_allowed"
        for item in verification["passes"][0]["inputs"]
    )


@pytest.mark.latex
@pytest.mark.skipif(shutil.which("pdflatex") is None, reason="pdflatex unavailable")
def test_compiler_rejects_absolute_external_verbatim_input(tmp_path):
    project = tmp_path / "demo"
    root = project / "paper/paper.tex"
    root.parent.mkdir(parents=True)
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("OUTSIDE SECRET\n", encoding="utf-8")
    root.write_text(
        "\\documentclass{article}\n"
        "\\usepackage{fancyvrb}\n"
        "\\begin{document}\n"
        f"\\VerbatimInput{{{outside}}}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    repository = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [str(repository / "compile_paper.sh"), str(project), "demo"],
        cwd=repository,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0


@pytest.mark.latex
@pytest.mark.skipif(shutil.which("pdflatex") is None, reason="pdflatex unavailable")
def test_compiler_rejects_project_symlink_input(tmp_path):
    project = tmp_path / "demo"
    root = project / "paper/paper.tex"
    root.parent.mkdir(parents=True)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("OUTSIDE SECRET\n", encoding="utf-8")
    (project / "paper/leak.txt").symlink_to(outside)
    root.write_text(
        "\\documentclass{article}\n"
        "\\usepackage{fancyvrb}\n"
        "\\begin{document}\\VerbatimInput{leak.txt}\\end{document}\n",
        encoding="utf-8",
    )
    repository = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [str(repository / "compile_paper.sh"), str(project), "demo"],
        cwd=repository,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0


@pytest.mark.latex
@pytest.mark.skipif(shutil.which("bibtex") is None, reason="bibtex unavailable")
def test_stale_bbl_cannot_survive_bibtex_failure(tmp_path):
    project = tmp_path / "demo"
    root = project / "paper/paper.tex"
    bib = project / "paper/refs.bib"
    root.parent.mkdir(parents=True)
    root.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\\cite{x}"
        "\\bibliographystyle{plain}\\bibliography{refs}\\end{document}\n",
        encoding="utf-8",
    )
    bib.write_text("@article{x,title={Current},author={A},year={2026}}\n", encoding="utf-8")
    stale = project / "demo_paper.bbl"
    stale.write_text("STALE REFERENCE TITLE\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "bibtex"
    fake.write_text("#!/bin/sh\necho injected failure >&2\nexit 9\n", encoding="utf-8")
    fake.chmod(0o755)
    repository = Path(__file__).resolve().parents[1]
    env = __import__("os").environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = subprocess.run(
        [str(repository / "compile_paper.sh"), str(project), "demo"],
        cwd=repository,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode != 0
    assert "BibTeX 执行失败" in result.stderr
    assert not stale.exists()


@pytest.mark.latex
@pytest.mark.skipif(shutil.which("bibtex") is None, reason="bibtex unavailable")
def test_bibliography_receipt_matches_generated_bbl(tmp_path):
    project = tmp_path / "demo"
    root = project / "paper/paper.tex"
    bib = project / "paper/refs.bib"
    root.parent.mkdir(parents=True)
    root.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\\cite{x}"
        "\\bibliographystyle{plain}\\bibliography{refs}\\end{document}\n",
        encoding="utf-8",
    )
    bib.write_text(
        "@article{x,title={Current},author={Author},journal={Journal},year={2026}}\n",
        encoding="utf-8",
    )
    repository = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [str(repository / "compile_paper.sh"), str(project), "demo"],
        cwd=repository,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    valid, errors, receipt = verify_bibliography_receipt(project, "demo")
    assert valid, errors
    assert receipt is not None
    assert receipt["backend"] == "bibtex"
    assert receipt["generated_bbl"]["sha256"]
    (project / "demo_paper.bbl").write_text("tampered bibliography\n", encoding="utf-8")
    valid_after, errors_after, _ = verify_bibliography_receipt(project, "demo")
    assert valid_after is False
    assert any("bibliography evidence changed" in error for error in errors_after)


@pytest.mark.latex
@pytest.mark.skipif(shutil.which("biber") is None, reason="biber unavailable")
def test_addbibresource_requires_biber_success(tmp_path):
    project = tmp_path / "demo"
    root = project / "paper/paper.tex"
    bib = project / "paper/refs.bib"
    root.parent.mkdir(parents=True)
    root.write_text(
        "\\documentclass{article}\n"
        "\\usepackage[backend=biber]{biblatex}\n"
        "\\addbibresource{refs.bib}\n"
        "\\begin{document}\\cite{x}\\printbibliography\\end{document}\n",
        encoding="utf-8",
    )
    bib.write_text(
        "@article{x,title={Current},author={Author},year={2026}}\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "biber"
    fake.write_text("#!/bin/sh\necho injected biber failure >&2\nexit 12\n", encoding="utf-8")
    fake.chmod(0o755)
    repository = Path(__file__).resolve().parents[1]
    env = __import__("os").environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = subprocess.run(
        [str(repository / "compile_paper.sh"), str(project), "demo"],
        cwd=repository,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode != 0
    assert "Biber 执行失败" in result.stderr
    assert not (project / "demo_paper.bbl").exists()


@pytest.mark.latex
@pytest.mark.skipif(shutil.which("biber") is None, reason="biber unavailable")
def test_biber_receipt_matches_generated_bbl(tmp_path):
    project = tmp_path / "demo"
    root = project / "paper/paper.tex"
    bib = project / "paper/refs.bib"
    root.parent.mkdir(parents=True)
    root.write_text(
        "\\documentclass{article}\n"
        "\\usepackage[backend=biber]{biblatex}\n"
        "\\addbibresource{refs.bib}\n"
        "\\begin{document}\\cite{x}\\printbibliography\\end{document}\n",
        encoding="utf-8",
    )
    bib.write_text(
        "@article{x,title={Current},author={Author},year={2026}}\n",
        encoding="utf-8",
    )
    repository = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [str(repository / "compile_paper.sh"), str(project), "demo"],
        cwd=repository,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    valid, errors, receipt = verify_bibliography_receipt(project, "demo")
    assert valid, errors
    assert receipt is not None
    assert receipt["backend"] == "biber"
    assert receipt["control_input"]["path"].endswith("pass1.bcf")


@pytest.mark.latex
@pytest.mark.skipif(shutil.which("pdflatex") is None, reason="pdflatex unavailable")
def test_unresolved_citations_block_compilation(tmp_path):
    project = tmp_path / "demo"
    root = project / "paper/paper.tex"
    root.parent.mkdir(parents=True)
    root.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}Missing citation \\cite{missing}.\\end{document}\n",
        encoding="utf-8",
    )
    repository = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [str(repository / "compile_paper.sh"), str(project), "demo"],
        cwd=repository,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "bibliography 构建证据" in result.stderr


def test_custom_bst_is_bound_and_packaged(tmp_path):
    project = tmp_path / "demo"
    root = project / "paper/paper.tex"
    bib = project / "paper/refs.bib"
    bst = project / "paper/custom.bst"
    root.parent.mkdir(parents=True)
    root.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\\cite{x}"
        "\\bibliographystyle{custom}\\bibliography{refs}\\end{document}\n",
        encoding="utf-8",
    )
    bib.write_text("@article{x,title={X}}\n", encoding="utf-8")
    bst.write_text("ENTRY {} {} {}\nREAD\n", encoding="utf-8")

    graph = require_safe_latex_dependencies(project, "demo")

    assert graph.bibliography_styles == (bst,)
    assert bst in graph.files
    assert bst in submission_bundle_paths(project, "demo", require_pdf=False)


def test_verbatim_and_iffalse_examples_are_not_dependencies(tmp_path):
    project = tmp_path / "demo"
    root = project / "paper/paper.tex"
    root.parent.mkdir(parents=True)
    root.write_text(
        "\\begin{document}\n"
        "\\begin{verbatim}\\input{missing-example}\\end{verbatim}\n"
        "\\iffalse\\input{missing-disabled}\\fi\n"
        "\\end{document}\n",
        encoding="utf-8",
    )

    graph = require_safe_latex_dependencies(project, "demo")

    assert graph.diagnostics == ()
