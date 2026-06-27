export const DIAGNOSTIC_ACTION_LABEL = {
  open_runner_log: '查看 runner.log',
  open_entry_gate: '查看 entry_gate.md',
  open_reviewer_entry_artifacts: '查看 8.5 入口材料',
  open_consultation_request: '查看咨询请求',
  open_human_review: '查看 human_review.md',
  open_failed_artifact: '查看失败产物',
  refresh_status: '刷新诊断',
  resume_project: '恢复运行',
}

export function badgeText(project) {
  return project.diagnostic_badge || ''
}

export function actionLabel(actionId) {
  return DIAGNOSTIC_ACTION_LABEL[actionId] || actionId
}
