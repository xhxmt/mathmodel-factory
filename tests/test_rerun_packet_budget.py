from scripts import judge_packet


def test_duplicate_logs_share_one_chunk_and_keep_aliases(tmp_path):
    paths = []
    for name in ("a", "b", "c", "d"):
        path = tmp_path / f"{name}.log"
        path.write_text("identical evidence\n" * 4000)
        paths.append(path)
    context, files = judge_packet._render_context(tmp_path, "execution", paths, [])
    assert len(context.encode()) < 56000
    assert [f["status"] for f in files] == ["truncated", "alias", "alias", "alias"]
    assert files[0]["aliases"] == ["b.log", "c.log", "d.log"]
    assert all(f["alias_chunk_id"] == files[0]["chunk_id"] for f in files[1:])
    again = judge_packet._render_context(tmp_path, "execution", paths, [])
    assert again == (context, files)
    coverage = judge_packet._completeness(files, [])
    assert coverage["status"] == "COMPLETE"
    assert coverage["meaning"] == "REQUIRED_ARTIFACTS_ONLY"
    assert not coverage["overall_coverage"]["all_selected_content_complete"]


def test_required_receipt_is_whole_and_duplicate_satisfies_alias(tmp_path, monkeypatch):
    monkeypatch.setattr(judge_packet, "EXECUTION_CONTEXT_BYTES", 300)
    log = tmp_path / "large.log"
    log.write_text("secondary" * 300)
    paths = [log]
    for name in ("submitted.json", "completed.json"):
        path = tmp_path / name
        path.write_text('{"receipt":"same"}')
        paths.append(path)
    requirements = [{"id": "receipts", "paths": [p.name for p in paths[1:]]}]
    _, files = judge_packet._render_context(tmp_path, "execution", paths, requirements)
    coverage = judge_packet._completeness(files, requirements)
    assert coverage["eligible"]
    assert coverage["overall_coverage"]["deduplicated_paths"] == 1
    assert not coverage["overall_coverage"]["all_selected_content_complete"]
