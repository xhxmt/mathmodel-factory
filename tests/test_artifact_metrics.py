import os, zipfile

def test_artifact_metrics(tmp_path):
    from hard_metrics import collect_artifact_metrics
    base = "demo"
    (tmp_path / f"{base}_paper.pdf").write_bytes(b"%PDF-1.4 fake")
    zpath = tmp_path / f"{base}_submission.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("a.txt", "hello")
    m = collect_artifact_metrics(str(tmp_path), base)
    assert m["pdf_ok"] is True
    assert m["zip_ok"] is True
    assert "pdf_pages" in m   # 可能为 None（pdfinfo 读不了假 pdf）

def test_artifact_metrics_missing(tmp_path):
    from hard_metrics import collect_artifact_metrics
    m = collect_artifact_metrics(str(tmp_path), "nope")
    assert m["pdf_ok"] is False
    assert m["zip_ok"] is False
    assert m["pdf_pages"] is None
