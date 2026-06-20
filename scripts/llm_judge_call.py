#!/usr/bin/env python3
"""Shared single-shot LLM judge caller for the evaluation harnesses.

One backend dispatcher used by BOTH evaluation/run_evaluation.sh (external
scoring loop) and scripts/perturbation_harness.py, so the three model backends
live in one place and cannot drift apart.

Backend is chosen by the model name prefix:
  - "deepseek*"  -> DeepSeek chat completions API   (DEEPSEEK_API_KEY)
  - "gemini*"    -> Google Generative Language API  (GEMINI_API_KEY)
  - everything else -> `claude -p` subprocess        (CLAUDE_BIN, default `claude`)

The prefix dispatch keeps the existing evaluation harness callers working
unchanged.  The model-registry layer (web/model_registry.json, read by
run_paper.sh) can instead pass an EXPLICIT backend via --backend, plus
--base-url / --key-env for the generic OpenAI-compatible backend.  That lets
arbitrary new providers (Qwen/DashScope, Moonshot, OpenRouter, a local
vLLM/Ollama OpenAI shim, …) be registered without editing this file.

Supported explicit backends:
  - "openai"   -> POST {base_url}/chat/completions  (Bearer key from --key-env)
  - "deepseek" -> DeepSeek (an OpenAI-compatible host; same path)
  - "gemini"   -> Google Generative Language API
  - "claude"   -> `claude -p` subprocess

The prompt is read from stdin; the raw model text is written to stdout. Exit 0
on success, non-zero (with a one-line reason on stderr) on any failure, so a
bash caller can branch on rc and capture an empty body as "judge failed".

Usage:
    llm_judge_call.py --model deepseek-chat [--timeout 360] [--max-tokens 4000]
    echo "<prompt>" | llm_judge_call.py --model gemini-2.5-flash
    echo "<prompt>" | llm_judge_call.py --model qwen-max \
        --backend openai \
        --base-url https://dashscope.aliyuncs.com/compatible-mode/v1 \
        --key-env DASHSCOPE_API_KEY
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request


def _openai_compat_call(prompt: str, model: str, timeout: int, max_tokens: int,
                        base_url: str, key_env: str) -> str:
    """Generic OpenAI-compatible /chat/completions backend.

    Works for DeepSeek, Qwen/DashScope (compatible-mode), Moonshot, OpenRouter,
    a local vLLM/Ollama OpenAI shim, etc.  base_url is the API root WITHOUT the
    trailing '/chat/completions' (e.g. 'https://api.deepseek.com' or
    'https://dashscope.aliyuncs.com/compatible-mode/v1').
    """
    key = os.environ.get(key_env, "")
    if not key:
        raise RuntimeError(f"{key_env} not set")
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return data["choices"][0]["message"]["content"]


def _deepseek_call(prompt: str, model: str, timeout: int, max_tokens: int) -> str:
    # DeepSeek is an OpenAI-compatible host; reuse the generic backend.
    return _openai_compat_call(prompt, model, timeout, max_tokens,
                               "https://api.deepseek.com", "DEEPSEEK_API_KEY")


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


def call(prompt: str, model: str, timeout: int, max_tokens: int,
         backend: str | None = None, base_url: str | None = None,
         key_env: str | None = None) -> str:
    """Dispatch a single-shot call.

    If `backend` is given, use it explicitly (registry-driven path).  Otherwise
    fall back to prefix dispatch on the model name (legacy harness path).
    """
    if backend:
        backend = backend.lower()
        if backend in ("openai", "deepseek", "qwen", "openai_compat"):
            url = base_url or "https://api.deepseek.com"
            env = key_env or "DEEPSEEK_API_KEY"
            return _openai_compat_call(prompt, model, timeout, max_tokens, url, env)
        if backend == "gemini":
            return _gemini_call(prompt, model, timeout, max_tokens)
        if backend == "claude":
            return _claude_call(prompt, model, timeout, max_tokens)
        raise RuntimeError(f"unknown backend '{backend}'")
    if model.startswith("deepseek"):
        return _deepseek_call(prompt, model, timeout, max_tokens)
    if model.startswith("gemini"):
        return _gemini_call(prompt, model, timeout, max_tokens)
    return _claude_call(prompt, model, timeout, max_tokens)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, help="Model name (prefix selects backend unless --backend given).")
    ap.add_argument("--backend", default=None,
                    help="Explicit backend: openai|deepseek|gemini|claude. Overrides prefix dispatch.")
    ap.add_argument("--base-url", default=None,
                    help="API root for the openai backend (no trailing /chat/completions).")
    ap.add_argument("--key-env", default=None,
                    help="Env var holding the API key for the openai backend.")
    ap.add_argument("--timeout", type=int, default=360, help="Seconds (default 360).")
    ap.add_argument("--max-tokens", type=int, default=4000, help="Max output tokens (default 4000).")
    args = ap.parse_args()

    prompt = sys.stdin.read()
    if not prompt.strip():
        print("ERROR: empty prompt on stdin", file=sys.stderr)
        return 2

    try:
        text = call(prompt, args.model, args.timeout, args.max_tokens,
                    backend=args.backend, base_url=args.base_url, key_env=args.key_env)
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
