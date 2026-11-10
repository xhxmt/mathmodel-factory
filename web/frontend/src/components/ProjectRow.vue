<template>
  <div
    class="row"
    :class="['tone-' + (tone || 'none'), { nested, attention: awaiting }]"
    role="button"
    tabindex="0"
    @click="$emit('open', project)"
    @keydown.enter.self="$emit('open', project)"
  >
    <span class="dot" :class="tone"></span>

    <div class="main">
      <div class="title">
        <span class="name">{{ nested ? project.base_name : (project.problem_title || project.base_name) }}</span>
        <span v-if="!nested && project.problem_title" class="run mono">{{ project.base_name }}</span>
      </div>
      <div class="sub" :class="{ warn: awaiting, bad: failed }">{{ hint }}</div>
    </div>

    <div class="progress" :title="`进度 ${progress}%`">
      <div class="bar"><span :style="{ width: progress + '%' }" :class="tone"></span></div>
      <span class="pct tnum">{{ progress }}%</span>
    </div>

    <span class="pill" :class="tone">{{ label }}</span>
    <span class="time tnum">{{ rel(project.last_updated) }}</span>

    <div class="act" @click.stop>
      <button v-if="control" class="btn btn-sm" :class="awaiting ? 'btn-primary' : ''" @click="perform">
        <Icon :name="control.icon" :size="14" />{{ control.label }}
      </button>
      <Icon v-else name="chevron-right" :size="16" class="chev" />
    </div>
  </div>
</template>

<script>
import Icon from './Icon.vue'
import { relativeTime } from '../lib/api.js'
import {
  executionHint,
  executionLabel,
  executionStatus,
  needsHuman,
  primaryControl,
  projectProgress,
  statusTone,
  stepText,
} from '../lib/projectState.js'

export default {
  name: 'ProjectRow',
  components: { Icon },
  props: {
    project: { type: Object, required: true },
    nested: { type: Boolean, default: false },
  },
  emits: ['open', 'action'],
  computed: {
    tone() { return statusTone(this.project) },
    label() { return executionLabel(this.project) },
    awaiting() { return needsHuman(this.project) },
    failed() { return ['failed', 'interrupted', 'killed'].includes(executionStatus(this.project)) },
    hint() { return executionStatus(this.project) === 'completed' ? stepText(this.project) : executionHint(this.project) },
    progress() { return projectProgress(this.project) },
    control() { return primaryControl(this.project) },
  },
  methods: {
    rel: relativeTime,
    perform() {
      if (this.control?.kind === 'command') this.$emit('action', this.project, this.control.action)
      else this.$emit('open', this.project, this.control?.tab)
    },
  },
}
</script>

<style scoped>
.row {
  display: grid;
  justify-items: stretch;
  grid-template-columns: 14px minmax(0, 1fr) 150px 84px 84px 128px;
  align-items: center;
  gap: 14px;
  padding: 12px 16px;
  border-top: 1px solid var(--line);
  cursor: pointer;
  transition: background 0.12s var(--ease);
}
.row:first-child { border-top: 0; }
.row:hover { background: var(--panel-2); }
.row.nested { padding-left: 30px; background: var(--bg); }
.row.nested:hover { background: var(--panel-2); }

.main { min-width: 0; }
.title { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.name { font-weight: 500; color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.nested .name { font-family: var(--mono); font-size: 13px; font-weight: 400; }
.run { color: var(--ink-3); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex-shrink: 1; }
.sub { font-size: 12.5px; color: var(--ink-2); margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sub.warn { color: var(--amber); }
.sub.bad { color: var(--bad); }

.progress { display: flex; align-items: center; gap: 8px; }
.bar { flex: 1; height: 4px; border-radius: 4px; background: var(--panel-3); overflow: hidden; }
.bar span { display: block; height: 100%; background: var(--ink-3); border-radius: 4px; }
.bar span.live { background: var(--accent); }
.bar span.ok { background: var(--ok); }
.bar span.amber { background: var(--amber); }
.bar span.bad { background: var(--bad); }
.pct { font-size: 12px; color: var(--ink-3); width: 34px; text-align: right; }

.dot { justify-self: center; }
.pill { justify-self: start; }
.time { font-size: 12px; color: var(--ink-3); text-align: right; white-space: nowrap; }
.act { display: flex; justify-content: flex-end; min-width: 24px; }
.chev { color: var(--ink-3); }

@media (max-width: 860px) {
  .row { grid-template-columns: 14px minmax(0, 1fr) auto; row-gap: 6px; }
  .progress, .time, .run { display: none; }
  .pill { grid-column: 3; grid-row: 1; justify-self: end; }
  .act { grid-column: 2 / 4; grid-row: 2; justify-content: flex-start; }
  .act .chev { display: none; }
  .row.nested { padding-left: 24px; }
}
</style>
