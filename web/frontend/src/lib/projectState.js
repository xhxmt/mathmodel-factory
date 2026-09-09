import { stepByIndex } from './steps.js'
import { statusLabel } from './status.js'

export const STAGES = [
  { id: 1, name: '题意与数据', steps: [0, 1] },
  { id: 2, name: '候选与选模', steps: [2, 3] },
  { id: 3, name: '模型构建', steps: [4] },
  { id: 4, name: '模型求解', steps: [5] },
  { id: 5, name: '验证与评价', steps: [6, 7] },
  { id: 6, name: '图表与入口', steps: [8] },
  { id: 7, name: '初稿与核验', steps: [9, 10] },
  { id: 8, name: '评审与修订', steps: [11, 12, 13] },
  { id: 9, name: '摘要与定稿', steps: [14, 15] },
  { id: 10, name: '终审与交付', steps: [16] },
]

const index = (value) => value != null && Number.isInteger(Number(value)) && Number(value) >= 0 && Number(value) <= 16 ? Number(value) : null
export function executionStatus(project = {}) { return project.execution_state || project.status || 'unknown' }
export function needsHuman(project = {}) { return Boolean(project.consultation_pending || project.selection_pending) }
export function statusTone(project = {}) {
  return { running: 'live', retrying: 'amber', awaiting_consultation: 'amber', awaiting_selection: 'amber',
    completed: 'ok', paused: 'paused', failed: 'bad', interrupted: 'bad', killed: 'bad', archiving: 'live' }[executionStatus(project)] || ''
}
export function currentStepIndex(project = {}) {
  if (executionStatus(project) === 'completed') return 16
  const source = index(project.source_step_id)
  if (source !== null) return source
  if (project.last_completed_step != null) return Math.min(16, Math.max(0, Number(project.last_completed_step) + 1))
  // Legacy API: current_step is a completed checkpoint. Native status has source_step_id.
  return Math.min(16, Math.max(0, Number(project.current_step ?? -1) + 1))
}
export function completedStepIndex(project = {}) {
  if (project.last_completed_step != null) return Number(project.last_completed_step)
  if (executionStatus(project) === 'completed') return 16
  return project.source_step_id != null ? currentStepIndex(project) - 1 : Number(project.current_step ?? -1)
}
export function projectProgress(project = {}) {
  if (executionStatus(project) === 'completed') return 100
  return Math.min(99, Math.max(0, Math.round((completedStepIndex(project) + 1) / 17 * 100)))
}
export function currentStage(project = {}) {
  return STAGES.find(stage => stage.id === Number(project.active_stage)) || STAGES.find(stage => stage.steps.includes(currentStepIndex(project)))
}
export function humanLabel(project = {}) {
  if (project.consultation_pending) {
    return { joint_modeling_candidates: '回填 Pro 候选复核', joint_modeling_risk: '回填 Pro 风险复核' }[project.consultation_gate] || '回填人工咨询'
  }
  return { step3: '选择模型主线', content_freeze: '确认内容冻结', delivery_freeze_override: '审核回退请求' }[project.selection_gate] || '处理人工决策'
}
export function stepText(project = {}) {
  if (executionStatus(project) === 'completed') return '全部步骤已完成'
  if (project.active_subtask === 'content_freeze_guard' || project.selection_gate === 'content_freeze') return 'Step 16 前置 · 内容冻结确认'
  if (project.active_subtask === 'reviewer_entry_gate') return 'Step 8.5 · 阅卷入口设计'
  const step = currentStepIndex(project)
  return `Step ${step} · ${stepByIndex(step)?.name || '等待调度'}`
}
export function workflowStepState(step, project = {}) {
  const state = executionStatus(project)
  const active = currentStepIndex(project)
  if (state === 'completed') return 'done'
  if (step === active) {
    if (needsHuman(project)) return 'attention'
    if (['failed', 'interrupted', 'killed'].includes(state)) return 'blocked'
    if (state === 'paused') return 'paused'
    if (state === 'retrying') return 'retrying'
    return state === 'running' || state === 'archiving' ? 'live' : 'pending'
  }
  return step <= completedStepIndex(project) ? 'done' : 'pending'
}
export function primaryControl(project = {}) {
  const state = executionStatus(project)
  if (needsHuman(project)) return { kind: 'navigate', tab: project.selection_pending ? 'selection' : 'consultation', label: humanLabel(project), icon: 'user' }
  if (state === 'interrupted') return { kind: 'navigate', tab: 'diagnostics', label: '核对中断原因', icon: 'alert-triangle' }
  if (state === 'failed') return { kind: 'navigate', tab: 'diagnostics', label: '查看失败原因', icon: 'alert-triangle' }
  if (project.is_running && ['running', 'retrying'].includes(state)) return { kind: 'command', action: 'pause', label: '暂停', icon: 'pause' }
  if (state === 'ready' || state === 'paused') return { kind: 'command', action: 'resume', label: state === 'ready' ? '开始运行' : '恢复运行', icon: 'play' }
  return null
}
export function executionHint(project = {}) {
  if (needsHuman(project)) return humanLabel(project)
  if (executionStatus(project) === 'interrupted') return '运行进程已失联；需核对原进程与恢复条件。'
  if (executionStatus(project) === 'retrying') return '调度器正在既定次数内重试。'
  if (executionStatus(project) === 'completed') return '工作流已结束，交付状态单独核验。'
  return stepText(project)
}
export function evidenceLabel(project = {}) {
  return { VALID: '证据有效', INVALID: '证据已失效', UNAVAILABLE: '尚无审计证据' }[project.evidence_validity] || '证据待核验'
}
export function verdictLabel(project = {}) {
  return { PASS: '审查通过', PRECHECK_PASS: '数学预审通过', REOPEN_REVISION_TEXT: '需修订文本',
    REOPEN_REVISION_MODEL: '需修订模型', INDETERMINATE_REVIEW: '待进一步审查', UNAVAILABLE: '尚无有效结论' }[project.scientific_verdict] || project.scientific_verdict || '尚无有效结论'
}
export function executionLabel(project = {}) { return statusLabel(executionStatus(project), project.display_status) }
