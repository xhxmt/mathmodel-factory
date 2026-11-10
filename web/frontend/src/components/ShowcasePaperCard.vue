<template>
  <div
    class="paper-row"
    role="button"
    tabindex="0"
    @click="$emit('open', paper)"
    @keydown.enter.self="$emit('open', paper)"
  >
    <div class="mark"><Icon name="file-text" :size="16" /></div>
    <div class="main">
      <div class="title">{{ paper.title }}</div>
      <div class="sub">{{ paper.collection }}<span class="sep">·</span><span class="mono">{{ paper.base_name }}</span></div>
    </div>
    <span class="meta tnum">{{ rel(paper.updated_at) }}</span>
    <span class="meta tnum">{{ bytes(paper.size_bytes) }}</span>
    <div class="actions" @click.stop>
      <button class="btn btn-icon btn-sm btn-ghost" :disabled="downloading" @click="downloadPaper" title="下载 PDF">
        <Icon name="download" :size="14" />
      </button>
      <button class="btn btn-sm" @click="$emit('open', paper)">阅读</button>
    </div>
  </div>
</template>

<script>
import Icon from './Icon.vue'
import { downloadBlob, formatBytes, relativeTime } from '../lib/api.js'

export default {
  name: 'ShowcasePaperCard',
  components: { Icon },
  props: { paper: { type: Object, required: true } },
  emits: ['open'],
  data() {
    return { downloading: false }
  },
  methods: {
    bytes: formatBytes,
    rel: relativeTime,
    async downloadPaper() {
      this.downloading = true
      try {
        await downloadBlob(this.paper.pdf_url, `${this.paper.base_name}_paper.pdf`)
      } finally {
        this.downloading = false
      }
    },
  },
}
</script>

<style scoped>
.paper-row {
  display: grid;
  grid-template-columns: 32px minmax(0, 1fr) 80px 70px auto;
  align-items: center;
  gap: 14px;
  padding: 12px 16px;
  border-top: 1px solid var(--line);
  cursor: pointer;
  transition: background 0.12s var(--ease);
}
.paper-row:first-child { border-top: 0; }
.paper-row:hover { background: var(--panel-2); }
.mark {
  width: 32px; height: 32px; border-radius: var(--r);
  display: flex; align-items: center; justify-content: center;
  background: var(--accent-dim); color: var(--accent);
}
.main { min-width: 0; }
.title { font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sub { font-size: 12.5px; color: var(--ink-3); margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sep { margin: 0 6px; }
.meta { font-size: 12px; color: var(--ink-3); text-align: right; white-space: nowrap; }
.actions { display: flex; gap: 4px; justify-content: flex-end; }

@media (max-width: 720px) {
  .paper-row { grid-template-columns: 32px minmax(0, 1fr) auto; }
  .meta { display: none; }
}
</style>
