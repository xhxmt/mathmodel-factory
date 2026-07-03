<template>
  <section class="sel-panel panel">
    <div class="sel-head">
      <div class="sel-title">
        <Icon name="git-branch" :size="15" />
        <span>{{ title }}</span>
        <span v-if="options.length" class="count mono">{{ options.length }}</span>
      </div>
      <button class="btn btn-icon btn-ghost btn-sm" @click="load" :disabled="loading" title="刷新">
        <Icon name="refresh" :size="13" :class="{ spin: loading }" />
      </button>
    </div>

    <div v-if="loading" class="empty">加载中...</div>
    <div v-else-if="!available" class="empty">{{ message || '暂无待选择方案' }}</div>
    <div v-else class="grid">
      <article
        v-for="option in options"
        :key="option.id"
        class="card"
        :class="{ selected: selectedOptionId === option.id }"
      >
        <div class="top">
          <span class="rank mono">#{{ option.rank }}</span>
          <span class="family mono">{{ option.family || 'method' }}</span>
        </div>
        <h3>{{ option.title }}</h3>
        <p>{{ option.summary }}</p>
        <div class="score">
          <span>正确性</span>
          <meter min="0" max="100" :value="score(option, 'correctness')"></meter>
          <b class="mono">{{ score(option, 'correctness') }}</b>
        </div>
        <div class="score">
          <span>可行性</span>
          <meter min="0" max="100" :value="score(option, 'feasibility')"></meter>
          <b class="mono">{{ score(option, 'feasibility') }}</b>
        </div>
        <div class="meta mono">AUX: {{ option.recommended_aux || 'NONE' }}</div>
        <button
          class="btn btn-sm"
          :class="selectedOptionId === option.id ? 'btn-ghost' : 'btn-amber'"
          :disabled="submitting"
          @click="select(option)"
        >
          <Icon name="check-circle" :size="13" /> {{ selectedOptionId === option.id ? '已选' : '选择' }}
        </button>
      </article>
    </div>
    <div v-if="error" class="error">{{ error }}</div>
  </section>
</template>

<script>
import { computed, onMounted, ref, watch } from 'vue'
import Icon from './Icon.vue'
import { Projects } from '../lib/api.js'
import { useToasts } from '../composables/useToasts.js'

export default {
  name: 'SelectionPanel',
  components: { Icon },
  props: { base: { type: String, required: true } },
  emits: ['changed'],
  setup(props, { emit }) {
    const toasts = useToasts()
    const loading = ref(false)
    const submitting = ref(false)
    const payload = ref({})
    const selectedOptionId = ref('')
    const error = ref('')
    const available = computed(() => Boolean(payload.value?.available))
    const options = computed(() => Array.isArray(payload.value?.options) ? payload.value.options : [])
    const message = computed(() => payload.value?.message || '')
    const title = computed(() => payload.value?.gate === 'step4' ? '建模口径选择' : '方法主线选择')

    function score(option, key) {
      return Number(option?.scores?.[key] || 0)
    }

    async function load() {
      if (!props.base) return
      loading.value = true
      error.value = ''
      try {
        payload.value = await Projects.selection(props.base)
        selectedOptionId.value = payload.value?.selected_option_id || payload.value?.default_option_id || ''
      } catch (err) {
        error.value = err.response?.data?.detail || '方案选择加载失败'
      } finally {
        loading.value = false
      }
    }

    async function select(option) {
      if (!option?.id || submitting.value) return
      submitting.value = true
      error.value = ''
      try {
        await Projects.selectOption(props.base, {
          gate: payload.value?.gate || 'step3',
          selected_option_id: option.id,
          selected_aux_id: option.recommended_aux || 'NONE',
          reason: `Selected ${option.id} from Web selection panel`,
        })
        selectedOptionId.value = option.id
        toasts.success(`${option.title} 已写入 Step 3 决策`, '方案选择')
        emit('changed')
      } catch (err) {
        error.value = err.response?.data?.detail || '方案选择保存失败'
      } finally {
        submitting.value = false
      }
    }

    watch(() => props.base, load)
    onMounted(load)
    return { loading, submitting, available, options, selectedOptionId, message, title, error, score, load, select }
  },
}
</script>

<style scoped>
.sel-panel { display: flex; flex-direction: column; gap: 12px; padding: 14px; }
.sel-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.sel-title { display: flex; align-items: center; gap: 8px; font-weight: 800; font-size: 13px; }
.count { padding: 2px 6px; border: 1px solid var(--line); border-radius: var(--r-sm); color: var(--ink-3); font-size: 10px; }
.grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.card { min-height: 235px; display: flex; flex-direction: column; gap: 9px; padding: 12px; border: 1px solid var(--line); border-radius: var(--r); background: var(--panel-2); }
.card.selected { border-color: var(--ok); background: var(--ok-dim); }
.top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.rank { color: var(--amber); font-weight: 900; font-size: 11px; }
.family, .meta { color: var(--ink-3); font-size: 10px; overflow-wrap: anywhere; }
h3 { margin: 0; font-size: 14px; line-height: 1.28; letter-spacing: 0; }
p { flex: 1; margin: 0; color: var(--ink-2); font-size: 11.5px; line-height: 1.45; }
.score { display: grid; grid-template-columns: 42px minmax(0, 1fr) 28px; align-items: center; gap: 8px; font-size: 11px; color: var(--ink-2); }
meter { width: 100%; height: 7px; }
meter::-webkit-meter-bar { background: var(--bg-2); border: 0; border-radius: 999px; }
meter::-webkit-meter-optimum-value { background: var(--amber); border-radius: 999px; }
.score b { color: var(--ink); font-size: 11px; text-align: right; }
.empty { min-height: 46px; padding: 10px 12px; border: 1px dashed var(--line-2); border-radius: var(--r); color: var(--ink-3); font-size: 12.5px; }
.error { padding: 9px 10px; border: 1px solid var(--bad); border-radius: var(--r-sm); background: var(--bad-dim); color: var(--bad); font-size: 12px; }
.spin { animation: spin 0.7s linear infinite; }
@media (max-width: 980px) {
  .grid { grid-template-columns: 1fr; }
  .card { min-height: auto; }
}
</style>
