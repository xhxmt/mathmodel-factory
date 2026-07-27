import argparse
import hashlib
import json
import sys

import pytest

from scripts import api_agent_run


def _args(**overrides):
    values = {
        "model": "deepseek-chat",
        "backend": "deepseek",
        "base_url": None,
        "key_env": "DEEPSEEK_API_KEY",
        "timeout": 30,
        "max_tokens": 1000,
        "output_file": "judge_evaluation.md",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_configuration_fingerprint_binds_prompt_context_and_system_version():
    records = [{"path": "paper.tex", "status": "included", "source_sha256": "abc"}]

    first = api_agent_run._configuration_record(_args(), "judge this", records)
    second = api_agent_run._configuration_record(_args(), "judge this", records)
    changed = api_agent_run._configuration_record(_args(model="other"), "judge this", records)

    assert first == second
    assert first["configuration_fingerprint"] != changed["configuration_fingerprint"]
    assert first["system_prompt_version"] == "paper-evaluation-untrusted-data-v1"


def test_atomic_result_refuses_overwrite_and_records_fingerprint(tmp_path):
    output = tmp_path / "judge.md"
    metadata = {"configuration_fingerprint": "fingerprint-one"}

    metadata_path = api_agent_run._atomic_write_result(output, "first", metadata)

    assert output.read_text(encoding="utf-8") == "first"
    assert json.loads(metadata_path.read_text(encoding="utf-8")) == metadata
    with pytest.raises(FileExistsError):
        api_agent_run._atomic_write_result(
            output, "second", {"configuration_fingerprint": "fingerprint-two"}
        )
    assert output.read_text(encoding="utf-8") == "first"


def test_atomic_result_allows_explicit_overwrite(tmp_path):
    output = tmp_path / "judge.md"
    api_agent_run._atomic_write_result(output, "first", {"version": 1})

    api_agent_run._atomic_write_result(output, "second", {"version": 2}, overwrite=True)

    assert output.read_text(encoding="utf-8") == "second"
    metadata = json.loads(
        output.with_name("judge.md.llm-result.json").read_text(encoding="utf-8")
    )
    assert metadata["version"] == 2


def test_project_paths_cannot_escape_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(ValueError, match="escapes project"):
        api_agent_run._project_path(project.resolve(), "../outside.md")


def test_inline_context_records_truncation(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "paper.tex").write_text("abcdef", encoding="utf-8")
    monkeypatch.setattr(api_agent_run, "_MAX_CTX_BYTES", 3)

    context, records = api_agent_run._inline_context(project.resolve(), ["paper.tex"])

    assert records[0]["status"] == "truncated"
    assert records[0]["source_bytes"] == 6
    assert "abc" in context


def test_judge_packet_context_automatically_includes_manifest(tmp_path):
    project = tmp_path / "project"
    packet = project / "judge_packets" / "execution"
    packet.mkdir(parents=True)
    (packet / "manifest.json").write_text('{"status_counts": {"included": 1}}')
    (packet / "context.txt").write_text("paper claim and result evidence")

    context, records = api_agent_run._inline_context(
        project.resolve(), ["judge_packets/execution/context.txt"]
    )

    assert [record["path"] for record in records] == [
        "judge_packets/execution/manifest.json",
        "judge_packets/execution/context.txt",
    ]
    assert "status_counts" in context
    assert "paper claim and result evidence" in context


def test_main_persists_the_exact_effective_prompt_sent_to_the_api(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    prompt = project / "base_prompt.txt"
    prompt.write_text("Judge the supplied packet.", encoding="utf-8")
    context = project / "judge_packets" / "math" / "context.txt"
    context.parent.mkdir(parents=True)
    context.write_text("claim evidence", encoding="utf-8")
    (context.parent / "manifest.json").write_text(
        '{"role":"math"}', encoding="utf-8"
    )
    captured: dict[str, str] = {}

    def fake_call(full_prompt, *args, **kwargs):
        captured["full_prompt"] = full_prompt
        return "VERDICT: PASS\n"

    monkeypatch.setattr(api_agent_run.llm_judge_call, "call", fake_call)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "api_agent_run.py",
            "--model",
            "deepseek-chat",
            "--backend",
            "deepseek",
            "--prompt-file",
            str(prompt),
            "--project",
            str(project),
            "--output-file",
            "judge_outputs/math.md",
            "--effective-prompt-file",
            "judge_outputs/math.rendered_prompt.txt",
            "--context-file",
            "judge_packets/math/context.txt",
        ],
    )

    assert api_agent_run.main() == 0

    effective_prompt = project / "judge_outputs" / "math.rendered_prompt.txt"
    persisted = effective_prompt.read_text(encoding="utf-8")
    assert persisted == captured["full_prompt"]
    metadata = json.loads(
        (project / "judge_outputs" / "math.md.llm-result.json").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["effective_prompt_path"] == "judge_outputs/math.rendered_prompt.txt"
    assert metadata["effective_prompt_sha256"] == hashlib.sha256(
        persisted.encode("utf-8")
    ).hexdigest()
