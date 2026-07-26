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
        <a class="btn btn-icon btn-ghost" :href="paper.pdf_url" target="_blank" rel="noopener" title="在新窗口打开">
          <Icon name="external-link" :size="15" />
        </a>
        <a class="btn btn-icon btn-amber" :href="downloadUrl" download title="下载论文">
          <Icon name="download" :size="15" />
        </a>
      </div>
    </header>
    <div class="viewer-body">
      <iframe :src="viewerUrl" :title="paper.title"></iframe>
    </div>
  </div>
</template>

<script>
import { onBeforeUnmount, onMounted } from 'vue'
import Icon from './Icon.vue'
import { Showcase } from '../lib/api.js'

export default {
  name: 'ShowcasePaperViewer',
  components: { Icon },
  props: { paper: { type: Object, required: true } },
  emits: ['close'],
  computed: {
    viewerUrl() { return `${this.paper.pdf_url}#toolbar=1&navpanes=0&view=FitH` },
    downloadUrl() { return Showcase.downloadUrl(this.paper) },
  },
  setup(_, { emit }) {
    const previousOverflow = document.body.style.overflow
    function onKey(event) {
      if (event.key === 'Escape') emit('close')
    }
    onMounted(() => {
      document.body.style.overflow = 'hidden'
      window.addEventListener('keydown', onKey)
    })
    onBeforeUnmount(() => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', onKey)
    })
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
.viewer-actions a { text-decoration: none; }
.viewer-body { flex: 1; min-height: 0; padding: 10px; background: var(--bg-2); }
.viewer-body iframe { display: block; width: 100%; height: 100%; border: 1px solid var(--line); border-radius: var(--r); background: #fff; }

@media (max-width: 600px) {
  .viewer-head { min-height: 58px; padding: 8px; gap: 8px; }
  .viewer-title .label { display: none; }
  .viewer-title strong { font-size: 12px; }
  .viewer-body { padding: 0; }
  .viewer-body iframe { border: 0; border-radius: 0; }
}
</style>
