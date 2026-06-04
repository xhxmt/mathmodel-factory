#!/usr/bin/env python3
"""Shared single-shot LLM judge caller for the evaluation harnesses.

One backend dispatcher used by BOTH evaluation/run_evaluation.sh (external
scoring loop) and scripts/perturbation_harness.py, so the three model backends
live in one place and cannot drift apart.

Backend is chosen by the model name prefix:
  - "deepseek*"  -> DeepSeek chat completions API   (DEEPSEEK_API_KEY)
  - "gemini*"    -> Google Generative Language API  (GEMINI_API_KEY)
  - everything else -> `claude -p` subprocess        (CLAUDE_BIN, default `claude`)

The prompt is read from stdin; the raw model text is written to stdout. Exit 0
on success, non-zero (with a one-line reason on stderr) on any failure, so a
bash caller can branch on rc and capture an empty body as "judge failed".

Usage:
    llm_judge_call.py --model deepseek-chat [--timeout 360] [--max-tokens 4000]
    echo "<prompt>" | llm_judge_call.py --model gemini-2.5-flash
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request


def _deepseek_call(prompt: str, model: str, timeout: int, max_tokens: int) -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY not set")
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }).encode()
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return data["choices"][0]["message"]["content"]


def _gemini_call(prompt: str, model: str, timeout: int, max_tokens: int) -> str:
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max(max_tokens, 8192),
                             "temperature": 0.0},
    }).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    parts = data["candidates"][0]["content"]["parts"]
    text = "".join(p.get("text", "") for p in parts)
    if not text:
        raise RuntimeError("gemini returned no text parts")
    return text


def _claude_call(prompt: str, model: str, timeout: int, max_tokens: int) -> str:
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    args = [claude_bin, "-p", "--dangerously-skip-permissions",
            "--strict-mcp-config", "--model", model]
    effort = os.environ.get("CLAUDE_EFFORT")
    if effort:
        args += ["--effort", effort]
    r = subprocess.run(args, input=prompt, capture_output=True, text=True,
                       timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"claude exited {r.returncode}: {r.stderr.strip()[:200]}")
    return r.stdout


def call(prompt: str, model: str, timeout: int, max_tokens: int) -> str:
    if model.startswith("deepseek"):
        return _deepseek_call(prompt, model, timeout, max_tokens)
    if model.startswith("gemini"):
        return _gemini_call(prompt, model, timeout, max_tokens)
    return _claude_call(prompt, model, timeout, max_tokens)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, help="Model name (prefix selects backend).")
    ap.add_argument("--timeout", type=int, default=360, help="Seconds (default 360).")
    ap.add_argument("--max-tokens", type=int, default=4000, help="Max output tokens (default 4000).")
    args = ap.parse_args()

    prompt = sys.stdin.read()
    if not prompt.strip():
        print("ERROR: empty prompt on stdin", file=sys.stderr)
        return 2

    try:
        text = call(prompt, args.model, args.timeout, args.max_tokens)
    except subprocess.TimeoutExpired:
        print(f"ERROR: model '{args.model}' timed out after {args.timeout}s", file=sys.stderr)
        return 4
    except Exception as exc:  # noqa: BLE001 - surface any backend error as rc!=0
        print(f"ERROR: {args.model} call failed: {exc}", file=sys.stderr)
        return 3

    if not text.strip():
        print(f"ERROR: model '{args.model}' returned empty output", file=sys.stderr)
        return 5

    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
