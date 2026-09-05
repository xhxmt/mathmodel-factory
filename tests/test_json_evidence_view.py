import json

import pytest

from scripts.json_evidence_view import build_view, canonical_bytes, verify_view
from scripts.judge_packet import _render_context


def test_view_preserves_scientific_values_and_binds_full_source(tmp_path):
    source = tmp_path / "values.json"
    value = {"estimate": 1.25, "interval": [1.0, 2.0], "predictions": [0.1] * 1000}
    source.write_text(json.dumps(value))
    view = build_view(tmp_path, "values.json", {"/predictions": "full curve not reviewed"})
    assert view["data"]["estimate"] == 1.25
    assert view["data"]["interval"] == [1.0, 2.0]
    assert view["data"]["predictions"]["count"] == 1000
    assert verify_view(tmp_path, canonical_bytes(view))["referenced_arrays"] == ["/predictions"]
    value["predictions"][500] = 0.2
    source.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="differs"):
        verify_view(tmp_path, canonical_bytes(view))


@pytest.mark.parametrize("pointer", ["/absent", "/estimate", "/object", "/booleans"])
def test_view_cannot_drop_claim_scalars_objects_or_unknown_paths(tmp_path, pointer):
    (tmp_path / "v.json").write_text(json.dumps({
        "estimate": 2.5, "object": {"reliable": False}, "booleans": [True],
    }))
    with pytest.raises(ValueError):
        build_view(tmp_path, "v.json", {pointer: "not a valid array"})


@pytest.mark.parametrize("change", ["scalar", "source", "schema"])
def test_invalid_view_is_not_included_in_packet(tmp_path, change):
    (tmp_path / "v.json").write_text('{"estimate":1.5,"curve":[1,2]}')
    view = build_view(tmp_path, "v.json", {"/curve": "curve not read"})
    if change == "scalar":
        view["data"]["estimate"] = 99
    elif change == "source":
        (tmp_path / "v.json").write_text('{"estimate":1.6,"curve":[1,2]}')
    else:
        view["schema"] = "invented"
    path = tmp_path / "claims.evidence-view.json"
    path.write_bytes(canonical_bytes(view))
    context, files = _render_context(tmp_path, "execution", [path], [
        {"id": "claim", "paths": [path.name]},
    ])
    assert files[0]["status"] == "omitted"
    assert files[0]["reason"] == "invalid_structured_evidence"
    assert "estimate" not in context


def test_view_discloses_array_limitations_in_packet(tmp_path):
    (tmp_path / "v.json").write_text('{"estimate":1.5,"curve":[1,2]}')
    path = tmp_path / "claims.evidence-view.json"
    path.write_bytes(canonical_bytes(build_view(tmp_path, "v.json", {"/curve": "curve not read"})))
    context, files = _render_context(tmp_path, "execution", [path], [
        {"id": "claim", "paths": [path.name]},
    ])
    assert files[0]["status"] == "included"
    assert files[0]["structured_evidence"]["limitations"] == {"/curve": "curve not read"}
    assert "curve not read" in context


@pytest.mark.parametrize("relative", ["../v.json", "/v.json", "./v.json", "a/../v.json"])
def test_view_rejects_noncanonical_source_paths(tmp_path, relative):
    with pytest.raises(ValueError):
        build_view(tmp_path, relative, {})


def test_view_rejects_source_alias_and_duplicate_keys(tmp_path):
    (tmp_path / "v.json").write_text('{"x":1,"x":2}')
    with pytest.raises(ValueError, match="duplicate"):
        build_view(tmp_path, "v.json", {})
    (tmp_path / "alias.json").symlink_to(tmp_path / "v.json")
    with pytest.raises(ValueError, match="symlink"):
        build_view(tmp_path, "alias.json", {})
