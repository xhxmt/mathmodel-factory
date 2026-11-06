export const DIAGNOSTIC_ACTION_LABEL = {
  open_runner_log: '查看 runner.log',
  open_entry_gate: '查看 entry_gate.md',
  open_reviewer_entry_artifacts: '查看 8.5 入口材料',
  open_consultation_request: '查看咨询请求',
  open_human_review: '查看 human_review.md',
  open_failed_artifact: '查看失败产物',
  open_gate_evidence: '查看门禁证据',
  open_audit_timeline: '查看审计时间线',
  retry_human_decision_commit: '重新提交人工决策',
  refresh_status: '刷新诊断',
  resume_project: '恢复运行',
  revert_upstream_edits_and_resume: '已撤销上游改动，恢复运行',
  grant_semantic_reopen: '批准一次回退',
  retry_failed_step: '重试失败的步骤',
}

// A run paused because its semantic-reopen budget is spent (phase 3a): one
// line per fact the operator needs to choose between reverting and granting.
export function reopenBudgetLines(budget) {
  if (!budget || typeof budget !== 'object') return []
  const which = budget.exhausted === 'run' ? '整次运行的回退总数' : `Stage ${budget.stage} 发起的回退次数`
  const lines = [
    `已用尽：${which}`,
    `Stage ${budget.stage}：${budget.stage_count}/${budget.stage_limit} · 整次运行：${budget.run_count}/${budget.run_limit}`,
  ]
  for (const cause of budget.causes || []) {
    lines.push(`待回退原因：${cause.cause_artifact || '(未知文件)'} · ${cause.flag} · 归属 Stage ${cause.owner_stage}`)
  }
  for (const item of budget.history || []) {
    lines.push(`已发生：r${item.revision} · Stage ${item.stage} → Stage ${item.owner_stage}`)
  }
  return lines
}

export function badgeText(project) {
  return project.diagnostic_badge || ''
}

export function actionLabel(actionId) {
  return DIAGNOSTIC_ACTION_LABEL[actionId] || actionId
}
