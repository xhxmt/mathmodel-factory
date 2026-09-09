import assert from 'node:assert/strict'
import test from 'node:test'
import { buildJointConsultationWorkflow, jointResponseBinding } from '../src/lib/jointModeling.js'

const request = {
  joint_modeling: true,
  request: { request_id: 'req-a', generation: 1, subject_fingerprint: 'sha-a', options_fingerprint: 'options-a' },
  attestations_required: ['new_conversation_used', 'copied_without_editing'],
  key_files: ['m1_spec.md'],
}
const answer = JSON.stringify({ request_id: 'req-a', generation: 1, subject_fingerprint: 'sha-a', summary: 'Review' })

test('manual attestations never default to enabled', () => {
  assert.equal(buildJointConsultationWorkflow(request, answer).ready, false)
  assert.deepEqual(jointResponseBinding(request).attestations, { new_conversation_used: false, copied_without_editing: false })
})
test('a reply for an old request cannot become ready after a refresh', () => {
  const confirmations = { new_conversation_used: true, copied_without_editing: true }
  assert.equal(buildJointConsultationWorkflow(request, answer, confirmations).ready, true)
  assert.equal(buildJointConsultationWorkflow({ ...request, request: { ...request.request, request_id: 'req-b' } }, answer, confirmations).ready, false)
})
test('binding preserves the rendered request identity and explicit true values only', () => {
  const binding = jointResponseBinding(request, { new_conversation_used: 'true', copied_without_editing: true })
  assert.equal(binding.request_id, 'req-a')
  assert.equal(binding.options_fingerprint, 'options-a')
  assert.equal(binding.attestations.new_conversation_used, false)
  assert.equal(binding.attestations.copied_without_editing, true)
})
test('a risk review requires the additional human specification approval', () => {
  const risk = { ...request, attestations_required: [...request.attestations_required, 'selected_model_spec_approved'] }
  assert.equal(buildJointConsultationWorkflow(risk, answer, { new_conversation_used: true, copied_without_editing: true }).ready, false)
})
