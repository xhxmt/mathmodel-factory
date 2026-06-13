import os
from conftest import FIXTURE

def test_citation_metrics():
    from hard_metrics import collect_citation_metrics
    tex = os.path.join(FIXTURE, "mini_paper.tex")
    bib = os.path.join(FIXTURE, "references.bib")
    m = collect_citation_metrics(tex, bib)
    assert m["dangling_cites"] == 1
    assert m["dangling_cite_keys"] == ["c"]
    assert m["uncited_entries"] == 1
    assert m["abstract_placeholder_residue"] == 1
