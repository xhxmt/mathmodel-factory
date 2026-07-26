import json

from scripts.run_codex_tui_judge import plain_terminal_text, valid_protocol


def test_valid_protocol_requires_verdict_and_complete_json(tmp_path):
    output = tmp_path / "judge.md"

    output.write_text('VERDICT: PASS\n{"verdict":"PASS"}\n', encoding="utf-8")
    assert valid_protocol(output) is True

    output.write_text('VERDICT: PASS\n{"verdict":', encoding="utf-8")
    assert valid_protocol(output) is False

    output.write_text('{"verdict":"PASS"}\n', encoding="utf-8")
    assert valid_protocol(output) is False


def test_plain_terminal_text_removes_cursor_sequences_between_words():
    raw = b"\x1b[3;3HDo\x1b[3;6Hyou\x1b[3;10Htrust\x1b[3;16Hthe\x1b[3;20Hcontents"

    assert plain_terminal_text(raw) == b"Doyoutrustthecontents"
