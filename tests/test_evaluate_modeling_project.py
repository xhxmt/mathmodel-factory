from pathlib import Path


def test_infer_step_windows_shell_error(monkeypatch):
    import evaluate_modeling_project as emp

    class DummyExc(OSError):
        def __init__(self):
            super().__init__("bad executable")
            self.winerror = 193

    def fake_run(*_args, **_kwargs):
        raise DummyExc()

    monkeypatch.setattr(emp.subprocess, "run", fake_run)

    step, detail = emp.infer_step(Path("C:/repo"), Path("C:/repo/project"))
    assert step is None
    assert "POSIX shell" in detail
