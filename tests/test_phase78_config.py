from __future__ import annotations

from pathlib import Path

import pytest

from factory_core.phase78_config import (
    Phase78ConfigurationError,
    load_phase78_settings,
    phase78_enabled,
)


def _enabled(tmp_path: Path) -> dict[str, str]:
    return {
        "PHASE78_ENABLED": "true",
        "PHASE78_AUTHORITY_DB_FILE": str(tmp_path / "authority.db"),
        "PHASE78_AUTHORITY_SOURCE_FENCE_SHA256": "a" * 64,
        "PHASE78_PHASE4_DB_FILE": str(tmp_path / "phase4.db"),
        "PHASE78_PHASE5_DB_FILE": str(tmp_path / "phase5.db"),
        "PHASE78_PHASE6_DB_FILE": str(tmp_path / "phase6.db"),
        "PHASE78_PHASE7_DB_FILE": str(tmp_path / "phase7.db"),
        "PHASE78_PHASE8_DB_FILE": str(tmp_path / "phase8.db"),
        "PHASE78_WORK_DB_FILE": str(tmp_path / "work.db"),
        "PHASE78_WORK_SPOOL": str(tmp_path / "spool"),
        "PHASE78_PROJECT_ROOT": str(tmp_path / "project"),
        "PHASE78_CAS_ROOT": str(tmp_path / "cas"),
        "PHASE78_SCRATCH_ROOT": str(tmp_path / "scratch"),
    }


def test_disabled_configuration_short_circuits_before_path_or_value_parsing() -> None:
    settings = load_phase78_settings(
        {
            "PHASE78_ENABLED": "false",
            "PHASE78_AUTHORITY_DB_FILE": "relative-is-ignored",
            "PHASE78_DEADLINE_MS": "not-an-integer",
        }
    )
    assert settings.enabled is False
    assert settings.authority_database is None
    assert settings.cas_root is None


def test_enabled_configuration_is_complete_absolute_and_bounded(tmp_path: Path) -> None:
    values = _enabled(tmp_path)
    values.update({"PHASE78_DEADLINE_MS": "1250", "PHASE78_LEASE_SECONDS": "9"})
    settings = load_phase78_settings(values)
    assert settings.enabled is True
    assert settings.authority_database == tmp_path / "authority.db"
    assert settings.deadline_ms == 1250
    assert settings.lease_seconds == 9


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PHASE78_ENABLED", "maybe"),
        ("PHASE78_AUTHORITY_SOURCE_FENCE_SHA256", "A" * 64),
        ("PHASE78_PHASE7_DB_FILE", "relative.db"),
        ("PHASE78_DEADLINE_MS", "0"),
        ("PHASE78_LEASE_SECONDS", "1.5"),
    ],
)
def test_enabled_invalid_configuration_fails_with_stable_public_code(
    tmp_path: Path, name: str, value: str
) -> None:
    values = _enabled(tmp_path)
    values[name] = value
    with pytest.raises(Phase78ConfigurationError) as captured:
        load_phase78_settings(values)
    assert captured.value.code == "PHASE78_CONFIGURATION_INVALID"


def test_enabled_missing_each_required_path_fails_closed(tmp_path: Path) -> None:
    for name in (
        "PHASE78_AUTHORITY_DB_FILE",
        "PHASE78_PHASE4_DB_FILE",
        "PHASE78_PHASE5_DB_FILE",
        "PHASE78_PHASE6_DB_FILE",
        "PHASE78_PHASE7_DB_FILE",
        "PHASE78_PHASE8_DB_FILE",
        "PHASE78_WORK_DB_FILE",
        "PHASE78_WORK_SPOOL",
        "PHASE78_PROJECT_ROOT",
        "PHASE78_CAS_ROOT",
        "PHASE78_SCRATCH_ROOT",
    ):
        values = _enabled(tmp_path)
        del values[name]
        with pytest.raises(Phase78ConfigurationError, match=name):
            load_phase78_settings(values)


def test_feature_flag_parser_is_exact() -> None:
    assert phase78_enabled({}) is False
    assert phase78_enabled({"PHASE78_ENABLED": "ON"}) is True
    with pytest.raises(Phase78ConfigurationError):
        phase78_enabled({"PHASE78_ENABLED": "enabled"})
