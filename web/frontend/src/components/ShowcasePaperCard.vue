<template>
  <article
    class="paper-card panel"
    tabindex="0"
    @click="$emit('open', paper)"
    @keydown.enter="$emit('open', paper)"
  >
    <div class="paper-top">
      <div class="paper-mark"><Icon name="file-text" :size="20" /></div>
      <div class="paper-title-wrap">
        <span class="paper-collection label">{{ paper.collection }}</span>
        <h2 class="paper-title">{{ paper.title }}</h2>
      </div>
      <span class="paper-tag mono">PDF</span>
    </div>

    <div class="paper-code mono">{{ paper.base_name }}</div>

    <div class="paper-foot">
      <div class="paper-meta mono">
        <span><Icon name="clock" :size="11" /> {{ rel(paper.updated_at) }}</span>
        <span><Icon name="package" :size="11" /> {{ bytes(paper.size_bytes) }}</span>
      </div>
      <div class="paper-actions" @click.stop>
        <button
          class="btn btn-icon btn-sm btn-ghost"
          :disabled="downloading"
          @click="downloadPaper"
          title="下载论文"
        ><Icon name="download" :size="13" /></button>
        <button class="btn btn-sm btn-ghost open-paper" @click="$emit('open', paper)">
          阅读 <Icon name="book-open" :size="13" />
        </button>
      </div>
    </div>
  </article>
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
.paper-card {
  min-height: 208px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 18px;
  cursor: pointer;
  border-top: 2px solid var(--live);
  transition: transform 0.2s var(--ease), box-shadow 0.2s var(--ease), background 0.2s var(--ease);
}
.paper-card:hover { transform: translateY(-2px); box-shadow: var(--shadow); background: var(--panel-2); }
.paper-card:focus-visible { outline: 2px solid var(--amber); outline-offset: 2px; }
.paper-top { display: flex; align-items: flex-start; gap: 12px; min-width: 0; }
.paper-mark {
  width: 42px; height: 42px; flex: 0 0 42px;
  display: flex; align-items: center; justify-content: center;
  color: var(--live); background: var(--live-dim);
  border: 1px solid color-mix(in srgb, var(--live) 28%, transparent);
  border-radius: var(--r);
}
.paper-title-wrap { flex: 1; min-width: 0; }
.paper-collection { display: block; margin: 2px 0 7px; letter-spacing: 0.08em; }
.paper-title {
  display: -webkit-box; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 2;
  font-size: 16px; line-height: 1.35; letter-spacing: 0; color: var(--ink);
}
.paper-tag {
  flex: 0 0 auto; padding: 4px 7px; border: 1px solid var(--line-2);
  border-radius: var(--r-xs); color: var(--ink-2); font-size: 9px; font-weight: 700;
}
.paper-code {
  min-height: 35px; padding: 9px 11px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  background: var(--bg-2); border: 1px solid var(--line); border-radius: var(--r-sm);
  color: var(--ink-3); font-size: 11px;
}
.paper-foot {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  margin-top: auto; padding-top: 13px; border-top: 1px solid var(--line);
}
.paper-meta { display: flex; flex-wrap: wrap; gap: 10px; color: var(--ink-3); font-size: 10.5px; }
.paper-meta span { display: inline-flex; align-items: center; gap: 5px; }
.paper-actions { display: flex; align-items: center; gap: 6px; }
.open-paper { color: var(--ink); }
.open-paper:hover { border-color: var(--live); color: var(--live); }

@media (max-width: 520px) {
  .paper-card { min-height: 196px; padding: 15px; }
  .paper-foot { align-items: flex-end; }
  .paper-meta { flex-direction: column; gap: 4px; }
}
</style>
