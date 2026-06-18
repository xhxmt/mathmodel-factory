import os, subprocess, sys, json
from conftest import FIXTURE, SCRIPTS

HM = os.path.join(SCRIPTS, "hard_metrics.py")

def test_render_markdown():
    from hard_metrics import collect_all, render_markdown
    md = render_markdown([collect_all(FIXTURE, "mini")])
    assert md.startswith("|")
    assert "dangling_cites" in md
    assert "| mini |" in md

def test_cli_single():
    out = subprocess.run([sys.executable, HM, FIXTURE, "mini"],
                         capture_output=True, text=True, encoding='utf-8')
    assert out.returncode == 0
    assert "dangling_cites" in out.stdout

def test_cli_batch_json(tmp_path):
    # build a tiny project tree: <parent>/proj1/proj1_paper.tex + references.bib
    proj = tmp_path / "proj1"
    proj.mkdir()
    (proj / "proj1_paper.tex").write_text(r"\cite{x} ABSTRACT_PLACEHOLDER", encoding="utf-8")
    (proj / "references.bib").write_text("@article{x, year={2020}}", encoding="utf-8")
    out = subprocess.run([sys.executable, HM, "--batch", str(tmp_path), "--json"],
                         capture_output=True, text=True, encoding='utf-8')
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert any(r["project"] == "proj1" for r in data)
