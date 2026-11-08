"""Stage catalog v2 wording for the shared paper-segment prompts.

The prompt templates are shared by v1 and v2 projects.  A v1 project must keep
byte-identical prompts (benchmark comparability; ``prompt_template_sha256`` and
``effective_prompt_sha256`` stay unchanged), so the templates are not edited.
A v2 project instead gets these exact sentence replacements after rendering,
because in v2 there is no Step-13 math precheck: Step 13 is the one full
three-role final audit, run after Steps 14 and 15 and after the content-freeze
approval, and Step 16 only accepts it.

Every ``old`` string must occur exactly once in its rendered template.  If a
template edit removes one, rendering a v2 prompt fails instead of silently
telling the agent about a precheck that does not exist; the test suite checks
the same condition for every entry, and renders every active prompt of a v2
project to check that none still mentions the precheck.
"""

from __future__ import annotations

from pathlib import Path

V2_REVIEW_REWORK_LINE = (
    "- 审稿返工：如果本提示词开头有「审稿返工」段，本次运行就是为那些审稿问题而重算，按该段逐条处理；"
    "没有该段，本步骤不是因审稿问题而重跑。"
)

# The computation Step each template renders, for the review rework block.
REVIEW_REWORK_TEMPLATES = {
    "step5_full_solve.txt": 5,
    "step6_sensitivity.txt": 6,
    "step7_model_eval.txt": 7,
    "step8_visualization.txt": 8,
}


def review_rework_block(project: Path, template: str) -> str:
    """The open review issues a v2 review recompute is re-running this Step for.

    Present only while the latest ``rework_rule=review_issue`` reopen has not
    yet returned to its revision and this Step is one it recomputes. It names
    each issue with the ledger's current Category, Severity and Notes, so the
    re-run Step does not have to infer why it runs from events it cannot see.
    """

    step = REVIEW_REWORK_TEMPLATES.get(Path(template).name)
    if step is None:
        return ""
    from ..audit.ledger import open_review_issues
    from ..stages import STAGE_CATALOG_VERSION_V2
    from ..storage import SQLiteStateStore

    store = SQLiteStateStore(project)
    if not store.exists or store.load().stage_catalog_version != STAGE_CATALOG_VERSION_V2:
        return ""
    events = store.events()
    reopen = next(
        (
            event
            for event in reversed(events)
            if event.type == "STAGE_SEMANTIC_REOPENED"
            and event.payload.get("rework_rule") == "review_issue"
        ),
        None,
    )
    if reopen is None or step not in (reopen.payload.get("recompute_steps") or []):
        return ""
    return_subtask = reopen.payload.get("return_subtask")
    if any(
        event.revision > reopen.revision
        and event.type == "STEP_SUCCEEDED"
        and event.payload.get("stage") == 8
        and event.payload.get("subtask") == return_subtask
        for event in events
    ):
        return ""
    ids = [str(item).split(" ", 1)[0] for item in reopen.payload.get("review_issues") or []]
    if not ids:
        return ""
    current = {
        issue.issue_id: issue
        for issue in open_review_issues(project / "audit_issue_ledger.md", {"BLOCKING", "MAJOR"})
    }
    lines = []
    for issue_id in ids:
        issue = current.get(issue_id)
        if issue is None:
            lines.append(f"- {issue_id}：台账中已不是 OPEN 的 BLOCKING/MAJOR 行，以台账为准。")
        else:
            lines.append(f"- {issue_id}（{issue.category}，{issue.severity}）：{issue.notes}")
    steps = "、".join(str(item) for item in reopen.payload.get("recompute_steps") or [])
    return (
        "【审稿返工 — 本次运行的原因】\n"
        f"引擎因下列 Step 11 审稿问题重算 Step {steps}（事件 rev {reopen.revision}，"
        "`rework_rule=review_issue`），本步骤是这次重算的一部分；这些问题就是本次运行要解决的事。"
        "逐条判断根因是否在本步骤的产物（代码、结果、图、报告）中：在的，必须在本步骤修复"
        "（改代码后重新运行、重新生成产物；求解作业照常经 `solver_submit.sh` 提交），"
        "并在本步骤的日志或报告中逐条写明改了什么、证据在哪；不在的，写明它属于哪一步、为什么本步骤不改。"
        "不要修改这些行的 Status，Step 12 会复核后更新。研究员备注若与这些问题无关，以本段为准。\n"
        + "\n".join(lines)
        + "\n\n"
    )

# The Step every rendered template belongs to, for the final-Judge rework block.
JUDGE_REWORK_TEMPLATES = {
    "step4_model_construction.txt": 4,
    "step5_full_solve.txt": 5,
    "step6_sensitivity.txt": 6,
    "step7_model_eval.txt": 7,
    "step8_visualization.txt": 8,
    "step8_5_reviewer_entry.txt": 8,
    "step9_paper_draft.txt": 9,
    "step10_gate1_numerical.txt": 10,
    "step11_constructive_review.txt": 11,
    "step12_revision.txt": 12,
    "step14_abstract.txt": 14,
    "step15_polish.txt": 15,
}
JUDGE_REWORK_VERDICTS = frozenset({"REOPEN_REVISION_MODEL", "REOPEN_REVISION_TEXT"})
JUDGE_FINDING_CHARS = 1500


def _judge_findings(project: Path, verdict: str) -> list[str] | None:
    """The blocking findings of the Judge run that reopened, or None if unreadable."""

    import json

    try:
        aggregate = json.loads((project / "judge_outputs" / "aggregate.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(aggregate, dict) or aggregate.get("verdict") != verdict:
        return None
    lines: list[str] = []
    for role in aggregate.get("roles") or []:
        if not isinstance(role, dict) or role.get("status") in {None, "PASS"}:
            continue
        try:
            body = json.loads(str(role.get("text") or "").split("\n", 1)[1])
        except (IndexError, ValueError):
            return None
        name = str(role.get("role"))
        if name == "paper":
            picked = [item for item in body.get("issues") or []
                      if isinstance(item, dict) and item.get("severity") in {"blocking", "major"}]
        else:
            evidence = [item for item in body.get("evidence") or [] if isinstance(item, dict)]
            picked = [item for item in evidence if item.get("severity") == "fatal"] or [
                item for item in evidence if item.get("severity") == "risk"]
        for item in picked:
            text = "；".join(str(item[key]) for key in ("claim", "finding", "recommendation") if item.get(key))
            if len(text) > JUDGE_FINDING_CHARS:
                text = text[:JUDGE_FINDING_CHARS] + "……（全文见 judge_evaluation.md）"
            quote = str(item.get("quote") or "")
            lines.append(
                f"- [{name} {item.get('ref_id', '')}，{item.get('severity')}] {text}"
                + (f"（论文引文：{quote}）" if quote else "")
            )
    return lines


def judge_rework_block(project: Path, template: str) -> str:
    """The final-Judge findings a v2 repair cycle is re-running this Step for.

    Present from the final Judge's ``REOPEN_REVISION_*`` reopen until the
    final Judge starts again, for each Step after the reopen target.  Without
    it a re-run Step sees only machine audits that already PASS and has no way
    to learn why it runs (acceptance A, F13).
    """

    step = JUDGE_REWORK_TEMPLATES.get(Path(template).name)
    if step is None:
        return ""
    from ..stages import STAGE_CATALOG_VERSION_V2
    from ..storage import SQLiteStateStore

    store = SQLiteStateStore(project)
    if not store.exists or store.load().stage_catalog_version != STAGE_CATALOG_VERSION_V2:
        return ""
    events = store.events()
    reopen = next(
        (
            event
            for event in reversed(events)
            if event.type == "STEP_REOPENED"
            and event.payload.get("subtask") == "final_judge"
            and event.payload.get("final_decision") in JUDGE_REWORK_VERDICTS
        ),
        None,
    )
    if reopen is None:
        return ""
    if any(
        event.revision > reopen.revision
        and event.type == "STEP_STARTED"
        and event.payload.get("subtask") == "final_judge"
        for event in events
    ):
        return ""
    target = reopen.payload.get("resume_after_step")
    if type(target) is int and step <= target:
        return ""
    verdict = str(reopen.payload["final_decision"])
    findings = _judge_findings(project, verdict)
    if findings is None:
        findings = ["- 未能从 `judge_outputs/aggregate.json` 读出与本次退回一致的评审发现；"
                    "请直接阅读 `judge_evaluation.md` 中各角色的 FAIL/REVISE 依据。"]
    elif not findings:
        findings = ["- 评审未列出 fatal/blocking/major 级发现；请阅读 `judge_evaluation.md` 中各角色的结论。"]
    return (
        "【最终评审返工 — 本次运行的原因】\n"
        f"Step 13 最终评审给出 `{verdict}`（事件 rev {reopen.revision}），引擎因此退回到 Step {target} 之后，"
        "本步骤是这一轮修复的一部分。这是唯一的一次修复：若下一次最终评审仍不通过，就不能正常交付。"
        "下列评审发现就是本次运行要解决的事；只复核现有产物、因机器审计已 PASS 而不改，等于放弃这次修复。"
        "逐条判断根因是否在本步骤的产物中：在的，必须在本步骤从根源修复（模型推导、代码、结果、图、论文正文），"
        "改代码后重新运行并重新生成下游产物（求解作业照常经 `solver_submit.sh` 提交），"
        "并在本步骤的日志或报告中逐条写明改了什么、证据在哪；不在的，写明它属于哪一步、为什么本步骤不改。"
        "完整依据见 `judge_evaluation.md`。研究员备注若与这些发现无关，以本段为准。\n"
        + "\n".join(findings)
        + "\n\n"
    )


V2_PROMPT_OVERLAY: dict[str, tuple[tuple[str, str], ...]] = {
    # The v2 Stage 8 exit routes open results/model review issues back to
    # Steps 5-8 (engine.REVIEW_ISSUE_REWORK_RULE); the re-run Steps read them.
    "step5_full_solve.txt": (
        (
            "修复真相源和执行证据后让 runner 重跑 Step-5 `results` profile 自动更新。",
            "修复真相源和执行证据后让 runner 重跑 Step-5 `results` profile 自动更新。\n" + V2_REVIEW_REWORK_LINE,
        ),
    ),
    "step6_sensitivity.txt": (
        (
            "修复对应产物后由 runner 重跑 Step-6 `results` profile 自动关闭，不要手工改状态。",
            "修复对应产物后由 runner 重跑 Step-6 `results` profile 自动关闭，不要手工改状态。\n" + V2_REVIEW_REWORK_LINE,
        ),
    ),
    "step7_model_eval.txt": (
        ("本步骤只产新文档。", "本步骤只产新文档。\n" + V2_REVIEW_REWORK_LINE),
    ),
    "step8_visualization.txt": (
        ("[重要 — 风格规范]", V2_REVIEW_REWORK_LINE + "\n\n[重要 — 风格规范]"),
    ),
    "step10_gate1_numerical.txt": (
        (
            "Step 13 只做隔离的数学预审，完整的数学、执行和论文质量 Gate 2 由 Step 15 后的 `final` 审计执行。",
            "完整的数学、执行和论文质量 Gate 2 由 Step 13 最终审计执行：它在 Step 14 摘要、Step 15 润色和"
            "内容冻结审批之后，对最终文本运行唯一一次完整三角色 `final` 审计。",
        ),
    ),
    "step11_constructive_review.txt": (
        (
            "Step 13 只做数学预审，完整的数学、执行和论文质量判决由 Step 15 后的 `final` 审计执行。",
            "完整的数学、执行和论文质量判决由 Step 13 最终审计执行：它在 Step 14 摘要、Step 15 润色和"
            "内容冻结审批之后，对最终文本运行唯一一次完整三角色 `final` 审计。",
        ),
    ),
    "step12_revision.txt": (
        (
            "如果 Step 16 消费的 `final` 审计给出 `VERDICT: REOPEN_REVISION_TEXT`",
            "如果 Step 13 最终审计（内容冻结后对最终文本运行的唯一一次完整三角色 `final` 审计）"
            "给出 `VERDICT: REOPEN_REVISION_TEXT`",
        ),
        (
            "Step 13 的 `PRECHECK_PASS` 只是数学预审通过，不是最终 Gate 2 PASS。",
            "本项目没有 Step 13 数学预审：唯一一次完整审计在 Step 14 摘要、Step 15 润色和内容冻结之后运行。",
        ),
        (
            "阶段 PASS 或 Step-13 `PRECHECK_PASS` 都不等于最终交付 PASS。",
            "阶段 PASS 不等于最终交付 PASS。",
        ),
        (
            "如果 Step 11、Step 13 数学预审或最终审计要求新求解",
            "如果 Step 11 或 Step 13 最终审计要求新求解",
        ),
        (
            "—— Step 13 数学预审或最终审计的具体 issue",
            "—— Step 13 最终审计的具体 issue",
        ),
        (
            "## 给 Step 13 数学预审 / 最终审计的提示",
            "## 给 Step 13 最终审计的提示",
        ),
        (
            "让后续数学预审和最终审计知道哪些 issue 已经被处理",
            "让 Step 13 最终审计知道哪些 issue 已经被处理",
        ),
        (
            "供 Step 13 数学预审或最终审计判定（可能触发 `REOPEN_REVISION_MODEL`）。",
            "并在 `audit_issue_ledger.md` 的对应行把 Category 写成 `results`（求解、结果、灵敏度或图生成代码）"
            "或 `model`，Notes 写清要改的文件与具体改法。本项目在本步骤结束时由引擎读取台账中"
            "`## Incremental audit findings` 以外的 OPEN BLOCKING/MAJOR 行：`results`/`model` 类触发一次"
            " Step 5–8 重算（重跑的步骤会读这些行并修复）后回到本步骤复核，其余类别重跑一次 Step 11–12；"
            "之后仍未解决则运行失败。已真正修复的行改为 RESOLVED 并写明证据。",
        ),
        (
            "后续预审或最终审计返修需要 diff",
            "后续最终审计返修需要 diff",
        ),
    ),
    "step14_abstract.txt": (
        (
            "Step 13 数学预审已经执行，或由用户通过原生 `GATE2_CONTINUATION_AUTHORIZED` 事件授权技术覆盖"
            "（项目内 override 文件不是执行授权）；真实 verdict 与未解决问题必须以 `judge_evaluation.md` "
            "和权威事件记录为准。`PRECHECK_PASS` 只表示可继续摘要/润色，完整 Gate 2 尚待 Step 15 后的 "
            "`final` 审计，不得假定 Gate 2 PASS。",
            "本项目的 Step 13 是最终审计：它在本步骤和 Step 15 润色之后、内容冻结审批之后，"
            "对最终文本运行唯一一次完整三角色 `final` 审计，所以此刻还没有任何 Judge 结论，不得假定会通过。"
            "若 `judge_evaluation.md` 存在，它是上一轮最终审计判退后返修时留下的结论，"
            "真实 verdict 与未解决问题以它和权威事件记录为准。",
        ),
        (
            "不得在摘要中把 OPEN/BLOCKING 项、符号 warning 或 `PRECHECK_PASS` 改写成“最终审计通过”",
            "不得在摘要中把 OPEN/BLOCKING 项或符号 warning 改写成“最终审计通过”",
        ),
    ),
    "step15_polish.txt": (
        (
            "—— Step 13 数学预审的真实 verdict 与问题；它不包含最终论文质量评分，不得虚构“写作清晰度”结论。"
            "完整三角色评审在本步骤完成后的 `final` 审计运行。",
            "—— 若存在，是上一轮 Step 13 最终审计判退后返修时留下的真实 verdict 与问题；"
            "不得虚构“写作清晰度”结论。本轮的完整三角色评审在本步骤和内容冻结审批之后由 Step 13 运行。",
        ),
        (
            "完成本步骤后立即停止。Step 16 接力 compile + 打包。",
            "完成本步骤后立即停止。接下来是内容冻结审批，然后 Step 13 最终审计编译并评审最终文本，"
            "Step 16 只做验收与打包。",
        ),
    ),
}


def project_uses_catalog_v2(project: Path) -> bool:
    from ..stages import STAGE_CATALOG_VERSION_V2
    from ..storage import SQLiteStateStore

    store = SQLiteStateStore(project)
    return store.exists and store.load().stage_catalog_version == STAGE_CATALOG_VERSION_V2


def apply_v2_overlay(template: str, text: str) -> str:
    """Rewrite a rendered paper-segment prompt for a v2 project."""

    for old, new in V2_PROMPT_OVERLAY.get(Path(template).name, ()):
        count = text.count(old)
        if count != 1:
            raise ValueError(
                f"Stage catalog v2 prompt overlay for {template} expected one "
                f"occurrence of {old[:40]!r}, found {count}"
            )
        text = text.replace(old, new)
    return text
