import { needsHuman, humanLabel, executionStatus, stepText } from './projectState.js'

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

export function workspaceTabs({ consultationPending = false, selectionPending = false, diagnostics = null, cloudEnabled = false } = {}) {
  const hasDiagnostics = Boolean(diagnostics?.status?.reason_code)
  const tabs = [
    { key: 'overview', label: '概览', icon: 'activity' },
    { key: 'pipeline', label: '流水线', icon: 'layers' },
    { key: 'plan', label: '任务图', icon: 'git-branch' },
    { key: 'logs', label: '日志', icon: 'terminal' },
    { key: 'artifacts', label: '产物', icon: 'folder' },
    { key: 'evidence', label: '证据', icon: 'shield' },
    { key: 'delivery', label: '交付', icon: 'package' },
    { key: 'solver', label: '求解任务', icon: 'cpu' },
    { key: 'diagnostics', label: '诊断', icon: 'alert-triangle', attention: hasDiagnostics },
    { key: 'cloud', label: '云端', icon: 'zap', attention: cloudEnabled },
  ]
  if (consultationPending) {
    const deliveryIndex = tabs.findIndex((tab) => tab.key === 'delivery')
    tabs.splice(deliveryIndex, 0, { key: 'consultation', label: '咨询', icon: 'message-square', attention: true })
  }
  if (selectionPending) {
    const diagnosticsIndex = tabs.findIndex((tab) => tab.key === 'diagnostics')
    tabs.splice(diagnosticsIndex, 0, { key: 'selection', label: '人工决策', icon: 'git-branch', attention: true })
  }
  return tabs
}

export const CONTEST_PHASES = [
  { id: 1, key: 'problem_understanding', label: '题意与数据', steps: [0, 1] },
  { id: 2, key: 'model_tournament', label: '模型竞赛', steps: [2, 3], humanGate: 'step3' },
  { id: 3, key: 'model_and_solve', label: '建模与求解', steps: [4, 5] },
  { id: 4, key: 'validation', label: '结果验证', steps: [6, 7] },
  { id: 5, key: 'paper_construction', label: '论文构建', steps: [8, '8_5', 9] },
  { id: 6, key: 'deterministic_paper_audit', label: '确定性审计', steps: [10] },
  { id: 7, key: 'review_and_revision', label: '审稿与修订', steps: [11, 12, 13, 14, 15], humanGate: 'content_freeze' },
  { id: 8, key: 'final_audit_and_delivery', label: '最终交付', steps: [16] },
]

export function phaseForStep(step) {
  const key = step === '8_5' ? '8_5' : Number(step)
  return CONTEST_PHASES.find((phase) => phase.steps.includes(key)) || CONTEST_PHASES[0]
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

export function buildCloudTaskPanel(status = {}, projectConfig = {}) {
  const solvers = projectConfig.solver_types?.length
    ? projectConfig.solver_types
    : status.solvers || []
  return {
    available: Boolean(status.available),
    enabled: Boolean(projectConfig.enabled),
    region: status.region || projectConfig.region || 'N/A',
    service: status.service || status.service_name || projectConfig.service_name || 'N/A',
    threshold: projectConfig.threshold_time || 300,
    solvers,
    badges: [
      status.available ? 'Cloud Run 可用' : 'Cloud Run 不可用',
      projectConfig.enabled ? '本项目已启用' : '本项目未启用',
      solvers.length ? solvers.join(', ') : '无求解器配置',
    ],
  }
}
