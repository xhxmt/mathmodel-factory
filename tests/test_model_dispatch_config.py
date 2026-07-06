import json
from pathlib import Path


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_step_model_ids_prefers_project_override_and_supports_default_string(tmp_path):
    config = tmp_path / "model_config.json"
    write_json(
        config,
        {
            "_default": {
                "step_13": {"primary": "default-judge", "fallback": "default-backup"},
                "step_7": "default-eval",
            },
            "demo": {
                "step_13": {"primary": "project-judge"},
            },
        },
    )

    from scripts.model_dispatch_config import get_step_model_ids

    assert get_step_model_ids(config, "demo", "13") == ("project-judge", "")
    assert get_step_model_ids(config, "other", "13") == ("default-judge", "default-backup")
    assert get_step_model_ids(config, "other", "7") == ("default-eval", "")
    assert get_step_model_ids(config, "other", "5") is None


def test_model_entry_rejects_missing_and_disabled_models(tmp_path):
    registry = tmp_path / "model_registry.json"
    write_json(
        registry,
        {
            "models": [
                {"id": "codex-fast", "backend": "codex", "model": "gpt-5.5", "effort": "xhigh"},
                {"id": "old", "backend": "claude", "model": "haiku", "enabled": False},
            ]
        },
    )

    from scripts.model_dispatch_config import get_model_entry

    assert get_model_entry(registry, "codex-fast") == {
        "backend": "codex",
        "model": "gpt-5.5",
        "effort": "xhigh",
        "base_url": "",
        "key_env": "",
    }
    assert get_model_entry(registry, "old") is None
    assert get_model_entry(registry, "missing") is None
