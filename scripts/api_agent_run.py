#!/usr/bin/env python3
"""Non-agentic API-model step runner for the Modeling Factory pipeline.

The main pipeline (run_paper.sh) normally drives each step with an *agentic*
CLI backend (`claude`, `codex`, `agy`) that can read/write files and run
solvers.  A plain HTTP model API (DeepSeek, Qwen/DashScope, any
OpenAI-compatible host, Gemini) is NOT agentic — it only turns a prompt into
text.  This runner bridges that gap for the steps where a single-shot text
model is the right tool: the judge / review / evaluation steps whose output is
ONE markdown file (e.g. judge_evaluation.md, review_comments.md, evaluation.md).

It:
  1. reads the rendered step prompt,
  2. inlines a curated context bundle (the project artifacts the step needs,
     because the API model cannot open files itself),
  3. calls the model through scripts/llm_judge_call.py's `call()` dispatcher
     (explicit backend so the model registry fully controls routing),
  4. writes the model's text to --output-file (the step's expected artifact).

Backend / base-url / key-env come straight from the model-registry entry that
run_paper.sh resolved, so adding a new provider needs no change here.

Exit codes: 0 ok; 2 bad args / empty prompt; 3 backend call failed;
4 timeout; 5 empty model output; 6 could not write output file.
"""

from __future__ import annotations

import argparse
import os
import sys
import subprocess
from pathlib import Path

# Reuse the single shared backend dispatcher (DeepSeek/Qwen/OpenAI/Gemini/Claude).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm_judge_call  # noqa: E402


# Files whose content is too big / noisy to ever inline.
_MAX_CTX_BYTES = 200_000


def _inline_context(project: Path, rel_paths: list[str]) -> str:
    """Concatenate existing context files with clear delimiters."""
    chunks: list[str] = []
    for rel in rel_paths:
        p = (project / rel)
        try:
            if not p.is_file():
                continue
            data = p.read_text(errors="replace")
        except Exception:
            continue
        if len(data) > _MAX_CTX_BYTES:
            data = data[:_MAX_CTX_BYTES] + "\n…(truncated)…\n"
        chunks.append(f"\n----- 文件: {rel} -----\n{data}")
    return "".join(chunks)


def _strip_outer_fence(text: str) -> str:
    """If the whole response is wrapped in a single ``` fence, unwrap it."""
    s = text.strip()
    if s.startswith("```"):
        first_nl = s.find("\n")
        if first_nl != -1 and s.endswith("```"):
            inner = s[first_nl + 1: -3]
            # Only unwrap when there is no second fence inside (i.e. it really
            # was one outer wrapper, not legitimate fenced code blocks).
            if "```" not in inner:
                return inner.strip() + "\n"
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True)
    ap.add_argument("--backend", required=True,
                    help="openai|deepseek|gemini|claude (from the registry entry).")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--key-env", default=None)
    ap.add_argument("--prompt-file", required=True, help="Rendered step prompt.")
    ap.add_argument("--project", required=True, help="Project directory.")
    ap.add_argument("--output-file", required=True,
                    help="Relative-to-project artifact the step expects.")
    ap.add_argument("--context-file", action="append", default=[],
                    help="Relative-to-project file to inline as context (repeatable).")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--max-tokens", type=int, default=8000)
    args = ap.parse_args()

    project = Path(args.project).resolve()
    prompt_path = Path(args.prompt_file)
    if not prompt_path.is_file():
        print(f"ERROR: prompt file not found: {prompt_path}", file=sys.stderr)
        return 2
    base_prompt = prompt_path.read_text(errors="replace")
    if not base_prompt.strip():
        print("ERROR: empty prompt", file=sys.stderr)
        return 2

    context = _inline_context(project, args.context_file)
    out_rel = args.output_file

    full_prompt = (
        base_prompt
        + "\n\n"
        + "═══════════════════════════════════════════════\n"
        + "重要：你是通过 HTTP API 调用的非 agentic 模型，**无法访问文件系统、"
        + "无法运行代码、无法调用工具**。本步骤所需的全部项目材料已在下方内联给出。\n"
        + f"请直接输出文件 `{out_rel}` 的**完整最终内容**（Markdown），不要包含任何"
        + "解释性前后缀、不要用 ``` 包裹整篇内容、不要写“我已写入文件”之类的话。\n"
        + "如果本步骤要求一行 `VERDICT:` 控制标记，请把它放在输出中（按该步骤 prompt 的约定）。\n"
        + "═══════════════════════════════════════════════\n"
        + "=== 输入材料开始 ===\n"
        + (context if context.strip() else "(本步骤没有可内联的上下文文件)\n")
        + "\n=== 输入材料结束 — 现在直接输出 " + out_rel + " 的完整内容 ===\n"
    )

    try:
        text = llm_judge_call.call(
            full_prompt, args.model, args.timeout, args.max_tokens,
            backend=args.backend, base_url=args.base_url, key_env=args.key_env,
        )
    except subprocess.TimeoutExpired:
        print(f"ERROR: model '{args.model}' timed out after {args.timeout}s", file=sys.stderr)
        return 4
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {args.model} ({args.backend}) call failed: {exc}", file=sys.stderr)
        return 3

    if not text or not text.strip():
        print(f"ERROR: model '{args.model}' returned empty output", file=sys.stderr)
        return 5

    text = _strip_outer_fence(text)
    out_path = project / out_rel
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: could not write {out_path}: {exc}", file=sys.stderr)
        return 6

    print(f"api_agent_run: wrote {len(text)} chars to {out_rel} "
          f"via {args.model} [{args.backend}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
