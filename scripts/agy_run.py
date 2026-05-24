#!/usr/bin/env python3
"""Run a single Google Antigravity SDK agent turn against a rendered prompt.

Invoked from run_paper.sh::run_agy as the replacement for the legacy
`agy --print ...` CLI.  Behavior contract:

- Read prompt from `--prompt-file PATH` (UTF-8).
- Set up `LocalAgentConfig` with:
    * workspaces = --workspace args (repeatable) — equivalent of the
      legacy CLI's `--add-dir`.  File-tool access is restricted to these
      directories via the auto-applied `policy.workspace_only()` rule (see
      LocalAgentConfig._apply_workspace_policies).  Pass $PROJECT and
      $FACTORY both, so the agent can read modeling_guide.md, method_library/,
      and the prompt templates from the factory root.
    * policies = [policy.allow_all()] — equivalent of the legacy CLI's
      `--dangerously-skip-permissions`.  Without this, the default policy
      (`confirm_run_command()`) DENIES run_command, breaking any agent that
      needs to shell out to solver_submit.sh / pytest / etc.  Note that
      workspace_only file scoping is still applied on top of allow_all by
      the LocalAgentConfig model validator.
    * capabilities = default (LocalAgentConfig's default factory leaves
      every BuiltinTool enabled; the base AgentConfig's read-only default
      is overridden in the subclass).
    * model = --model if given, else SDK default (whatever the OAuth /
      GEMINI_API_KEY identity is bound to).
- Stream agent text deltas to stdout in real time (one print per Text
  chunk + flush after each line break).  run_paper.sh watches the
  stdout-log mtime for hang detection.
- Outer timeout enforced by the bash `timeout` wrapper.  This script ALSO
  applies `asyncio.wait_for` with a slightly-smaller inner timeout (passed
  via --timeout-secs) so we get a clean Python-level cancellation message
  before the bash SIGTERM lands.

Exit codes:
  0    — agent completed normally
  1    — SDK error / config error / unexpected exception
  2    — SDK not installed (ModuleNotFoundError on google.antigravity)
  124  — inner asyncio timeout fired (mirrors the unix `timeout` convention)

Auth: GEMINI_API_KEY env var.  Get a key at https://aistudio.google.com/apikey
and put it in $FACTORY/.env as GEMINI_API_KEY=... — run_paper.sh sources that
file on startup so this script inherits the value via os.environ.

NOTE: the SDK does NOT accept the OAuth token used by the legacy `agy` CLI
at ~/.gemini/antigravity-cli/antigravity-oauth-token.  Inspected on disk:
.venv/.../google/antigravity/connections/local/local_connection.py raises
AntigravityValidationError if neither GeminiConfig(api_key=...) nor
GEMINI_API_KEY is set.
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
        "--workspace",
        action="append",
        default=[],
        metavar="DIR",
        help="Workspace directory the agent may read/write in.  Repeatable.  "
             "Equivalent to the legacy `agy --add-dir <DIR>` flag.  Pass "
             "$PROJECT and $FACTORY both for the modeling factory.",
    )
    p.add_argument(
        "--model",
        default=None,
        help="Optional Gemini model name (e.g. 'gemini-2.5-pro', "
             "'gemini-2.5-flash').  Defaults to whatever the SDK / OAuth "
             "identity is bound to.",
    )
    p.add_argument(
        "--system-instructions",
        default=None,
        help="Optional system instructions string appended to the default "
             "templated system prompt.",
    )
    return p.parse_args()


def _build_config(args: argparse.Namespace):
    """Construct LocalAgentConfig with autonomous-run policy + workspaces."""
    # Late imports so we can return exit code 2 on ModuleNotFoundError
    # without crashing argparse first.
    from google.antigravity import LocalAgentConfig  # type: ignore
    from google.antigravity.hooks import policy  # type: ignore

    workspaces = [str(Path(w).expanduser().resolve()) for w in args.workspace]

    kwargs = {
        "policies": [policy.allow_all()],
        "workspaces": workspaces,
    }
    if args.model:
        kwargs["model"] = args.model
    if args.system_instructions:
        kwargs["system_instructions"] = args.system_instructions

    return LocalAgentConfig(**kwargs)


async def _run(args: argparse.Namespace, prompt: str) -> int:
    from google.antigravity import Agent  # type: ignore
    from google.antigravity.types import Text  # type: ignore

    config = _build_config(args)

    async def _chat_and_stream() -> int:
        async with Agent(config) as agent:
            response = await agent.chat(prompt)
            # The .chunks cursor yields the rich event stream (Text + ToolCall
            # + ToolResult).  We log tool calls inline so the run_paper.sh log
            # records what files / commands the agent touched, then stream
            # text deltas straight to stdout.
            async for chunk in response.chunks:
                if isinstance(chunk, Text):
                    sys.stdout.write(chunk.text)
                    if "\n" in chunk.text:
                        sys.stdout.flush()
                else:
                    # ToolCall / ToolResult: render a one-line audit entry.
                    cls = type(chunk).__name__
                    print(f"\n[{cls}] {chunk!r}", flush=True)
            sys.stdout.write("\n")
            sys.stdout.flush()
            # Best-effort usage report for cost tracking; surface via stderr
            # so it doesn't pollute the agent's text stream.
            try:
                usage = agent.conversation.usage  # type: ignore[attr-defined]
                print(f"[agy_run] usage: {usage!r}", file=sys.stderr, flush=True)
            except Exception:
                pass
        return 0

    try:
        return await asyncio.wait_for(
            _chat_and_stream(), timeout=args.timeout_secs
        )
    except asyncio.TimeoutError:
        print(
            f"\n[agy_run] inner timeout fired after {args.timeout_secs}s",
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
        print(
            "[agy_run] GEMINI_API_KEY is not set in the environment.  The "
            "google-antigravity SDK requires a Gemini API key and does NOT "
            "accept the OAuth token used by the legacy `agy` CLI (see "
            ".venv/.../google/antigravity/connections/local/local_connection.py "
            "around the AntigravityValidationError raise).  Get a key at "
            "https://aistudio.google.com/apikey and put it in $FACTORY/.env "
            "as GEMINI_API_KEY=... — run_paper.sh sources that file on "
            "startup so subprocesses inherit it.",
            file=sys.stderr,
            flush=True,
        )
        # Don't fail here — let the SDK raise its own auth error with context
        # (it may also accept api_key passed programmatically in some flows).

    try:
        return asyncio.run(_run(args, prompt))
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("google"):
            print("[agy_run] google-antigravity SDK is not installed in this "
                  "Python interpreter.  Run:\n"
                  "    python3 -m venv .venv\n"
                  "    .venv/bin/pip install google-antigravity\n"
                  "And make sure run_paper.sh invokes .venv/bin/python3 "
                  "(not system python3).\n"
                  f"Underlying error: {exc}", file=sys.stderr)
            return 2
        traceback.print_exc()
        return 1
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
