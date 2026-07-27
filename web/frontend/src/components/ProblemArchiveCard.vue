<template>
  <article class="archive-card panel" :class="'ac-' + latest.status">
    <div class="archive-head">
      <div class="archive-mark"><Icon name="package" :size="16" /></div>
      <div class="archive-title-wrap">
        <div class="archive-title">{{ archive.title }}</div>
        <div class="archive-meta mono">
          <span>{{ archive.run_count }} 次运行</span>
          <span v-if="archive.completed_count">· {{ archive.completed_count }} 次完成</span>
          <span v-if="archive.running_count" class="live">· {{ archive.running_count }} 次运行中</span>
        </div>
      </div>
      <span class="archive-badge mono">题目归档</span>
      <button
        class="btn btn-icon btn-sm btn-ghost"
        :title="expanded ? '收起历史运行' : '展开历史运行'"
        :aria-expanded="expanded ? 'true' : 'false'"
        @click="expanded = !expanded"
      >
        <Icon :name="expanded ? 'chevron-up' : 'chevron-down'" :size="14" />
      </button>
    </div>

    <div class="latest-label label">最近一次运行</div>
    <div class="run-row latest-run" role="button" tabindex="0" @click="$emit('open', latest)" @keydown.enter="$emit('open', latest)">
      <span class="dot" :class="dotClass(latest)"></span>
      <div class="run-main">
        <div class="run-name mono">{{ latest.base_name }}</div>
        <div class="run-meta mono">
          <span>{{ stepText(latest) }}</span>
          <span v-if="latest.last_updated">· {{ rel(latest.last_updated) }}</span>
          <span v-if="latest.archived" class="archived">· 已归档</span>
        </div>
      </div>
      <span class="tag" :class="'st-' + latest.status">{{ status(latest) }}</span>
      <div class="run-actions" @click.stop>
        <button v-if="latest.is_running" class="btn btn-icon btn-sm btn-ghost" title="暂停" @click="$emit('action', latest, 'pause')"><Icon name="pause" :size="13" /></button>
        <button v-else-if="canResume(latest)" class="btn btn-icon btn-sm btn-ghost" title="恢复" @click="$emit('action', latest, 'resume')"><Icon name="play" :size="13" /></button>
        <button class="btn btn-sm btn-ghost" @click="$emit('open', latest)">进入 <Icon name="chevron-right" :size="12" /></button>
      </div>
    </div>

    <div v-if="expanded" class="history">
      <div class="history-head label">历史运行 · HISTORY</div>
      <div
        v-for="(run, index) in history"
        :key="run.run_id || run.base_name"
        class="run-row history-run"
        role="button"
        tabindex="0"
        @click="$emit('open', run)"
        @keydown.enter="$emit('open', run)"
      >
        <span class="run-index mono">#{{ archive.run_count - index - 1 }}</span>
        <span class="dot" :class="dotClass(run)"></span>
        <div class="run-main">
          <div class="run-name mono">{{ run.base_name }}</div>
          <div class="run-meta mono">
            <span>{{ stepText(run) }}</span>
            <span v-if="run.last_updated">· {{ rel(run.last_updated) }}</span>
            <span v-if="run.archived" class="archived">· 已归档</span>
          </div>
        </div>
        <span class="tag" :class="'st-' + run.status">{{ status(run) }}</span>
        <div class="run-actions" @click.stop>
          <button v-if="run.is_running" class="btn btn-icon btn-sm btn-ghost" title="暂停" @click="$emit('action', run, 'pause')"><Icon name="pause" :size="13" /></button>
          <button v-else-if="canResume(run)" class="btn btn-icon btn-sm btn-ghost" title="恢复" @click="$emit('action', run, 'resume')"><Icon name="play" :size="13" /></button>
          <button class="btn btn-icon btn-sm btn-ghost" title="进入该次运行" @click="$emit('open', run)"><Icon name="chevron-right" :size="13" /></button>
        </div>
      </div>
    </div>
  </article>
</template>

<script>
import Icon from './Icon.vue'
import { relativeTime } from '../lib/api.js'
import { statusLabel } from '../lib/status.js'
import { stepByIndex } from '../lib/steps.js'

export default {
  name: 'ProblemArchiveCard',
  components: { Icon },
  props: {
    archive: { type: Object, required: true },
  },
  emits: ['open', 'action'],
  data() {
    return { expanded: false }
  },
  computed: {
    latest() { return this.archive.latest || this.archive.runs?.[0] || {} },
    history() { return (this.archive.runs || []).slice(1) },
  },
  methods: {
    rel: relativeTime,
    status(run) { return statusLabel(run.status) },
    canResume(run) { return !run.archived && ['paused', 'ready', 'awaiting_consultation', 'awaiting_selection'].includes(run.status) },
    dotClass(run) {
      return {
        running: 'live',
        awaiting_consultation: 'amber',
        awaiting_selection: 'amber',
        completed: 'ok',
        paused: 'paused',
        failed: 'bad',
        killed: 'bad',
      }[run.status] || ''
    },
    stepText(run) {
      const current = Number(run.current_step ?? -1)
      if (current >= 16) return 'Step 16 · 已完成'
      const next = Math.min(16, Math.max(0, current + 1))
      const step = stepByIndex(next)
      return step ? `Step ${next} · ${step.name}` : `Step ${next}`
    },
  },
}
</script>

<style scoped>
.archive-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 15px;
  border-left: 2px solid var(--ink-3);
}
.archive-card.ac-running { border-left-color: var(--live); }
.archive-card.ac-completed { border-left-color: var(--ok); }
.archive-card.ac-paused { border-left-color: var(--paused); }
.archive-card.ac-failed, .archive-card.ac-killed { border-left-color: var(--bad); }

.archive-head { display: flex; align-items: center; gap: 10px; min-width: 0; }
.archive-mark {
  width: 32px; height: 32px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  border-radius: var(--r-sm); background: var(--amber-dim); color: var(--amber);
  border: 1px solid var(--amber-line);
}
.archive-title-wrap { flex: 1; min-width: 0; }
.archive-title { font-size: 13.5px; font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.archive-meta { margin-top: 4px; color: var(--ink-3); font-size: 10.5px; }
.archive-meta .live { color: var(--live); }
.archive-badge {
  flex-shrink: 0; padding: 4px 7px; border-radius: var(--r-xs);
  color: var(--amber); background: var(--amber-dim); border: 1px solid var(--amber-line);
  font-size: 9px; font-weight: 700; letter-spacing: 0.08em;
}
.latest-label, .history-head { color: var(--ink-3); font-size: 9px; letter-spacing: 0.12em; }

.run-row {
  display: flex; align-items: center; gap: 9px; min-width: 0;
  padding: 10px 11px; border: 1px solid var(--line); border-radius: var(--r);
  background: var(--panel-2); cursor: pointer;
  transition: border-color 0.15s var(--ease), background 0.15s var(--ease), transform 0.15s var(--ease);
}
.run-row:hover { border-color: var(--line-2); background: var(--panel-3); transform: translateY(-1px); }
.run-row:focus-visible { outline: 2px solid var(--amber); outline-offset: 2px; }
.latest-run { border-color: var(--amber-line); background: color-mix(in srgb, var(--panel-2) 84%, var(--amber-dim)); }
.run-index { width: 26px; flex-shrink: 0; color: var(--ink-3); font-size: 10px; }
.run-main { flex: 1; min-width: 0; }
.run-name { color: var(--ink); font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.run-meta { margin-top: 4px; color: var(--ink-3); font-size: 10.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.run-meta .archived { color: var(--ok); }
.run-actions { display: flex; align-items: center; gap: 4px; flex-shrink: 0; }
.history { display: flex; flex-direction: column; gap: 7px; padding-top: 11px; border-top: 1px solid var(--line); }

.tag { font: 600 9.5px/1 var(--mono); letter-spacing: 0.05em; text-transform: uppercase; padding: 4px 7px; border-radius: var(--r-xs); border: 1px solid var(--line); background: var(--panel); color: var(--ink-2); white-space: nowrap; }
.st-running { color: var(--live); border-color: var(--live-dim); background: var(--live-dim); }
.st-awaiting_consultation, .st-awaiting_selection { color: var(--amber); border-color: var(--amber-line); background: var(--amber-dim); }
.st-completed { color: var(--ok); border-color: var(--ok-dim); background: var(--ok-dim); }
.st-paused { color: var(--paused); }
.st-failed, .st-killed { color: var(--bad); border-color: var(--bad-dim); background: var(--bad-dim); }

@media (max-width: 640px) {
  .archive-badge { display: none; }
  .run-row { align-items: flex-start; flex-wrap: wrap; }
  .run-main { min-width: calc(100% - 48px); }
  .run-actions { width: 100%; justify-content: flex-end; }
}
</style>
