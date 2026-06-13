import os
from conftest import FIXTURE

def test_assumption_metrics():
    from hard_metrics import collect_assumption_metrics
    m = collect_assumption_metrics(os.path.join(FIXTURE, "assumption_ledger.md"))
    assert m["assumptions_total"] == 3
    assert m["protected"] == 2
    assert m["critical"] == 1

def test_assumption_metrics_missing():
    from hard_metrics import collect_assumption_metrics
    assert collect_assumption_metrics("/no/such/ledger.md") is None
