#!/usr/bin/env python3
"""Run a single Google Antigravity SDK agent turn against a rendered prompt.

Invoked from run_paper.sh::run_agy as the replacement for the legacy
`agy --print ...` CLI.  Behavior contract:

- Read prompt from `--prompt-file PATH` (UTF-8, no trailing-newline trim).
- Set up `LocalAgentConfig(capabilities=CapabilitiesConfig())` so the agent
  has write access (default SDK mode is read-only — see the docs at
  https://github.com/google-antigravity/antigravity-sdk-python).
- Inherit cwd from the caller: run_paper.sh does
  `( cd "$PROJECT" && python3 .../agy_run.py ... )` so the agent operates
  rooted at the project dir; absolute paths to $FACTORY in the prompt body
  (modeling_guide.md, method_library/, ...) are accessed directly by the
  Go harness with no extra workspace-dir config — if that turns out to be
  insufficient, see the TODO in `_build_config` below.
- Stream agent text to stdout in real time (one print() per token batch +
  flush after each line break).  run_paper.sh watches stdout-log mtime for
  hang detection, so token-level streaming keeps the heartbeat fresh.
- Outer timeout enforced by the bash `timeout` wrapper.  This script ALSO
  applies `asyncio.wait_for` with a slightly-smaller inner timeout (passed
  via --timeout-secs) so we get a clean Python-level cancellation message
  before the bash SIGTERM lands.

Exit codes:
  0    — agent completed normally
  1    — SDK error / config error / unexpected exception
  2    — SDK not installed (ModuleNotFoundError on google.antigravity)
  124  — inner asyncio timeout fired (mirrors the unix `timeout` convention)

Auth: expects `GEMINI_API_KEY` in env (or set `api_key=` in LocalAgentConfig
below — currently we trust the env to keep this script credential-free).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import traceback
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument(
        "--prompt-file",
        type=Path,
        required=True,
        help="Path to the rendered prompt (UTF-8 text).",
    )
    p.add_argument(
        "--timeout-secs",
        type=int,
        required=True,
        help="Inner asyncio timeout. Should be < the outer bash `timeout` so "
             "we get a Python-level cancel before SIGTERM.",
    )
    p.add_argument(
        "--system-instructions",
        default=None,
        help="Optional system instructions string.  If omitted, the SDK's "
             "default system prompt is used.",
    )
    return p.parse_args()


def _build_config():
    """Construct LocalAgentConfig with write capabilities enabled.

    TODO(phase-3.6): if the Go harness refuses to read absolute paths under
    $FACTORY (the factory root, sibling to $PROJECT/.. typically), we may
    need to add the factory dir as a workspace root.  The SDK exposes
    `skills_paths` and `app_data_dir` on LocalAgentConfig but neither is the
    obvious equivalent of the legacy `agy --add-dir <DIR>` flag.  If you hit
    "permission denied" or "path not allowed" errors, inspect
    google/antigravity/connections/local/local_connection_config.py for the
    correct field name and add it here.
    """
    # Late import so we can return exit code 2 on ModuleNotFoundError without
    # crashing argparse first.
    from google.antigravity import LocalAgentConfig, CapabilitiesConfig  # type: ignore

    return LocalAgentConfig(
        capabilities=CapabilitiesConfig(),
    )


async def _run(prompt: str, timeout_secs: int, system_instructions: str | None) -> int:
    from google.antigravity import Agent  # type: ignore

    config = _build_config()
    # SDK may accept system_instructions on LocalAgentConfig — pyrefactor here
    # if the field name differs.  For now we pass it via kwarg if provided.
    if system_instructions:
        # Attempt to set after construction; ignore if the SDK exposes it
        # differently.  This keeps the script resilient to minor API drift.
        try:
            config.system_instructions = system_instructions  # type: ignore[attr-defined]
        except Exception:
            pass

    async def _chat_and_stream() -> int:
        async with Agent(config) as agent:
            response = await agent.chat(prompt)
            # Per the SDK docs, iterating a ChatResponse yields str tokens.
            # Some SDKs yield rich event objects instead — guard for both.
            async for token in response:
                if hasattr(token, "text"):
                    chunk = token.text
                else:
                    chunk = str(token)
                if not chunk:
                    continue
                sys.stdout.write(chunk)
                # Flush after every newline so run_paper.sh's hang-detection
                # mtime watcher sees fresh writes promptly.
                if "\n" in chunk:
                    sys.stdout.flush()
            sys.stdout.write("\n")
            sys.stdout.flush()
        return 0

    try:
        return await asyncio.wait_for(_chat_and_stream(), timeout=timeout_secs)
    except asyncio.TimeoutError:
        print(
            f"\n[agy_run] inner timeout fired after {timeout_secs}s",
            file=sys.stderr,
            flush=True,
        )
        return 124


def main() -> int:
    args = _parse_args()
    try:
        prompt = args.prompt_file.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[agy_run] cannot read prompt file {args.prompt_file}: {exc}",
              file=sys.stderr)
        return 1

    if not os.environ.get("GEMINI_API_KEY"):
        print("[agy_run] GEMINI_API_KEY not set in environment — the SDK will "
              "fail to authenticate.  Set it in your shell profile or in the "
              "factory's .env (see .env.example) before running.",
              file=sys.stderr)
        # Don't fail here — let the SDK produce its own error, which carries
        # more context (it may also accept OAuth tokens at ~/.gemini/...).

    try:
        return asyncio.run(_run(prompt, args.timeout_secs, args.system_instructions))
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("google"):
            print("[agy_run] google-antigravity SDK is not installed.  Run:\n"
                  "    pip install --user google-antigravity\n"
                  f"Underlying error: {exc}", file=sys.stderr)
            return 2
        traceback.print_exc()
        return 1
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
