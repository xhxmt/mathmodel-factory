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
    "- 审稿返工：读取 `audit_issue_ledger.md` 中 `## Incremental audit findings` 以外各段状态为 OPEN 的 "
    "BLOCKING/MAJOR 审稿行。本步骤若是因这些行被引擎重算（事件 `rework_rule=review_issue`），凡根因在"
    "本步骤产物（代码、结果、图、报告）的行，在本步骤按 Notes 修复并在本步骤日志/报告中写明修复证据；"
    "不在本步骤范围的不要改，也不要改这些行的状态（由 Step 12 复核后更新）。"
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
