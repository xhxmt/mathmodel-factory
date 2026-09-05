"""Approved native Codex execution identity; hashes never expose credential values."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import shutil
import stat
import re
from .canonical import canonical_sha256


def _file(path):
    path = Path(path).resolve(strict=True)
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("provider identity requires an ordinary file")
    raw = path.read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "byte_length": len(raw)}


def routing_environment_sha256(environment):
    routing = {key: value for key, value in environment.items()
               if (key.startswith(("CODEX_", "OPENAI_", "AZURE_OPENAI_", "LD_", "DYLD_", "NODE_"))
                   or key.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "PATH", "HOME", "XDG_CONFIG_HOME"})
               and key != "CODEX_CLI_PATH"
               and not any(word in key.upper() for word in ("KEY", "TOKEN", "PASSWORD", "SECRET"))}
    return canonical_sha256(routing)


def provider_identity(project=None):
    """Resolve once for approval; execute the native binary without a JS launcher."""
    selected = os.environ.get("CODEX_CLI_PATH") or shutil.which("codex")
    if not selected:
        raise ValueError("Codex CLI unavailable")
    selected = Path(selected).resolve(strict=True)
    chain = [_file(selected)]
    native = selected
    if selected.name == "codex.js":
        package = selected.parent.parent
        native = package / "node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"
        chain.append(_file(native))
    native = native.resolve(strict=True)
    with native.open("rb") as stream:
        if stream.read(4) != b"\x7fELF" or native.name != "codex":
            raise ValueError("formal provider requires an approved native Codex executable")
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).resolve()
    configuration = []
    config_paths = {codex_home / "config.toml", codex_home / "requirements.toml",
                    Path("/etc/codex/config.toml"), Path("/etc/codex/managed_config.toml"), Path("/etc/codex/requirements.toml")}
    if project is not None:
        workdir = Path(project).resolve(strict=True)
        config_paths.update(parent / ".codex/config.toml" for parent in (workdir, *workdir.parents))
    for path in sorted(config_paths):
        configuration.append({**_file(path), "load_path": str(path)} if path.exists() else {"path": str(path), "load_path": str(path), "absent": True})
    # Authentication is deliberately excluded: provider credentials may rotate,
    # while routing and config values must remain exactly those approved.
    return {"schema": "phase9-codex-native-identity-v1", "native": _file(native),
            "resolution_chain": chain, "configuration": configuration,
            "routing_environment_sha256": routing_environment_sha256(os.environ),
            "sandbox": _file("/usr/bin/bwrap"), "response_identity": "unavailable"}


def validate_call(call, profile, *, model, effort):
    if not isinstance(call, dict) or call.get("provider_identity") != profile:
        raise ValueError("provider call identity differs from approved target")
    argv = call.get("argv", [])
    if (not argv or argv[0] != profile["native"]["path"] or "exec" not in argv
            or "--model" not in argv or argv[argv.index("--model") + 1] != model
            or f'model_reasoning_effort="{effort}"' not in argv
            or call.get("argv_sha256") != canonical_sha256(argv)):
        raise ValueError("provider argv differs from approved configuration")


def verify_launch(intent, pid, execution):
    """Read kernel identity again inside the Authority writer's trusted path."""
    call = intent["provider_call"]
    profile = call["provider_identity"]
    wrapped = execution.get("sandbox_argv", [])
    if (execution.get("provider_call_sha256") != canonical_sha256(call)
            or execution.get("sandbox_argv_sha256") != canonical_sha256(wrapped)
            or not wrapped or wrapped[0] != profile["sandbox"]["path"]
            or wrapped[-len(call["argv"]):] != call["argv"]):
        raise ValueError("provider launch does not bind its approved command")
    native_pid = execution.get("native_process_pid")
    gate_pid = execution.get("gate_process_pid")
    if type(native_pid) is not int or type(gate_pid) is not int:
        raise ValueError("provider launch lacks native exec-stop identity")
    raw = Path(f"/proc/{native_pid}/stat").read_text()
    fields = raw[raw.rfind(")") + 2:].split()
    status = Path(f"/proc/{native_pid}/status").read_text()
    tracer = int(next(line.split()[1] for line in status.splitlines() if line.startswith("TracerPid:")))
    if (fields[0] not in {"t", "T"} or int(fields[1]) != gate_pid or tracer != gate_pid
            or execution.get("native_process_start_ticks") != fields[19]
            or execution.get("pid_namespace_inode") != os.stat(f"/proc/{native_pid}/ns/pid").st_ino):
        raise ValueError("native process was not held at the owned exec gate")
    ancestor = gate_pid
    while ancestor != pid:
        raw = Path(f"/proc/{ancestor}/stat").read_text()
        parent = int(raw[raw.rfind(")") + 2:].split()[1])
        if parent <= 1 or parent == ancestor:
            raise ValueError("native process escaped its wrapper scope")
        ancestor = parent
    executable = Path(f"/proc/{native_pid}/exe").read_bytes()
    command = Path(f"/proc/{native_pid}/cmdline").read_bytes()
    expected = b"\0".join(part.encode() for part in call["argv"]) + b"\0"
    digest = hashlib.sha256(executable).hexdigest()
    if (command != expected or digest != profile["native"]["sha256"]
            or execution.get("kernel_executable_sha256") != digest
            or execution.get("kernel_cmdline_sha256") != hashlib.sha256(command).hexdigest()):
        raise ValueError("kernel native provider identity differs from approved launch")
    validate_execution_view(execution, profile)
    actual_configuration = configuration_observation(native_pid, profile)
    if actual_configuration != execution.get("configuration_observation"):
        raise ValueError("native configuration observation differs")
    cwd = os.stat(f"/proc/{native_pid}/cwd")
    expected_cwd = os.stat(Path(f"/proc/{native_pid}/root") / call["cwd"].lstrip("/"))
    if (cwd.st_dev, cwd.st_ino) != (expected_cwd.st_dev, expected_cwd.st_ino):
        raise ValueError("native cwd bypasses the frozen namespace")


def validate_execution_view(execution, profile):
    view = execution.get("execution_view", {})
    if (execution.get("execution_view_sha256") != canonical_sha256(view)
            or view.get("native_sha256") != profile["native"]["sha256"]):
        raise ValueError("sealed provider execution view differs")
    if view.get("configuration_states") != expected_configuration_states(profile):
        raise ValueError("sealed view does not bind every configuration presence/absence")
    observed = execution.get("configuration_observation")
    if observed != {"states": expected_configuration_states(profile), "read_only_directories": view.get("protected_directories")}:
        raise ValueError("native configuration namespace proof differs")
    required_directories = sorted({str(parent) for row in profile["configuration"]
                                  for parent in Path(row.get("load_path", row["path"])).parents},
                                 key=lambda p: (len(Path(p).parts), p))
    if view.get("protected_directories") != required_directories:
        raise ValueError("configuration namespace ancestor closure differs")
    files = view.get("files", [])
    required = [profile["native"], *[row for row in profile["configuration"] if not row.get("absent")]]
    for row in required:
        if not any(item.get("source") == row["path"] and item.get("destination") == row.get("load_path", row["path"])
                   and item.get("sha256") == row["sha256"] and item.get("byte_length") == row["byte_length"]
                   and item.get("seals") == 15 for item in files):
            raise ValueError("approved provider/configuration lacks sealed execution bytes")


def expected_configuration_states(profile):
    return sorted([{"load_path": row.get("load_path", row["path"]),
                    "presence": "ABSENT" if row.get("absent") else "PRESENT",
                    "sha256": None if row.get("absent") else row["sha256"],
                    "byte_length": 0 if row.get("absent") else row["byte_length"]}
                   for row in profile["configuration"]], key=lambda row: row["load_path"])


def configuration_observation(native_pid, profile):
    """Independently inspect the stopped child's root and readonly mounts."""
    root = Path(f"/proc/{native_pid}/root")
    states = expected_configuration_states(profile)
    for row in states:
        path = root / row["load_path"].lstrip("/")
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            if row["presence"] != "ABSENT":
                raise ValueError("approved config is absent in the native view")
            continue
        if row["presence"] == "ABSENT" or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("unapproved config exists in the native view")
        raw = path.read_bytes()
        if len(raw) != row["byte_length"] or hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise ValueError("native configuration bytes differ")
    directories = sorted({str(parent) for row in states for parent in Path(row["load_path"]).parents},
                         key=lambda p: (len(Path(p).parts), p))
    mounts = {}
    for line in Path(f"/proc/{native_pid}/mountinfo").read_text().splitlines():
        fields = line.split()
        name = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4])
        mounts[name] = fields[5].split(',')
    if any('ro' not in mounts.get(path, []) for path in directories):
        raise ValueError("native configuration namespace is not immutable")
    return {"states": states, "read_only_directories": directories}
