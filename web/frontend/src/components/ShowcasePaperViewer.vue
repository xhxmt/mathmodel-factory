<template>
  <div class="viewer" role="dialog" aria-modal="true" :aria-label="paper.title">
    <header class="viewer-head">
      <button class="btn btn-icon btn-ghost" @click="$emit('close')" title="关闭阅读器">
        <Icon name="x" :size="16" />
      </button>
      <div class="viewer-title">
        <span class="label">展示论文 · PDF</span>
        <strong>{{ paper.title }}</strong>
      </div>
      <div class="viewer-actions">
        <button class="btn btn-icon btn-ghost" :disabled="!viewerUrl" @click="openExternal" title="在新窗口打开">
          <Icon name="external-link" :size="15" />
        </button>
        <button class="btn btn-icon btn-amber" :disabled="!viewerUrl" @click="download" title="下载论文">
          <Icon name="download" :size="15" />
        </button>
      </div>
    </header>
    <div class="viewer-body">
      <div v-if="loading" class="viewer-state"><div class="spinner"></div></div>
      <div v-else-if="error" class="viewer-state viewer-error"><Icon name="alert-triangle" :size="18" /> {{ error }}</div>
      <iframe v-else :src="viewerFrameUrl" :title="paper.title"></iframe>
    </div>
  </div>
</template>

<script>
import Icon from './Icon.vue'
import { fetchBlobUrl } from '../lib/api.js'

export default {
  name: 'ShowcasePaperViewer',
  components: { Icon },
  props: { paper: { type: Object, required: true } },
  emits: ['close'],
  data() {
    return { viewerUrl: '', loading: true, error: '', previousOverflow: '' }
  },
  computed: {
    viewerFrameUrl() { return `${this.viewerUrl}#toolbar=1&navpanes=0&view=FitH` },
  },
  async mounted() {
    this.previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    window.addEventListener('keydown', this.onKey)
    try {
      this.viewerUrl = await fetchBlobUrl(this.paper.pdf_url)
    } catch (error) {
      this.error = error.response?.data?.detail || '论文暂时无法读取'
    } finally {
      this.loading = false
    }
  },
  beforeUnmount() {
    document.body.style.overflow = this.previousOverflow
    window.removeEventListener('keydown', this.onKey)
    if (this.viewerUrl) URL.revokeObjectURL(this.viewerUrl)
  },
  methods: {
    onKey(event) {
      if (event.key === 'Escape') this.$emit('close')
    },
    openExternal() {
      if (this.viewerUrl) window.open(this.viewerUrl, '_blank', 'noopener')
    },
    download() {
      if (!this.viewerUrl) return
      const anchor = document.createElement('a')
      anchor.href = this.viewerUrl
      anchor.download = `${this.paper.base_name}_paper.pdf`
      anchor.click()
    },
  },
}
</script>

<style scoped>
.viewer { position: fixed; inset: 0; z-index: 260; display: flex; flex-direction: column; background: var(--bg); }
.viewer-head {
  min-height: 64px; display: flex; align-items: center; gap: 13px; padding: 10px 16px;
  border-bottom: 1px solid var(--line); background: var(--panel);
}
.viewer-title { min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.viewer-title strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; letter-spacing: 0; }
.viewer-actions { display: flex; gap: 7px; margin-left: auto; }
.viewer-body { flex: 1; min-height: 0; padding: 10px; background: var(--bg-2); }
.viewer-body iframe { display: block; width: 100%; height: 100%; border: 1px solid var(--line); border-radius: var(--r); background: #fff; }
.viewer-state { height: 100%; display: flex; align-items: center; justify-content: center; gap: 9px; color: var(--ink-3); }
.viewer-error { color: var(--bad); }

@media (max-width: 600px) {
  .viewer-head { min-height: 58px; padding: 8px; gap: 8px; }
  .viewer-title .label { display: none; }
  .viewer-title strong { font-size: 12px; }
  .viewer-body { padding: 0; }
  .viewer-body iframe { border: 0; border-radius: 0; }
}
</style>
