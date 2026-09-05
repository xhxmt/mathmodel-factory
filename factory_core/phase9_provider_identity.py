"""Approved native Codex execution identity; hashes never expose credential values."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import shutil
from .canonical import canonical_sha256


def _file(path):
    path = Path(path).resolve(strict=True)
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
        configuration.append(_file(path) if path.exists() else {"path": str(path), "absent": True})
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
    executable = Path(f"/proc/{pid}/exe").read_bytes()
    command = Path(f"/proc/{pid}/cmdline").read_bytes()
    expected = {b"\0".join(part.encode() for part in argv) + b"\0" for argv in (wrapped, call["argv"])}
    digest = hashlib.sha256(executable).hexdigest()
    if (command not in expected or digest not in {profile["native"]["sha256"], profile["sandbox"]["sha256"]}
            or execution.get("kernel_executable_sha256") != digest
            or execution.get("kernel_cmdline_sha256") != hashlib.sha256(command).hexdigest()):
        raise ValueError("kernel provider identity differs from approved launch")
