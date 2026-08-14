const SOLVER_STATUS_LABELS = {
  RUNNING: '运行中',
  COMPLETED: '已完成',
  FAILED: '已失败',
  TIMEOUT: '已超时',
  EXITED: '已退出',
  CANCELLED: '已取消',
}

const RECEIPT_CLAIM_LABELS = {
  LEGACY_JOB_METADATA_ONLY: '仅有旧版任务元数据',
  EXECUTION_IDENTITY_AND_DECLARED_OUTPUTS_ONLY_NO_OPTIMALITY_PROOF:
    '仅证明执行身份与声明输出，不构成最优性证明',
}

const RECEIPT_ERROR_LABELS = {
  MISSING_TWO_STAGE_RECEIPT: '缺少两阶段凭证',
  COMPLETION_RECEIPT_MISSING: '缺少完成阶段凭证',
  COMPLETION_DID_NOT_PRODUCE_TRUSTED_OUTPUTS: '完成阶段未生成可信输出',
  CURRENT_OUTPUTS_DIFFER_FROM_COMPLETION_RECEIPT: '当前输出与完成凭证不一致',
  SUBMITTED_CODE_OR_INPUTS_CHANGED: '提交后的代码或输入已发生变化',
  SUBMISSION_RECEIPT_EVENT_MISSING: '缺少提交凭证事件',
  SUBMISSION_RECEIPT_EVENT_HASH_MISMATCH: '提交凭证事件哈希不匹配',
  COMPLETION_RECEIPT_EVENT_MISSING: '缺少完成凭证事件',
  COMPLETION_RECEIPT_EVENT_HASH_MISMATCH: '完成凭证事件哈希不匹配',
}

function normalizedCode(value) {
  return String(value || '').trim().toUpperCase()
}

export function solverStatusLabel(value) {
  const code = normalizedCode(value)
  return SOLVER_STATUS_LABELS[code] || value || '未知'
}

export function receiptClaimLabel(value) {
  const code = normalizedCode(value)
  return RECEIPT_CLAIM_LABELS[code] || value || '凭证未就绪'
}

export function receiptErrorLabel(value) {
  const code = normalizedCode(value)
  return RECEIPT_ERROR_LABELS[code] || value || '未知凭证错误'
}
