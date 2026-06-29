<template>
  <div class="cloud-panel panel">
    <div class="cp-head">
      <div class="cp-title">
        <Icon name="zap" :size="15" />
        <span class="label">GCP 云端任务</span>
      </div>
      <div class="cp-actions">
        <button class="btn btn-sm btn-ghost" @click="refresh" title="刷新云端状态">
          <Icon name="refresh" :size="13" :class="{ spin: loading }" />
        </button>
        <button
          class="btn btn-sm"
          :class="panel.enabled ? 'btn-ghost' : 'btn-amber'"
          :disabled="saving || loading || !panel.available"
          @click="toggle"
        >
          <Icon name="zap" :size="13" />
          {{ saving ? '保存中' : panel.enabled ? '关闭云端' : '启用云端' }}
        </button>
      </div>
    </div>

    <div class="cp-badges">
      <span v-for="badge in panel.badges" :key="badge" class="cp-badge mono">{{ badge }}</span>
    </div>

    <div class="cp-grid">
      <div class="cp-cell">
        <span class="cp-l">服务</span>
        <span class="cp-v mono">{{ panel.service }}</span>
      </div>
      <div class="cp-cell">
        <span class="cp-l">区域</span>
        <span class="cp-v mono">{{ panel.region }}</span>
      </div>
      <div class="cp-cell">
        <span class="cp-l">触发阈值</span>
        <span class="cp-v mono">{{ panel.threshold }}s</span>
      </div>
      <div class="cp-cell">
        <span class="cp-l">项目</span>
        <span class="cp-v mono">{{ base }}</span>
      </div>
    </div>

    <div class="cp-queue">
      <div class="queue-h">
        <Icon name="activity" :size="13" />
        <span>任务流</span>
      </div>
      <div class="queue-row" :class="{ on: panel.enabled }">
        <span class="dot" :class="panel.available ? 'live' : 'bad'"></span>
        <span>{{ panel.available ? 'Cloud Run 服务可接收求解任务' : 'Cloud Run 当前不可用' }}</span>
      </div>
      <div class="queue-row" :class="{ on: panel.enabled }">
        <span class="dot" :class="panel.enabled ? 'ok' : ''"></span>
        <span>{{ panel.enabled ? '本项目长耗时求解会走云端' : '本项目仍使用本地求解' }}</span>
      </div>
      <div class="queue-row">
        <span class="dot"></span>
        <span>任务历史需后端提供 solver job proxy 后显示</span>
      </div>
    </div>
  </div>
</template>

<script>
import { computed, onMounted, ref } from 'vue'
import Icon from './Icon.vue'
import { Cloud } from '../lib/api.js'
import { buildCloudTaskPanel } from '../lib/workspaceUi.js'
import { useToasts } from '../composables/useToasts.js'

export default {
  name: 'CloudTaskPanel',
  components: { Icon },
  props: {
    base: { type: String, required: true },
    projectConfig: { type: Object, default: null },
  },
  emits: ['changed'],
  setup(props, { emit }) {
    const toasts = useToasts()
    const status = ref(null)
    const loading = ref(false)
    const saving = ref(false)
    const panel = computed(() => buildCloudTaskPanel(status.value || {}, props.projectConfig || {}))

    async function refresh() {
      loading.value = true
      try {
        status.value = await Cloud.status()
      } catch (error) {
        status.value = { available: false, error: error.message }
      } finally {
        loading.value = false
      }
    }

    async function toggle() {
      if (!panel.value.available || saving.value) return
      saving.value = true
      try {
        const response = panel.value.enabled ? await Cloud.disable(props.base) : await Cloud.enable(props.base)
        toasts.success(panel.value.enabled ? '云端加速已关闭' : '云端加速已启用', props.base)
        emit('changed', response?.config || null)
      } catch (error) {
        toasts.error(error.response?.data?.detail || '云端设置失败')
      } finally {
        saving.value = false
      }
    }

    onMounted(refresh)

    return { loading, saving, panel, refresh, toggle }
  },
}
</script>

<style scoped>
.cloud-panel { padding: 16px; }
.cp-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 12px; }
.cp-title, .cp-actions { display: flex; align-items: center; gap: 8px; }
.cp-badges { display: flex; flex-wrap: wrap; gap: 7px; margin-bottom: 14px; }
.cp-badge { padding: 5px 8px; border: 1px solid var(--line); border-radius: var(--r-xs); background: var(--panel-2); color: var(--ink-2); font-size: 10.5px; }
.cp-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 16px; }
.cp-cell { padding: 12px; background: var(--panel-2); border: 1px solid var(--line); border-radius: var(--r); }
.cp-l { display: block; color: var(--ink-3); font-size: 11px; margin-bottom: 4px; }
.cp-v { display: block; color: var(--ink); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cp-queue { border: 1px solid var(--line); border-radius: var(--r); overflow: hidden; }
.queue-h { display: flex; align-items: center; gap: 7px; padding: 10px 12px; background: var(--panel-2); color: var(--ink-2); font-weight: 700; font-size: 12px; }
.queue-row { display: flex; align-items: center; gap: 9px; padding: 10px 12px; color: var(--ink-2); border-top: 1px solid var(--line); font-size: 13px; }
.queue-row.on { color: var(--ink); }
.dot.bad { background: var(--bad); box-shadow: 0 0 10px var(--bad-glow); }
.spin { animation: spin 0.7s linear infinite; }
@media (max-width: 840px) {
  .cp-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 520px) {
  .cp-grid { grid-template-columns: 1fr; }
}
</style>
