from __future__ import annotations

import json
import os
import re
from pathlib import Path


COMMON_PREAMBLE = """Before doing any substantive work, read the project style guide in the current project directory: prefer `modeling_guide.md` (math-modeling mode) if present, otherwise read `analysis_guide.md` (legacy social-science mode). It is the canonical guide for local job execution, figure style, project file layout, code conventions, error recovery, and table formatting.

If `human_review.md` exists, read it before substantive work and treat it as the newest human guidance. Older downstream artifacts may remain after a rewind; do not treat them as authoritative unless deliberately regenerated.

Do not inspect, read, cite, summarize, reuse, or mention completed projects unless the human researcher explicitly instructs you to do so.

ANSWER-KEY ISOLATION: never read or search `evaluation/`, `external/`, `benchmark/`, `reference_papers/`, any `*answer_key*`, `*excellent*`, or past solutions for this problem. Derive targets from the problem statement and transferable methodology only.

"""


class PromptRenderer:
    def __init__(self, factory_root: str | Path):
        self.root = Path(factory_root).resolve()

    def render(
        self,
        template: str,
        project: Path,
        *,
        step_key: str | int,
        replacements: dict[str, str] | None = None,
        include_preamble: bool = True,
    ) -> str:
        path = self.root / "prompts" / template
        text = path.read_text(encoding="utf-8")
        values = {
            "__PROJECT_PATH__": str(project),
            "__RESEARCH_QUESTION__": self.research_question(project),
            "__BASE_NAME__": project.name,
            "__FACTORY__": str(self.root),
            **(replacements or {}),
        }
        for key, value in values.items():
            text = text.replace(key, value)
        text = self._apply_ablations(template, text)
        note = self.user_note(project.name, step_key)
        if note:
            text += f"\n\nNOTE FROM THE RESEARCHER: {note}"
        agent_key = path.stem if str(step_key) == path.stem.removeprefix("step") else f"step{step_key}"
        text = f"AGENT_KEY: {agent_key}\n\n{text}"
        preamble = COMMON_PREAMBLE + self._dynamic_consultation_preamble(project)
        return preamble + text if include_preamble else text

    @staticmethod
    def research_question(project: Path) -> str:
        checkpoint = project / "checkpoint.md"
        if not checkpoint.is_file():
            return ""
        match = re.search(
            r"Research question\*{0,2}\s*[:：]\s*(.+)",
            checkpoint.read_text(encoding="utf-8", errors="replace"),
        )
        return match.group(1).strip() if match else ""

    def user_note(self, project_id: str, step_key: str | int) -> str:
        path = self.root / "web" / "notes.json"
        if not path.is_file():
            return ""
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return str(payload.get(project_id, {}).get(f"step_{step_key}", "")).strip()
        except (AttributeError, json.JSONDecodeError, OSError):
            return ""

    @staticmethod
    def _apply_ablations(template: str, text: str) -> str:
        enabled = lambda key: os.getenv(key, "0").lower() in {"1", "true", "yes", "on"}
        if template == "step1_research_viability.txt" and enabled("ABLATE_NO_CONSULTATION"):
            text = text.replace("；用 web 检索拿到主文献", "")
        if enabled("ABLATE_NO_INNOVATION_PROTECT"):
            lines = []
            for line in text.splitlines():
                if "PROTECTED" in line and any(
                    token in line for token in ("不可降级", "不得删除", "删 PROTECTED", "绝不动", "永远不动")
                ):
                    continue
                lines.append(line)
            text = "\n".join(lines)
        return text

    @staticmethod
    def _dynamic_consultation_preamble(project: Path) -> str:
        enabled = project / "consultation" / "enabled"
        if not enabled.is_file():
            return ""
        body = enabled.read_text(encoding="utf-8", errors="replace").replace(",", " ")
        gates = set(body.split())
        if gates and "dynamic" not in gates:
            return ""
        return (
            "If a load-bearing decision cannot be made responsibly, write "
            "`consultation/REQUEST.md` beginning with `CONSULT:`, explain the options, "
            "and stop without producing final Step artifacts.\n\n"
        )
