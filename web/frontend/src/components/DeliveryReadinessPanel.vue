<template>
  <section class="delivery panel">
    <header class="delivery-head">
      <div>
        <div class="eyebrow">DELIVERY READINESS</div>
        <h2>交付就绪中心</h2>
        <p>只认 Final Audit 与原子 current release，不把普通文件存在当成交付成功。</p>
      </div>
      <div class="readiness" :class="`st-${delivery.status || 'pending'}`">
        <Icon :name="delivery.ready ? 'check-circle' : 'alert-triangle'" :size="20" />
        <strong>{{ delivery.ready ? '可以提交' : delivery.status === 'blocked' ? '存在阻塞' : '尚未就绪' }}</strong>
        <span>{{ delivery.blocking_count || 0 }} 失败 · {{ delivery.pending_count || 0 }} 待完成</span>
      </div>
    </header>

    <div class="check-list">
      <button
        v-for="check in delivery.checks || []"
        :key="check.id"
        class="check"
        :class="`check-${check.status}`"
        :disabled="!check.path"
        @click="open(check)"
      >
        <Icon :name="check.status === 'pass' ? 'check-circle' : check.status === 'fail' ? 'alert-triangle' : 'clock'" :size="16" />
        <span><strong>{{ check.label }}</strong><small>{{ check.detail }}</small></span>
        <span class="status mono">{{ statusLabel(check.status) }}</span>
      </button>
    </div>

    <footer class="release-box">
      <div>
        <span>当前原子发布</span>
        <strong class="mono">{{ release.available ? release.release_id?.slice(0, 16) : '尚未生成' }}</strong>
      </div>
      <div class="download-actions">
        <button class="btn btn-sm btn-ghost" :disabled="!release.available || downloading" @click="downloadPaper">
          <Icon name="download" :size="13" /> PDF
        </button>
        <button class="btn btn-sm btn-amber" :disabled="!release.submission_available || downloading" @click="downloadSubmission">
          <Icon name="package" :size="13" /> 提交包 ZIP
        </button>
      </div>
    </footer>
  </section>
</template>

<script>
import { computed, ref } from 'vue'
import Icon from './Icon.vue'
import { Projects, downloadBlob } from '../lib/api.js'
import { useToasts } from '../composables/useToasts.js'

export default {
  name: 'DeliveryReadinessPanel',
  components: { Icon },
  props: {
    base: { type: String, required: true },
    delivery: { type: Object, default: () => ({}) },
  },
  emits: ['open-file'],
  setup(props, { emit }) {
    const toasts = useToasts()
    const downloading = ref(false)
    const release = computed(() => props.delivery?.release || {})
    const statusLabel = (value) => ({ pass: '通过', fail: '失败', pending: '待完成' }[value] || value)
    function open(check) {
      if (!check?.path) return
      emit('open-file', { path: check.path, name: check.path.split('/').pop(), type: check.path.endsWith('.json') ? 'json' : 'markdown' })
    }
    async function download(url, filename) {
      downloading.value = true
      try { await downloadBlob(url, filename) }
      catch (error) { toasts.error(error.response?.data?.detail || '下载失败') }
      finally { downloading.value = false }
    }
    const downloadPaper = () => download(Projects.paperUrl(props.base, true), `${props.base}_paper.pdf`)
    const downloadSubmission = () => download(Projects.submissionUrl(props.base), `${props.base}_submission.zip`)
    return { release, downloading, statusLabel, open, downloadPaper, downloadSubmission }
  },
}
</script>

<style scoped>
.delivery { padding: 18px; }
.delivery-head { display: flex; justify-content: space-between; gap: 20px; align-items: flex-start; }
.eyebrow { color: var(--amber); font: 800 9px var(--mono); letter-spacing: .16em; }
h2 { margin: 4px 0 6px; font-size: 20px; }
p { margin: 0; max-width: 650px; color: var(--ink-2); font-size: 12px; }
.readiness { min-width: 155px; display: grid; grid-template-columns: auto 1fr; gap: 2px 8px; padding: 10px 12px; border: 1px solid var(--line); border-radius: var(--r); }
.readiness svg { grid-row: 1 / 3; }
.readiness strong { font-size: 13px; }
.readiness span { color: var(--ink-3); font-size: 10px; }
.st-ready { color: var(--ok); background: var(--ok-dim); border-color: var(--ok) !important; }
.st-blocked { color: var(--bad); background: var(--bad-dim); border-color: var(--bad) !important; }
.check-list { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 18px; }
.check { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 10px; align-items: center; padding: 11px; border: 1px solid var(--line); border-radius: var(--r-sm); background: var(--panel-2); color: var(--ink); text-align: left; }
.check:not(:disabled) { cursor: pointer; }
.check:not(:disabled):hover { border-color: var(--live-line); }
.check > span { min-width: 0; }
.check strong, .check small { display: block; }
.check strong { font-size: 12px; }
.check small { margin-top: 3px; color: var(--ink-3); font-size: 10px; overflow-wrap: anywhere; }
.check-pass > svg, .check-pass .status { color: var(--ok); }
.check-fail > svg, .check-fail .status { color: var(--bad); }
.check-pending > svg, .check-pending .status { color: var(--amber); }
.status { font-size: 9.5px; }
.release-box { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 16px; padding: 12px; border-top: 1px solid var(--line); background: var(--panel-2); }
.release-box > div:first-child { display: flex; flex-direction: column; gap: 4px; }
.release-box span { color: var(--ink-3); font-size: 10px; }
.release-box strong { font-size: 11px; }
.download-actions { display: flex; gap: 8px; }
@media (max-width: 720px) {
  .delivery-head, .release-box { flex-direction: column; }
  .readiness { width: 100%; }
  .check-list { grid-template-columns: 1fr; }
  .release-box { align-items: stretch; }
  .download-actions .btn { flex: 1; }
}
</style>
