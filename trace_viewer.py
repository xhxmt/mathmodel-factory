#!/usr/bin/env python3
"""
trace_viewer.py — Find and display the active agent trace for a paper project.

Searches both Claude Code subagent traces and Codex session traces,
identifies the most recently active one, and prints a human-readable
summary of recent tool calls and agent messages.

Usage:
    python3 trace_viewer.py <project_name> [--lines N] [--follow]
"""

import json
import os
import sys
import time
import glob
import argparse
from pathlib import Path
from datetime import datetime, timezone


FACTORY = str(Path(__file__).resolve().parent)
CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
CODEX_SESSIONS = os.path.expanduser("~/.codex/sessions")


def find_claude_traces(project_name):
    """Find Claude Code trace files for a project.

    Claude Code encodes project paths by replacing / and _ with -.
    Rather than reconstructing the exact encoding, we glob for directories
    containing the project name (with underscores replaced by hyphens).
    """
    results = []
    # Project name with underscores → hyphens to match Claude's encoding
    name_encoded = project_name.replace("_", "-")
    pattern = os.path.join(CLAUDE_PROJECTS, f"*{name_encoded}")
    for trace_dir in glob.glob(pattern):
        if not os.path.isdir(trace_dir):
            continue
        for path in Path(trace_dir).rglob("*.jsonl"):
            mtime = path.stat().st_mtime
            is_subagent = "subagents" in str(path)
            results.append({
                "path": str(path),
                "mtime": mtime,
                "size": path.stat().st_size,
                "type": "claude-subagent" if is_subagent else "claude-parent",
                "project": project_name,
            })
    return results


def find_codex_traces(project_name):
    """Find Codex session traces whose cwd matches the project."""
    results = []
    if not os.path.isdir(CODEX_SESSIONS):
        return results

    # Search recent session files (last 7 days of directories)
    for day_dir in sorted(Path(CODEX_SESSIONS).rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        # Only check files modified in the last 48 hours
        if time.time() - day_dir.stat().st_mtime > 48 * 3600:
            break
        try:
            with open(day_dir) as f:
                first_line = f.readline().strip()
                if not first_line:
                    continue
                d = json.loads(first_line)
                payload = d.get("payload", {})
                cwd = payload.get("cwd", "")
                if project_name in cwd:
                    results.append({
                        "path": str(day_dir),
                        "mtime": day_dir.stat().st_mtime,
                        "size": day_dir.stat().st_size,
                        "type": "codex",
                        "project": project_name,
                        "cwd": cwd,
                    })
        except (json.JSONDecodeError, OSError):
            continue
    return results


def parse_claude_line(d):
    """Parse a Claude Code JSONL line into a human-readable event."""
    t = d.get("type", "")
    if t == "assistant":
        msg = d.get("message", {})
        events = []
        for c in msg.get("content", []):
            ct = c.get("type", "")
            if ct == "tool_use":
                name = c.get("name", "?")
                inp = c.get("input", {})
                detail = _claude_tool_detail(name, inp)
                events.append(f"  \033[36m{name}\033[0m {detail}")
            elif ct == "text":
                text = c.get("text", "")[:150].replace("\n", " ")
                events.append(f"  \033[33m...\033[0m {text}")
        return events
    elif t == "tool_result":
        # Skip tool results to keep output clean (they're verbose)
        return []
    return []


def _claude_tool_detail(name, inp):
    """Format tool call details concisely."""
    if name == "Bash":
        cmd = inp.get("command", "")
        # Show first line only, truncated
        first_line = cmd.split("\n")[0][:100]
        return f"$ {first_line}"
    elif name in ("Read", "Write"):
        fp = inp.get("file_path", "")
        return os.path.basename(fp)
    elif name == "Edit":
        fp = inp.get("file_path", "")
        return f"{os.path.basename(fp)}"
    elif name == "Grep":
        pat = inp.get("pattern", "")[:50]
        path = inp.get("path", "")
        return f"/{pat}/ in {os.path.basename(path) if path else '.'}"
    elif name == "Glob":
        return inp.get("pattern", "")[:60]
    elif name == "Agent":
        desc = inp.get("description", "")[:60]
        return desc
    elif name == "WebSearch":
        q = inp.get("query", "")[:60]
        return f'"{q}"'
    elif name == "WebFetch":
        return inp.get("url", "")[:80]
    else:
        return ""


def parse_codex_line(d):
    """Parse a Codex JSONL line into a human-readable event."""
    t = d.get("type", "")
    payload = d.get("payload", {})

    if t == "response_item":
        item_type = payload.get("type", "")
        if item_type == "function_call":
            name = payload.get("name", "?")
            args = payload.get("arguments", "")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    pass
            if isinstance(args, dict):
                cmd = args.get("cmd", "")[:120]
            else:
                cmd = str(args)[:120]
            return [f"  \033[36m{name}\033[0m $ {cmd}"]
        elif item_type == "function_call_output":
            # Skip outputs to keep clean
            return []
        elif item_type == "message":
            events = []
            for c in payload.get("content", []):
                if isinstance(c, dict) and c.get("type") == "output_text":
                    text = c.get("text", "")[:150].replace("\n", " ")
                    events.append(f"  \033[33m...\033[0m {text}")
            return events
        elif item_type == "reasoning":
            # Show reasoning summary if available
            summaries = payload.get("summary", [])
            if summaries:
                for s in summaries[:1]:  # Just first summary line
                    text = s.get("text", "")[:150].replace("\n", " ")
                    if text:
                        return [f"  \033[35mthinking:\033[0m {text}"]
            return []
    elif t == "event_msg":
        msg_type = payload.get("type", "")
        if msg_type == "agent_message":
            text = payload.get("message", "")[:150].replace("\n", " ")
            return [f"  \033[33m...\033[0m {text}"]
        elif msg_type == "token_count":
            info = (payload.get("info") or {}).get("total_token_usage") or {}
            inp = info.get("input_tokens", 0)
            out = info.get("output_tokens", 0)
            cached = info.get("cached_input_tokens", 0)
            return [f"  \033[2mtokens: {inp:,} in ({cached:,} cached) / {out:,} out\033[0m"]
    return []


def display_trace(trace_info, num_lines=30, follow=False):
    """Read and display a trace file."""
    path = trace_info["path"]
    ttype = trace_info["type"]
    mtime = datetime.fromtimestamp(trace_info["mtime"]).strftime("%H:%M:%S")
    size_kb = trace_info["size"] / 1024

    print(f"\033[1m{'═' * 70}\033[0m")
    print(f"\033[1mProject:\033[0m  {trace_info['project']}")
    print(f"\033[1mType:\033[0m     {ttype}")
    print(f"\033[1mFile:\033[0m     {os.path.basename(path)}")
    print(f"\033[1mSize:\033[0m     {size_kb:.0f} KB  (last modified {mtime})")
    print(f"\033[1m{'═' * 70}\033[0m")
    print()

    parser = parse_claude_line if ttype.startswith("claude") else parse_codex_line

    def read_and_display(show_last_n):
        """Read file and display last N events."""
        events = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                parsed = parser(d)
                events.extend(parsed)

        if show_last_n and len(events) > show_last_n:
            events = events[-show_last_n:]
            print(f"  \033[2m... ({len(events)} earlier events omitted)\033[0m")
            print()

        for e in events:
            print(e)
        return len(events)

    if not follow:
        read_and_display(num_lines)
        return

    # Follow mode: show last N then poll for new content
    read_and_display(num_lines)
    print()
    print(f"\033[2m--- following (Ctrl-C to stop) ---\033[0m")

    last_size = os.path.getsize(path)
    last_line_count = sum(1 for _ in open(path))

    while True:
        time.sleep(5)
        try:
            new_size = os.path.getsize(path)
        except OSError:
            continue
        if new_size <= last_size:
            continue

        # Read new lines
        current_count = 0
        new_events = []
        with open(path) as f:
            for i, line in enumerate(f):
                current_count = i + 1
                if current_count <= last_line_count:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                new_events.extend(parser(d))

        for e in new_events:
            print(e)

        last_size = new_size
        last_line_count = current_count


def main():
    parser = argparse.ArgumentParser(description="View active agent traces for a paper project")
    parser.add_argument("project", help="Project name (e.g., antiwoke_backlash)")
    parser.add_argument("-n", "--lines", type=int, default=30, help="Number of recent events to show (default: 30)")
    parser.add_argument("-f", "--follow", action="store_true", help="Follow the trace in real-time (like tail -f)")
    parser.add_argument("--all", action="store_true", help="Show all trace files, not just the most active")
    parser.add_argument("--codex", action="store_true", help="Only show Codex traces")
    parser.add_argument("--claude", action="store_true", help="Only show Claude traces")
    args = parser.parse_args()

    project = args.project

    # Find all traces
    traces = []
    if not args.codex:
        traces.extend(find_claude_traces(project))
    if not args.claude:
        traces.extend(find_codex_traces(project))

    if not traces:
        print(f"No traces found for project '{project}'.")
        print(f"Searched:")
        print(f"  Claude: {CLAUDE_PROJECTS}/*{project}*/")
        print(f"  Codex:  {CODEX_SESSIONS}/")
        sys.exit(1)

    # Sort by modification time (most recent first)
    traces.sort(key=lambda t: t["mtime"], reverse=True)

    def pick_best_trace(trace_list):
        """Pick the best trace to display.

        Prefer subagent/codex traces over parent traces since subagents
        do the real work. Only pick a parent if no subagent/codex trace
        has been active in the last 10 minutes.
        """
        now = time.time()
        # Separate worker traces (subagent + codex) from parent traces
        workers = [t for t in trace_list if t["type"] != "claude-parent"]
        parents = [t for t in trace_list if t["type"] == "claude-parent"]

        # If there's a recently active worker trace, prefer it
        recent_workers = [t for t in workers if (now - t["mtime"]) < 600]
        if recent_workers:
            return max(recent_workers, key=lambda t: t["mtime"])

        # Otherwise fall back to most recently modified overall
        return trace_list[0]

    if args.all:
        print(f"\033[1mAll traces for {project} (most recent first):\033[0m\n")
        for i, t in enumerate(traces):
            age_min = (time.time() - t["mtime"]) / 60
            size_kb = t["size"] / 1024
            active = " \033[32m● ACTIVE\033[0m" if age_min < 5 else ""
            print(f"  [{i+1}] {t['type']:<16s}  {size_kb:>7.0f} KB  {age_min:>5.0f}m ago{active}")
            print(f"      {os.path.basename(t['path'])}")
            print()
        best = pick_best_trace(traces)
        print(f"Showing best active trace:\n")
        display_trace(best, num_lines=args.lines, follow=args.follow)
    else:
        best = pick_best_trace(traces)
        age_min = (time.time() - best["mtime"]) / 60

        # Mention if there are other active traces
        active_count = sum(1 for t in traces if (time.time() - t["mtime"]) / 60 < 10)
        if active_count > 1:
            print(f"\033[2m({active_count} active traces found; showing best worker trace. Use --all to see all)\033[0m\n")

        display_trace(best, num_lines=args.lines, follow=args.follow)


if __name__ == "__main__":
    main()
