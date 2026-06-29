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

export function workspaceTabs({ consultationPending = false, diagnostics = null, cloudEnabled = false } = {}) {
  const hasDiagnostics = Boolean(diagnostics?.status?.reason_code)
  return [
    { key: 'overview', label: '概览', icon: 'activity' },
    { key: 'pipeline', label: '流水线', icon: 'layers' },
    { key: 'logs', label: '日志', icon: 'terminal' },
    { key: 'artifacts', label: '产物', icon: 'folder' },
    { key: 'diagnostics', label: '诊断', icon: 'alert-triangle', attention: hasDiagnostics },
    { key: 'consultation', label: '咨询', icon: 'message-square', attention: consultationPending },
    { key: 'cloud', label: '云端', icon: 'zap', attention: cloudEnabled },
  ]
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
