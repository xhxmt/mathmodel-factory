<template>
  <section v-if="visible" class="md-panel panel">
    <div class="md-head">
      <div class="md-title">
        <Icon name="git-branch" :size="15" />
        <span>建模方向</span>
        <span v-if="directions.length" class="md-count mono">{{ directions.length }}</span>
      </div>
      <button class="btn btn-icon btn-ghost btn-sm" @click="load" :disabled="loading" title="刷新">
        <Icon name="refresh" :size="13" :class="{ spin: loading }" />
      </button>
    </div>

    <div v-if="loading" class="md-loading">
      <div v-for="i in 3" :key="i" class="md-skel"></div>
    </div>

    <div v-else-if="!available" class="md-empty">
      <Icon name="clock" :size="18" />
      <span>{{ message || '等待题目解析完成' }}</span>
    </div>

    <div v-else class="md-grid">
      <article
        v-for="direction in directions"
        :key="direction.id"
        class="md-card"
        :class="{ selected: selectedId === direction.id, saving: savingId === direction.id }"
      >
        <div class="card-top">
          <span class="rank mono">#{{ direction.rank }}</span>
          <span class="domain mono">{{ direction.domain || 'method' }}</span>
        </div>
        <h3>{{ direction.title }}</h3>
        <div class="method mono">{{ direction.method_path }}</div>
        <div class="evidence">
          <span class="evidence-level mono">证据 {{ evidenceLabel(direction.evidence_level) }}</span>
          <span class="mono">数据 {{ percent(direction.data_coverage) }}</span>
          <span class="mono">样本 {{ direction.historical_samples || 0 }}</span>
        </div>
        <p class="rationale">{{ direction.rationale }}</p>
        <div class="risk mono">{{ riskText(direction) }}</div>
        <button class="btn btn-sm" :class="selectedId === direction.id ? 'btn-ghost' : 'btn-amber'" :disabled="Boolean(savingId)" @click="select(direction)">
          <span v-if="savingId === direction.id" class="spinner sm"></span>
          <template v-else-if="selectedId === direction.id"><Icon name="check" :size="13" /> 已选</template>
          <template v-else><Icon name="check-circle" :size="13" /> 选择</template>
        </button>
      </article>
    </div>

    <div v-if="error" class="md-error">
      <Icon name="alert-triangle" :size="13" />
      <span>{{ error }}</span>
    </div>
  </section>
</template>

<script>
import { onMounted, ref, watch } from 'vue'
import Icon from './Icon.vue'
import { Projects } from '../lib/api.js'
import { useToasts } from '../composables/useToasts.js'

export default {
  name: 'ModelingDirectionPanel',
  components: { Icon },
  props: {
    base: { type: String, required: true },
    currentStep: { type: Number, default: 0 },
  },
  emits: ['changed'],
  setup(props, { emit }) {
    const toasts = useToasts()
    const visible = ref(true)
    const available = ref(false)
    const loading = ref(false)
    const directions = ref([])
    const selectedId = ref('')
    const savingId = ref('')
    const message = ref('')
    const error = ref('')

    async function load() {
      if (!props.base) return
      loading.value = true
      error.value = ''
      try {
        const payload = await Projects.modelingDirections(props.base)
        available.value = Boolean(payload.available)
        directions.value = Array.isArray(payload.directions) ? payload.directions : []
        selectedId.value = payload.selected_direction_id || ''
        message.value = payload.message || ''
        visible.value = props.currentStep <= 1 || Boolean(selectedId.value) || directions.value.length > 0
      } catch (err) {
        error.value = err.response?.data?.detail || '建模方向加载失败'
        visible.value = props.currentStep <= 1
      } finally {
        loading.value = false
      }
    }

    async function select(direction) {
      if (!direction?.id || savingId.value) return
      savingId.value = direction.id
      error.value = ''
      try {
        await Projects.selectModelingDirection(props.base, direction.id)
        selectedId.value = direction.id
        toasts.success(`${direction.title} 已写入人工指令`, '建模方向')
        emit('changed')
      } catch (err) {
        error.value = err.response?.data?.detail || '建模方向保存失败'
      } finally {
        savingId.value = ''
      }
    }

    function riskText(direction) {
      const risks = Array.isArray(direction?.risks) ? direction.risks.filter(Boolean) : []
      return risks.length ? `风险: ${risks.join(' / ')}` : '风险: 无硬阻塞'
    }

    function evidenceLabel(level) {
      return ({ strong: '强', moderate: '中', weak: '弱', none: '无' })[level] || '无'
    }

    function percent(value) {
      const numeric = Number(value)
      return Number.isFinite(numeric) ? `${Math.round(numeric * 100)}%` : '0%'
    }

    watch(() => props.base, load)
    watch(() => props.currentStep, (step) => {
      visible.value = step <= 1 || Boolean(selectedId.value)
    })
    onMounted(load)

    return {
      visible,
      available,
      loading,
      directions,
      selectedId,
      savingId,
      message,
      error,
      load,
      select,
      riskText,
      evidenceLabel,
      percent,
    }
  },
}
</script>

<style scoped>
.md-panel { display: flex; flex-direction: column; gap: 12px; padding: 14px; }
.md-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.md-title { display: flex; align-items: center; gap: 8px; font-weight: 800; font-size: 13px; }
.md-count { padding: 2px 6px; border: 1px solid var(--line); border-radius: var(--r-sm); color: var(--ink-3); font-size: 10px; }
.spin { animation: spin 0.7s linear infinite; }
.md-loading { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.md-skel { min-height: 156px; border: 1px solid var(--line); border-radius: var(--r); background: linear-gradient(90deg, var(--panel-2), var(--panel-3), var(--panel-2)); background-size: 220% 100%; animation: sk 1.2s infinite linear; }
@keyframes sk { to { background-position: -220% 0; } }
.md-empty { display: flex; align-items: center; gap: 8px; min-height: 46px; padding: 10px 12px; border: 1px dashed var(--line-2); border-radius: var(--r); color: var(--ink-3); font-size: 12.5px; }
.md-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.md-card { min-height: 255px; display: flex; flex-direction: column; gap: 9px; padding: 12px; border: 1px solid var(--line); border-radius: var(--r); background: var(--panel-2); transition: border-color 0.16s var(--ease), background 0.16s var(--ease); }
.md-card.selected { border-color: var(--ok); background: var(--ok-dim); }
.md-card.saving { opacity: 0.78; }
.card-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.rank { color: var(--amber); font-weight: 900; font-size: 11px; }
.domain { color: var(--ink-3); font-size: 10px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
h3 { margin: 0; font-size: 14px; line-height: 1.28; letter-spacing: 0; }
.method { min-height: 28px; color: var(--ink-3); font-size: 10px; line-height: 1.35; overflow-wrap: anywhere; }
.evidence { display: flex; flex-wrap: wrap; gap: 6px; color: var(--ink-2); font-size: 10.5px; }
.evidence span { padding: 3px 6px; border: 1px solid var(--line); border-radius: var(--r-sm); background: var(--panel-3); }
.evidence-level { color: var(--amber); }
.rationale { flex: 1; margin: 0; color: var(--ink-2); font-size: 11.5px; line-height: 1.45; }
.risk { min-height: 28px; color: var(--ink-3); font-size: 10.5px; line-height: 1.35; overflow-wrap: anywhere; }
.spinner.sm { width: 13px; height: 13px; border-top-color: currentColor; }
.md-error { display: flex; align-items: center; gap: 7px; padding: 9px 10px; border: 1px solid var(--bad); border-radius: var(--r-sm); background: var(--bad-dim); color: var(--bad); font-size: 12px; }
@media (max-width: 980px) {
  .md-grid, .md-loading { grid-template-columns: 1fr; }
  .md-card { min-height: auto; }
}
</style>
