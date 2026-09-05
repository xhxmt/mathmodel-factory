"""Bounded real local FAILED/KILL/PAUSE process observations for Phase9.

These are controlled process-scope probes, not scientific solver work and not
provider responses. Their command bytes and OS observations remain in the
runtime record directory and their attempts use the same pre-dispatch ledger.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import signal
import sys

from .adapters.infrastructure.process import ProcessRequest, ProcessSupervisor
from .canonical import canonical_sha256
from .deadline import cap_timeout
from .phase9_runtime import _write_new


def _group_members(pid):
    active = []
    for path in Path("/proc").iterdir():
        if path.name.isdecimal():
            try:
                raw = (path / "stat").read_text()
                fields = raw[raw.rfind(")") + 2:].split()
                if int(fields[2]) == pid and fields[0] != "Z":
                    active.append(int(path.name))
            except FileNotFoundError:
                pass
    return active


def run_process_scope_probes(authority, runtime_id, records):
    records.mkdir()
    for action in ("failed", "kill", "pause"):
        folder = records / action
        folder.mkdir()
        # No child processes or session escape are available in this fixed
        # worker. Separate acceptance probes exercise descendant cleanup.
        code = ("import sys; print('PHASE9 FAILED SCOPE PROBE', flush=True); sys.exit(7)" if action == "failed"
                else "import time; print('PHASE9 CONTROLLED SCOPE PROBE', flush=True); time.sleep(60)")
        argv = [sys.executable, "-I", "-B", "-c", code]
        request = {"schema": "authority-phase9-process-probe-command-v1", "action": action,
                   "argv": argv, "timeout_seconds": cap_timeout(2),
                   "python_sha256": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()}
        _write_new(folder / "command.json", request)
        intent = authority.reserve_attempt(runtime_id, action, canonical_sha256(request))
        _write_new(folder / "dispatch_intent.json", intent)
        launch = None
        pause_observed = False
        stop_sent = False

        def started(pid):
            nonlocal launch
            launch = authority.launch(intent, pid)
            _write_new(folder / "launch.json", launch)

        def stop_requested():
            nonlocal pause_observed, stop_sent
            if action != "pause" or launch is None:
                return False
            pid = launch["process_pid"]
            # Let the fixed worker publish its identifying line before STOP.
            log = folder / "raw.log"
            if not log.exists() or not log.stat().st_size:
                return False
            if not stop_sent:
                os.killpg(pid, signal.SIGSTOP)
                stop_sent = True
            raw = Path(f"/proc/{pid}/stat").read_text()
            pause_observed = raw[raw.rfind(")") + 2:].split()[0] == "T"
            return pause_observed

        try:
            result = ProcessSupervisor().run(ProcessRequest(
                argv, folder, request["timeout_seconds"], folder / "raw.log",
                poll_seconds=0.05, kill_grace_seconds=0.1,
                on_started=started, stop_requested=stop_requested,
            ))
            members = _group_members(result.pid)
            passed = launch is not None and not members and (
                (action == "failed" and result.returncode == 7 and not result.timed_out)
                or (action == "kill" and result.timed_out and result.returncode == 124)
                or (action == "pause" and pause_observed and result.metadata.get("stop_requested") is True and result.returncode == -signal.SIGKILL)
            )
            identity = canonical_sha256({"launch_sha256": (launch or {}).get("launch_sha256"),
                                         "process_pid": result.pid, "process_start_ticks": (launch or {}).get("process_start_ticks")})
            probe_result = {"schema": "authority-phase9-process-scope-result-v1", "action": action.upper(),
                            "process_identity_sha256": identity, "result": "PASS" if passed else "FAIL",
                            "active_descendant_count": len(members)}
            raw = (folder / "raw.log").read_bytes()
            observation = {
                "outcome": "SUCCEEDED" if passed else "FAILED", "probe_pass": passed,
                "exit_code": result.returncode, "process_pid": result.pid,
                "launch_sha256": (launch or {}).get("launch_sha256"),
                "process_group_active_count": len(members), "active_group_members": members,
                "outputs": {"log": {"sha256": hashlib.sha256(raw).hexdigest(), "byte_length": len(raw)}},
                "process_identity_sha256": identity, "probe_result": probe_result,
                "probe_result_sha256": canonical_sha256(probe_result), "pause_observed": pause_observed,
            }
            _write_new(folder / "observation.json", observation)
            authority.observe(intent, observation)
            if not passed:
                raise RuntimeError(f"actual {action} process probe did not satisfy closure")
        except BaseException:
            # Existing observations are immutable. If no observation reached
            # the ledger the reserved attempt remains unresolved, never retried.
            raise
