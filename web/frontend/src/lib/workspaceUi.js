import { needsHuman, humanLabel, executionStatus, stepText } from './projectState.js'
import { CONTEST_PHASES_V1, contestPhasesFor } from './stageCatalog.js'

export { contestPhasesFor }

export const LOG_LEVELS = [
  { key: 'all', label: '全部' },
  { key: 'err', label: '错误' },
  { key: 'warn', label: '警告' },
  { key: 'info', label: '信息' },
  { key: 'ok', label: '成功' },
]

export function logLineLevel(text) {
  if (/\b(error|fail(ed)?|traceback|exception|fatal)\b|❌/i.test(text)) return 'err'
  if (/\b(warn(ing)?)\b|⚠/i.test(text)) return 'warn'
  if (/\b(ok|success|done|completed|pass(ed)?|converged)\b|✓|✅/i.test(text)) return 'ok'
  if (/^\s*[>$#]/.test(text) || /\b(step|info)\b/i.test(text)) return 'info'
  return ''
}

export function filterLogLines(lines, { query = '', level = 'all' } = {}) {
  const q = query.trim().toLowerCase()
  return (Array.isArray(lines) ? lines : []).filter((line) => {
    const text = String(line.text || '')
    if (level && level !== 'all' && logLineLevel(text) !== level) return false
    if (q && !text.toLowerCase().includes(q)) return false
    return true
  })
}

export function buildLogErrorContext(lines, radius = 2) {
  const safe = Array.isArray(lines) ? lines : []
  const idx = safe.findIndex((line) => logLineLevel(line.text || '') === 'err')
  if (idx === -1) return []
  return safe.slice(Math.max(0, idx - radius), Math.min(safe.length, idx + radius + 1))
}

// Six views.  The step list and the task graph share "流程"; evidence,
// delivery and diagnostics share "质量与交付".  Human decisions get their own
// tab, placed right after the overview, only while one is pending.
export function workspaceTabs({ consultationPending = false, selectionPending = false, diagnostics = null } = {}) {
  const hasDiagnostics = Boolean(diagnostics?.status?.reason_code)
  const tabs = [
    { key: 'overview', label: '概览', icon: 'activity' },
    { key: 'pipeline', label: '流程', icon: 'layers' },
    { key: 'artifacts', label: '文件', icon: 'folder' },
    { key: 'logs', label: '日志', icon: 'terminal' },
    { key: 'solver', label: '求解', icon: 'cpu' },
    { key: 'quality', label: '质量与交付', icon: 'shield', attention: hasDiagnostics },
  ]
  if (consultationPending) tabs.splice(1, 0, { key: 'consultation', label: '回填咨询', icon: 'message-square', attention: true })
  if (selectionPending) tabs.splice(1, 0, { key: 'selection', label: '人工决策', icon: 'git-branch', attention: true })
  return tabs
}

// Earlier tab names still arrive from deep links (?tab=), action items and
// child panels; map each onto the view that now holds it.
export const WORKSPACE_TAB_ALIASES = Object.freeze({
  plan: { tab: 'pipeline', view: 'plan' },
  evidence: { tab: 'quality', view: 'evidence' },
  delivery: { tab: 'quality', view: 'delivery' },
  diagnostics: { tab: 'quality', view: 'diagnostics' },
})

export function resolveWorkspaceTab(key) {
  const alias = WORKSPACE_TAB_ALIASES[key]
  return alias ? { ...alias } : { tab: key, view: null }
}

export const CONTEST_PHASES = CONTEST_PHASES_V1

export function phaseForStep(step, project = null) {
  const key = step === '8_5' ? '8_5' : Number(step)
  const phases = contestPhasesFor(project)
  return phases.find((phase) => phase.steps.includes(key)) || phases[0]
}

export function formatDuration(seconds) {
  const value = Math.max(0, Math.round(Number(seconds) || 0))
  const hours = Math.floor(value / 3600)
  const minutes = Math.floor((value % 3600) / 60)
  if (hours) return `${hours}h ${String(minutes).padStart(2, '0')}m`
  return `${minutes}m`
}

export function timingPresentation(timing = {}) {
  const level = String(timing.risk_level || 'unconfigured')
  const labels = {
    safe: '余量充足',
    guarded: '进入收敛',
    warning: '余量不足',
    critical: '立即收口',
    expired: '比赛已截止',
    unconfigured: '未配置比赛时钟',
  }
  return {
    level,
    label: labels[level] || labels.unconfigured,
    mode: String(timing.mode || 'legacy'),
    modeLabel: String(timing.mode_label || labels.unconfigured),
    recommendation: String(timing.recommendation || ''),
    average: timing.recent_step_average_seconds ? formatDuration(timing.recent_step_average_seconds) : '待积累',
    slack: timing.configured
      ? `${Number(timing.content_slack_seconds || 0) < 0 ? '超出 ' : ''}${formatDuration(Math.abs(Number(timing.content_slack_seconds || 0)))}`
      : '—',
    projectedAt: timing.projected_content_finish_at ? new Date(Number(timing.projected_content_finish_at) * 1000).toLocaleString('zh-CN', { hour12: false }) : '—',
    split: timeSplitPresentation(timing.time_split),
  }
}

// Phase 3e: the contest clock never pauses, so show where the elapsed time
// went: model work versus waiting on a person or an outside provider.
export function timeSplitPresentation(split) {
  if (!split || typeof split !== 'object') return null
  const value = (key) => Math.max(0, Number(split[key] || 0))
  return {
    working: formatDuration(value('working_seconds')),
    waitingExternal: formatDuration(value('waiting_external_seconds')),
    waitingHuman: formatDuration(value('waiting_human_seconds')),
    waitingProvider: formatDuration(value('waiting_provider_seconds')),
    stopped: formatDuration(value('stopped_seconds')),
  }
}

const ACTION_PRIORITY = { expired: 0, critical: 1, warning: 2, guarded: 3, info: 4 }

export function buildWorkspaceActions(dashboard = {}, stepsData = {}, project = {}) {
  const actions = (Array.isArray(dashboard?.actions) ? dashboard.actions : []).map((item) => ({
    ...item,
    severity: String(item?.severity || 'info'),
  }))
  const openIssues = Number(stepsData?.open_issues || 0)
  if (needsHuman(project)) actions.push({ id: 'workflow-human', severity: 'warning', title: humanLabel(project), summary: stepText(project), tab: project.selection_pending ? 'selection' : 'consultation' })
  if (executionStatus(project) === 'interrupted') actions.push({ id: 'workflow-interrupted', severity: 'critical', title: '运行中断，需核对恢复条件', summary: '先确认原进程与子进程的退出情况，再处理恢复。', tab: 'diagnostics' })
  if (executionStatus(project) === 'failed') actions.push({ id: 'workflow-failed', severity: 'warning', title: '本次执行失败', summary: project.reason_summary || '查看错误原因与诊断证据。', tab: 'diagnostics' })
  if (project.evidence_validity === 'INVALID') actions.push({ id: 'workflow-evidence', severity: 'critical', title: '当前证据需要重新核验', summary: '材料或审查绑定已变化，当前交付许可不可用。', tab: 'diagnostics' })
  if (openIssues > 0) {
    actions.push({
      id: 'audit-issues',
      severity: 'warning',
      title: '审计事项待处理',
      summary: `${openIssues} 项未解决事项`,
      tab: 'pipeline',
      file: 'audit_issue_ledger.md',
    })
  }
  const seen = new Set()
  return actions
    .filter((item) => item.id && !seen.has(item.id) && seen.add(item.id))
    .sort((a, b) => (ACTION_PRIORITY[a.severity] ?? 9) - (ACTION_PRIORITY[b.severity] ?? 9) || String(a.id).localeCompare(String(b.id)))
}

const PRIORITY_PATTERNS = [
  { pattern: /(?:^|\/)(.+\.pdf)$/i, score: 100 },
  { pattern: /judge_evaluation\.md$/i, score: 95 },
  { pattern: /review_comments\.md$/i, score: 92 },
  { pattern: /revision_summary\.md$/i, score: 91 },
  { pattern: /solve_log\.md$/i, score: 90 },
  { pattern: /sensitivity_report\.md$/i, score: 88 },
  { pattern: /logs\/runner\.log$/i, score: 86 },
  { pattern: /diagnostics\/status\.json$/i, score: 84 },
]

export function priorityArtifacts(files, currentStep = -1) {
  return (Array.isArray(files) ? files : [])
    .map((file, index) => {
      const path = String(file.path || '')
      const matched = PRIORITY_PATTERNS.find((entry) => entry.pattern.test(path))
      const stepBoost = currentStep >= 5 && /solve_log|sensitivity|runner\.log/i.test(path) ? 4 : 0
      return { ...file, priorityScore: (matched?.score || 0) + stepBoost, originalIndex: index }
    })
    .filter((file) => file.priorityScore > 0)
    .sort((a, b) => b.priorityScore - a.priorityScore || a.originalIndex - b.originalIndex)
}

export function buildConsultationWorkflow(request = {}, answer = '') {
  const trimmed = String(answer || '').trim()
  const evidence = (request.key_files || []).map((path) => ({
    path,
    name: String(path).split('/').pop() || path,
  }))
  const checks = [
    { key: 'content', label: '已阅读决策事项', ok: Boolean(request.content) },
    { key: 'evidence', label: '存在关键证据文件', ok: evidence.length > 0 },
    { key: 'length', label: '结论不少于 80 字', ok: trimmed.length >= 80 },
    { key: 'structure', label: '包含结论或理由', ok: /结论|推荐|理由|原因|取舍/.test(trimmed) },
  ]
  return {
    evidence,
    checks,
    ready: checks.every((check) => check.ok),
    missing: checks.filter((check) => !check.ok),
  }
}

