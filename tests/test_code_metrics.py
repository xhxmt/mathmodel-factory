from conftest import FIXTURE

def test_code_metrics():
    from hard_metrics import collect_code_metrics
    m = collect_code_metrics(FIXTURE)
    assert m["code_files"] == 2
    assert m["code_lines"] == 8
    assert m["code_mean_lines"] == 4.0

def test_code_metrics_empty(tmp_path):
    from hard_metrics import collect_code_metrics
    m = collect_code_metrics(str(tmp_path))
    assert m["code_files"] == 0
    assert m["code_mean_lines"] is None
