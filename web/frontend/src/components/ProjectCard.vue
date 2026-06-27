<template>
  <article class="card panel" :class="['ac-' + project.status, { pending: project.consultation_pending }]" @click="$emit('open', project)" tabindex="0" @keydown.enter="$emit('open', project)">
    <div class="c-top">
      <span class="dot" :class="dotClass"></span>
      <span class="c-name mono">{{ project.base_name }}</span>
      <span class="spacer"></span>
      <span class="tag" :class="'st-' + project.status">{{ statusLabel }}</span>
    </div>

    <div v-if="project.consultation_pending" class="c-consult">
      <Icon name="alert-triangle" :size="13" />
      <span>等待你处理 · {{ project.consultation_gate || 'gate' }}</span>
    </div>

    <div class="c-rail">
      <StepRail :current-step="project.current_step" :awaiting="project.consultation_pending" compact />
      <div class="c-railmeta mono">
        <span class="c-step">{{ stepText }}</span>
        <span class="c-pct" :class="{ done: project.progress_percent >= 100 }">{{ Math.round(project.progress_percent) }}%</span>
      </div>
    </div>

    <div class="c-foot">
      <div class="c-meta mono">
        <span class="m-i"><Icon name="clock" :size="11" /> {{ rel(project.last_updated) }}</span>
        <span v-if="project.pid" class="m-i">PID {{ project.pid }}</span>
      </div>
      <div class="c-actions" @click.stop>
        <button v-if="project.is_running" class="btn btn-icon btn-sm btn-ghost" @click="$emit('action', project, 'pause')" title="暂停"><Icon name="pause" :size="13" /></button>
        <button v-else-if="canResume" class="btn btn-icon btn-sm btn-ghost" @click="$emit('action', project, 'resume')" title="恢复"><Icon name="play" :size="13" /></button>
        <button class="btn btn-sm btn-ghost enter" @click.stop="$emit('open', project)">进入 <Icon name="chevron-right" :size="13" /></button>
      </div>
    </div>
  </article>
</template>

<script>
import Icon from './Icon.vue'
import StepRail from './StepRail.vue'
import { relativeTime } from '../lib/api.js'
import { stepByIndex } from '../lib/steps.js'
import { statusLabel as mapStatusLabel } from '../lib/status.js'

export default {
  name: 'ProjectCard',
  components: { Icon, StepRail },
  props: { project: { type: Object, required: true } },
  emits: ['open', 'action'],
  computed: {
    statusLabel() { return mapStatusLabel(this.project.status) },
    dotClass() {
      return { running: 'live', awaiting_consultation: 'amber', completed: 'ok', paused: 'paused', failed: 'bad', killed: 'bad' }[this.project.status] || ''
    },
    canResume() { return ['paused', 'ready', 'awaiting_consultation'].includes(this.project.status) },
    stepText() {
      const c = this.project.current_step
      if (c >= 16) return '16 · 已完成'
      const s = stepByIndex(Math.min(16, Math.max(0, c + 1)))
      return s ? `${Math.max(0, c + 1)} · ${s.name}` : `Step ${c + 1}`
    },
  },
  methods: { rel: relativeTime },
}
</script>

<style scoped>
.card {
  position: relative; display: flex; flex-direction: column; gap: 14px;
  padding: 16px 16px 14px; cursor: pointer;
  border-left: 2px solid var(--ink-3);
  transition: border-color 0.2s var(--ease), transform 0.2s var(--ease), box-shadow 0.2s var(--ease), background 0.2s var(--ease);
}
.card:hover { transform: translateY(-2px); box-shadow: var(--shadow); background: var(--panel-2); }
.card:focus-visible { outline: 2px solid var(--amber); outline-offset: 2px; }
.ac-running { border-left-color: var(--live); }
.ac-completed { border-left-color: var(--ok); }
.ac-paused { border-left-color: var(--paused); }
.ac-failed, .ac-killed { border-left-color: var(--bad); }
.card.pending { border-left-color: var(--amber); box-shadow: inset 2px 0 0 var(--amber), 0 0 0 1px var(--amber-line); }

.c-top { display: flex; align-items: center; gap: 9px; }
.c-name { font-size: 13.5px; font-weight: 600; color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.spacer { flex: 1; }

.c-consult {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 11px; margin: -2px 0;
  background: var(--amber-dim); border: 1px solid var(--amber-line); border-radius: var(--r-sm);
  color: var(--amber); font-size: 12px; font-weight: 600;
}

.c-rail { display: flex; flex-direction: column; gap: 9px; padding: 2px 2px 0; }
.c-railmeta { display: flex; align-items: center; justify-content: space-between; font-size: 11px; }
.c-step { color: var(--ink-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.c-pct { color: var(--ink-3); font-weight: 700; }
.c-pct.done { color: var(--ok); }

.c-foot { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding-top: 12px; border-top: 1px solid var(--line); }
.c-meta { display: flex; gap: 12px; font-size: 11px; color: var(--ink-3); }
.m-i { display: inline-flex; align-items: center; gap: 5px; }
.c-actions { display: flex; align-items: center; gap: 6px; }
.enter { color: var(--ink-2); }
.enter:hover { color: var(--ink); border-color: var(--live); }

.tag { font: 600 10px/1 var(--mono); letter-spacing: 0.06em; text-transform: uppercase; padding: 4px 8px; border-radius: var(--r-xs); border: 1px solid var(--line); background: var(--panel-2); color: var(--ink-2); white-space: nowrap; }
.st-running { color: var(--live); border-color: var(--live-dim); background: var(--live-dim); }
.st-awaiting_consultation { color: var(--amber); border-color: var(--amber-line); background: var(--amber-dim); }
.st-completed { color: var(--ok); border-color: var(--ok-dim); background: var(--ok-dim); }
.st-paused { color: var(--paused); }
.st-failed, .st-killed { color: var(--bad); border-color: var(--bad-dim); background: var(--bad-dim); }
</style>
