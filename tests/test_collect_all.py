from conftest import FIXTURE


def test_collect_all_flat():
    from hard_metrics import collect_all
    row = collect_all(FIXTURE, "mini")
    assert row["project"] == "mini"
    assert row["dangling_cites"] == 1
    assert row["assumptions_total"] == 3
    assert row["protected"] == 2
    assert row["code_files"] == 2
    assert row["symbol_coverage"] is not None
    assert 0.0 <= row["symbol_coverage"] <= 1.0
    assert "numbers_unmatched" in row
