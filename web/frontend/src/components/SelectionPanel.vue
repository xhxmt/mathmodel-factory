<template>
  <section class="sel-panel panel" :class="`gate-${gate}`">
    <div class="sel-head">
      <div class="sel-title">
        <Icon :name="gateMeta.icon" :size="16" />
        <div><span>{{ gateMeta.title }}</span><small>{{ gateMeta.subtitle }}</small></div>
      </div>
      <div class="head-meta">
        <span v-if="request.generation" class="revision mono">GEN {{ request.generation }}</span>
        <span class="revision mono">REV {{ revision ?? '—' }}</span>
        <button class="btn btn-icon btn-ghost btn-sm" @click="load" :disabled="loading" title="刷新">
          <Icon name="refresh" :size="13" :class="{ spin: loading }" />
        </button>
      </div>
    </div>

    <div v-if="loading" class="empty">正在读取不可变决策请求…</div>
    <div v-else-if="decision" class="decision-done">
      <Icon name="check-circle" :size="20" />
      <div>
        <strong>该人工决策已写入，不可修改</strong>
        <span>{{ decision.selected_primary || decision.selected_option_id }} · {{ decision.reason || '已确认' }}</span>
      </div>
    </div>
    <div v-else-if="!available" class="empty">{{ message || '暂无待处理人工节点' }}</div>

    <template v-else>
    <div v-if="request.request_id" class="request-binding mono">
      <span>REQUEST {{ request.request_id }}</span>
      <span>SUBJECT {{ shortHash(request.subject_fingerprint) }}</span>
      <span>OPTIONS {{ shortHash(request.options_fingerprint) }}</span>
    </div>

    <template v-if="gate === 'step3'">
      <div class="gate-note">先看小样证据与失败风险，再确认 PRIMARY；系统不会代替你按分数自动选主线。</div>
      <div class="grid">
        <article v-for="option in options" :key="option.id" class="card" :class="{ selected: selectedOptionId === option.id }">
          <div class="top">
            <span class="rank mono">#{{ option.rank || '—' }}</span>
            <span class="family mono">{{ option.family || 'METHOD' }}</span>
          </div>
          <h3>{{ option.title }}</h3>
          <p>{{ option.summary || '已通过候选流验证。' }}</p>
          <div class="fact-row"><span>小样</span><b>{{ demoStatus(option) }}</b></div>
          <div class="fact-row"><span>预计耗时</span><b>{{ option.estimated_time || '未单独估算' }}</b></div>
          <div class="risk-list">
            <span>主要风险</span>
            <ul><li v-for="risk in risks(option)" :key="risk">{{ risk }}</li></ul>
          </div>
          <div class="evidence-links">
            <button v-for="path in evidence(option)" :key="path" @click="openEvidence(path)"><Icon name="file-text" :size="11" />{{ fileName(path) }}</button>
          </div>
          <button class="btn btn-sm" :class="selectedOptionId === option.id ? 'btn-ghost' : 'btn-amber'" @click="selectedOptionId = option.id">
            <Icon name="check-circle" :size="13" /> {{ selectedOptionId === option.id ? '已设为 PRIMARY' : '设为 PRIMARY' }}
          </button>
        </article>
      </div>
    </template>

    <template v-else-if="gate === 'content_freeze'">
      <div class="freeze-summary">
        <Icon name="lock" :size="19" />
        <div><strong>确认后停止内容探索</strong><span>后续只运行确定性检查、Final Audit、编译、打包与交付。</span></div>
      </div>
      <div class="approval-options">
        <button
          v-for="option in options"
          :key="option.id"
          class="btn btn-sm"
          :class="selectedOptionId === option.id ? 'btn-amber' : 'btn-ghost'"
          @click="selectedOptionId = option.id"
        >{{ option.title }}</button>
      </div>
      <div v-if="!rejecting" class="checklist">
        <label v-for="item in freezeChecks" :key="item.key"><input v-model="confirmations[item.key]" type="checkbox" /><span>{{ item.label }}</span></label>
      </div>
      <div v-else class="reject-note">拒绝会保留当前阻塞，并立即创建下一代审批请求；本次拒绝记录不会被覆盖。</div>
    </template>

    <template v-else>
      <div class="override-warning">
        <Icon name="alert-triangle" :size="21" />
        <div><strong>Delivery Freeze 后回退上游</strong><span>Final Audit 要求从 Step {{ payload.resume_after_step ?? '—' }} 后恢复。回退会使当前快照与发布证据失效，必须重新审计。</span></div>
      </div>
      <label class="override-confirm"><input v-model="confirmations.override" type="checkbox" />我已评估剩余提交时间，并接受重新生成证据与错过截止时间的风险。</label>
    </template>

    <div class="decision-form">
      <label><span>{{ gate === 'step3' ? '选型理由' : gate === 'content_freeze' ? '冻结确认说明' : '强制回退理由' }}</span>
        <textarea v-model="reason" class="field" rows="3" :placeholder="gateMeta.placeholder"></textarea>
      </label>
      <div class="decision-footer">
        <span>提交后写入 append-only SQLite 决策，不能在 Web 中覆盖。</span>
        <button class="btn btn-amber" :disabled="!ready || submitting" @click="submitDecision">
          <Icon name="lock" :size="13" /> {{ submitting ? '正在写入…' : submitLabel }}
        </button>
      </div>
    </div>
    </template>
    <div v-if="error" class="error">{{ error }}</div>
  </section>
</template>

<script>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import Icon from './Icon.vue'
import { Projects } from '../lib/api.js'
import { useToasts } from '../composables/useToasts.js'

const META = {
  step3: { title: 'Human Gate 1 · 方法主线', subtitle: '候选小样完成后人工选择 PRIMARY', icon: 'git-branch', button: '确认模型主线', placeholder: '说明为什么该主线在正确性、可行性、时间风险上最适合本题…' },
  content_freeze: { title: 'Human Gate 2 · 内容冻结', subtitle: '人工核对主结论、摘要、核心图表与附件', icon: 'lock', button: '确认内容冻结', placeholder: '记录已复核的主结论和仍需在交付阶段关注的事项…' },
  delivery_freeze_override: { title: '交付冻结回退授权', subtitle: '仅用于 Final Audit 要求重大上游修改', icon: 'alert-triangle', button: '授权回退并重审', placeholder: '说明必须回退的阻塞问题、修改范围和剩余时间评估（必填）…' },
}

export default {
  name: 'SelectionPanel',
  components: { Icon },
  props: {
    base: { type: String, required: true },
    revision: { type: Number, default: null },
  },
  emits: ['changed', 'open-file'],
  setup(props, { emit }) {
    const toasts = useToasts()
    const loading = ref(false)
    const submitting = ref(false)
    const payload = ref({})
    const selectedOptionId = ref('')
    const reason = ref('')
    const error = ref('')
    const confirmations = reactive({ conclusion: false, abstract: false, figures: false, attachments: false, override: false })
    const gate = computed(() => payload.value?.gate || 'step3')
    const gateMeta = computed(() => META[gate.value] || META.step3)
    const available = computed(() => Boolean(payload.value?.available))
    const options = computed(() => Array.isArray(payload.value?.options) ? payload.value.options : [])
    const decision = computed(() => payload.value?.decision || null)
    const request = computed(() => payload.value?.request || {})
    const message = computed(() => payload.value?.message || '')
    const selectedOption = computed(() => options.value.find((item) => item.id === selectedOptionId.value))
    const rejecting = computed(() => String(selectedOptionId.value).startsWith('reject'))
    const submitLabel = computed(() => rejecting.value ? '拒绝并保持内容开放' : gateMeta.value.button)
    const freezeChecks = [
      { key: 'conclusion', label: '主结论与 canonical results 完全一致' },
      { key: 'abstract', label: '摘要中的数字、方法和结论已人工通读' },
      { key: 'figures', label: '核心图表可读、编号正确且能支撑论点' },
      { key: 'attachments', label: '页数、必交代码与附件已经核对' },
    ]
    const ready = computed(() => {
      if (!selectedOption.value || reason.value.trim().length < 8) return false
      if (gate.value === 'content_freeze') {
        return rejecting.value || freezeChecks.every((item) => confirmations[item.key])
      }
      if (gate.value === 'delivery_freeze_override') return confirmations.override
      return true
    })

    const fileName = (path) => String(path || '').split('/').pop() || path
    const shortHash = (value) => String(value || '—').slice(0, 12)
    const evidence = (option) => Array.isArray(option?.evidence_files) ? option.evidence_files : Array.isArray(option?.evidence) ? option.evidence : []
    const risks = (option) => Array.isArray(option?.main_tradeoffs) && option.main_tradeoffs.length ? option.main_tradeoffs.slice(0, 3) : ['未记录阻塞性批评']
    function demoStatus(option) {
      const line = Array.isArray(option?.why_high_ranked) ? option.why_high_ranked.find((item) => /demo status/i.test(item)) : ''
      return line ? line.replace(/^demo status:\s*/i, '').replace(/\.$/, '') : option?.validated ? 'VALIDATED' : '待核对'
    }
    function openEvidence(path) {
      emit('open-file', { path, name: fileName(path), type: path.endsWith('.json') ? 'json' : 'markdown' })
    }
    async function load() {
      if (!props.base) return
      loading.value = true
      error.value = ''
      try {
        payload.value = await Projects.selection(props.base)
        selectedOptionId.value = payload.value?.selected_option_id || payload.value?.default_option_id || ''
        reason.value = ''
        Object.keys(confirmations).forEach((key) => { confirmations[key] = false })
      } catch (err) {
        payload.value = {}
        error.value = err.response?.data?.detail || '人工决策加载失败'
      } finally { loading.value = false }
    }
    async function submitDecision() {
      if (!ready.value || submitting.value) return
      submitting.value = true
      error.value = ''
      try {
        await Projects.selectOption(props.base, {
          gate: gate.value,
          selected_option_id: selectedOptionId.value,
          selected_aux_id: selectedOption.value?.recommended_aux || 'NONE',
          reason: reason.value.trim(),
          confirmations: gate.value === 'content_freeze'
            ? freezeChecks.filter((item) => confirmations[item.key]).map((item) => item.key)
            : gate.value === 'delivery_freeze_override' && confirmations.override ? ['override_risk_accepted'] : [],
          expected_revision: props.revision,
          request_id: request.value.request_id || null,
          generation: request.value.generation || null,
          subject_fingerprint: request.value.subject_fingerprint || null,
          options_fingerprint: request.value.options_fingerprint || null,
        })
        toasts.success(`${gateMeta.value.title} 已写入不可变决策`, '人工节点')
        emit('changed')
        await load()
      } catch (err) {
        error.value = err.response?.data?.detail || '人工决策保存失败'
      } finally { submitting.value = false }
    }

    watch(() => props.base, load)
    onMounted(load)
    return { loading, submitting, payload, selectedOptionId, reason, error, confirmations, gate, gateMeta, available, options, decision, request, message, freezeChecks, ready, rejecting, submitLabel, fileName, shortHash, evidence, risks, demoStatus, openEvidence, load, submitDecision }
  },
}
</script>

<style scoped>
.sel-panel { display: flex; flex-direction: column; gap: 13px; padding: 16px; }
.sel-head, .sel-title, .head-meta { display: flex; align-items: center; }
.sel-head { justify-content: space-between; gap: 12px; }
.sel-title { gap: 9px; font-weight: 800; font-size: 13px; }
.sel-title div { display: flex; flex-direction: column; gap: 2px; }
.sel-title small { color: var(--ink-3); font-size: 9.5px; font-weight: 500; }
.head-meta { gap: 7px; }
.revision { padding: 3px 6px; border: 1px solid var(--line); border-radius: var(--r-xs); color: var(--ink-3); font-size: 9px; }
.request-binding { display: flex; flex-wrap: wrap; gap: 7px 12px; padding: 8px 10px; border: 1px solid var(--line); border-radius: var(--r-sm); background: var(--panel-2); color: var(--ink-3); font-size: 9px; }
.grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.card { display: flex; flex-direction: column; gap: 9px; padding: 12px; border: 1px solid var(--line); border-radius: var(--r); background: var(--panel-2); }
.card.selected { border-color: var(--ok); background: var(--ok-dim); }
.top, .fact-row { display: flex; justify-content: space-between; gap: 8px; }
.rank { color: var(--amber); font-weight: 900; font-size: 11px; }
.family { color: var(--ink-3); font-size: 10px; }
h3 { margin: 0; font-size: 14px; }
.card p { margin: 0; color: var(--ink-2); font-size: 11px; line-height: 1.45; }
.fact-row { padding-top: 6px; border-top: 1px solid var(--line); font-size: 10px; }
.fact-row span, .risk-list > span { color: var(--ink-3); }
.fact-row b { font-size: 10px; }
.risk-list { font-size: 10px; }
.risk-list ul { margin: 5px 0 0; padding-left: 16px; color: var(--ink-2); }
.evidence-links { display: flex; flex-wrap: wrap; gap: 5px; }
.evidence-links button { display: inline-flex; align-items: center; gap: 3px; max-width: 100%; padding: 3px 5px; border: 1px solid var(--line); border-radius: var(--r-xs); background: var(--bg-2); color: var(--live); font: 9px var(--mono); cursor: pointer; overflow: hidden; text-overflow: ellipsis; }
.gate-note, .freeze-summary, .override-warning, .decision-done { padding: 11px 12px; border: 1px solid var(--line); border-radius: var(--r); background: var(--panel-2); color: var(--ink-2); font-size: 11px; }
.freeze-summary, .override-warning, .decision-done { display: flex; align-items: center; gap: 10px; }
.freeze-summary div, .override-warning div, .decision-done div { display: flex; flex-direction: column; gap: 3px; }
.freeze-summary strong, .override-warning strong, .decision-done strong { color: var(--ink); font-size: 12px; }
.override-warning { border-color: var(--bad); background: var(--bad-dim); color: var(--bad); }
.decision-done { border-color: var(--ok); background: var(--ok-dim); color: var(--ok); }
.checklist { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.approval-options { display: flex; flex-wrap: wrap; gap: 8px; }
.reject-note { padding: 10px 12px; border: 1px solid var(--bad); border-radius: var(--r-sm); background: var(--bad-dim); color: var(--bad); font-size: 11px; }
.checklist label, .override-confirm { display: flex; align-items: flex-start; gap: 8px; padding: 10px; border: 1px solid var(--line); border-radius: var(--r-sm); background: var(--panel-2); color: var(--ink-2); font-size: 11px; }
input[type='checkbox'] { accent-color: var(--amber); }
.decision-form { padding-top: 12px; border-top: 1px solid var(--line); }
.decision-form label > span { display: block; margin-bottom: 6px; color: var(--ink-2); font-size: 11px; font-weight: 700; }
textarea { width: 100%; resize: vertical; line-height: 1.5; }
.decision-footer { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-top: 8px; }
.decision-footer > span { color: var(--ink-3); font-size: 9.5px; }
.empty, .error { padding: 10px 12px; border: 1px dashed var(--line-2); border-radius: var(--r); color: var(--ink-3); font-size: 12px; }
.error { border-style: solid; border-color: var(--bad); background: var(--bad-dim); color: var(--bad); }
.spin { animation: spin .7s linear infinite; }
@media (max-width: 980px) { .grid { grid-template-columns: 1fr; } }
@media (max-width: 680px) {
  .checklist { grid-template-columns: 1fr; }
  .decision-footer { align-items: stretch; flex-direction: column; }
}
</style>
