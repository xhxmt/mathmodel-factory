<template>
  <section v-if="visible" class="md-panel panel">
    <div class="md-head">
      <div class="md-title">
        <Icon name="git-branch" :size="15" />
        <span>分层方法召回</span>
        <span v-if="directions.length" class="md-count mono">{{ directions.length }}</span>
      </div>
      <button class="btn btn-icon btn-ghost btn-sm" @click="load" :disabled="loading" title="刷新">
        <Icon name="refresh" :size="13" :class="{ spin: loading }" />
      </button>
    </div>

    <div v-if="loading" class="md-loading">
      <div v-for="i in 3" :key="i" class="md-skel"></div>
    </div>

    <ContentBlockRenderer
      v-else
      :blocks="blocks"
      :selected-id="selectedId"
      :busy-id="savingId"
      @action="onBlockAction"
    />

    <div v-if="error" class="md-error">
      <Icon name="alert-triangle" :size="13" />
      <span>{{ error }}</span>
    </div>
  </section>
</template>

<script>
import { onMounted, ref, watch } from 'vue'
import ContentBlockRenderer from './ContentBlockRenderer.vue'
import Icon from './Icon.vue'
import { Projects } from '../lib/api.js'
import { useToasts } from '../composables/useToasts.js'

export default {
  name: 'ModelingDirectionPanel',
  components: { ContentBlockRenderer, Icon },
  props: {
    base: { type: String, required: true },
    currentStep: { type: Number, default: 0 },
  },
  emits: ['changed'],
  setup(props, { emit }) {
    const toasts = useToasts()
    const visible = ref(true)
    const loading = ref(false)
    const directions = ref([])
    const blocks = ref([])
    const selectedId = ref('')
    const savingId = ref('')
    const error = ref('')

    async function load() {
      if (!props.base) return
      loading.value = true
      error.value = ''
      try {
        const payload = await Projects.modelingDirections(props.base)
        directions.value = Array.isArray(payload.directions) ? payload.directions : []
        blocks.value = Array.isArray(payload.blocks) ? payload.blocks : []
        selectedId.value = payload.selected_direction_id || ''
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
        await load()
        emit('changed')
      } catch (err) {
        error.value = err.response?.data?.detail || '建模方向保存失败'
      } finally {
        savingId.value = ''
      }
    }

    function onBlockAction(action, item) {
      if (action?.id !== 'select_modeling_direction') return
      const directionId = action.payload?.direction_id || item?.id
      const direction = directions.value.find((candidate) => candidate.id === directionId)
      if (direction) select(direction)
    }

    watch(() => props.base, load)
    watch(() => props.currentStep, (step) => {
      visible.value = step <= 1 || Boolean(selectedId.value)
    })
    onMounted(load)

    return {
      visible,
      loading,
      directions,
      blocks,
      selectedId,
      savingId,
      error,
      load,
      select,
      onBlockAction,
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
.md-error { display: flex; align-items: center; gap: 7px; padding: 9px 10px; border: 1px solid var(--bad); border-radius: var(--r-sm); background: var(--bad-dim); color: var(--bad); font-size: 12px; }
@media (max-width: 980px) {
  .md-loading { grid-template-columns: 1fr; }
}
</style>
