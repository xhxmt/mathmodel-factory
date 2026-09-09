export const jointAttestationLabels = {
  new_conversation_used: '我使用了新的 ChatGPT 对话，并手动选择 Pro 模型',
  no_old_project_context: '本次咨询没有使用旧对话或其他项目上下文',
  exact_upload_manifest_used: '我提供了本次咨询包中的全部材料，没有增删',
  copied_without_editing: '我完整回填了 Pro 原始答复，没有改写意见',
  selected_model_spec_approved: '我已阅读风险复核结果，确认当前完整模型规格进入求解',
}

export function jointResponseBinding(request, attestations = {}) {
  const identity = request?.request || {}
  return {
    request_id: identity.request_id,
    generation: identity.generation,
    subject_fingerprint: identity.subject_fingerprint,
    options_fingerprint: identity.options_fingerprint,
    attestations: Object.fromEntries((request?.attestations_required || []).map((key) => [key, attestations[key] === true])),
  }
}

export function buildJointConsultationWorkflow(request, answer, attestations = {}) {
  let parsed = null
  try { parsed = JSON.parse(String(answer || '').trim().replace(/^```(?:json)?\s*\n([\s\S]*?)\n```$/, '$1')) } catch {}
  const identity = request?.request || {}
  const bound = Boolean(parsed && !Array.isArray(parsed) && identity.request_id
    && ['request_id', 'generation', 'subject_fingerprint'].every((key) => parsed[key] === identity[key]))
  const checks = [
    { key: 'request', label: '当前咨询请求已加载', ok: Boolean(identity.request_id) },
    { key: 'json', label: '完整 JSON 答复', ok: Boolean(parsed && !Array.isArray(parsed)) },
    { key: 'binding', label: '答复对应本次请求', ok: bound },
    ...((request?.attestations_required || []).map((key) => ({ key, label: jointAttestationLabels[key] || key, ok: attestations[key] === true }))),
  ]
  return {
    checks, ready: checks.every((item) => item.ok), missing: checks.filter((item) => !item.ok),
    evidence: (request?.key_files || []).map((path) => ({ path, name: path.split('/').pop() })),
  }
}
