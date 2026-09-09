<template>
  <article class="card panel" :class="['ac-' + state, { pending: awaiting }]" @click="$emit('open', project)" tabindex="0" @keydown.enter.self="$emit('open', project)">
    <div class="c-top">
      <span class="dot" :class="tone"></span>
      <span class="c-run mono">{{ project.base_name }}</span>
      <span class="tag" :class="'st-' + state">{{ label }}</span>
    </div>
    <h3>{{ project.problem_title || project.base_name }}</h3>
    <div class="c-stage"><span class="mono">{{ stage?.id ? String(stage.id).padStart(2, '0') + ' / 10' : '—' }}</span><span>{{ stage?.name || '等待调度' }}</span></div>
    <div v-if="awaiting || state === 'interrupted' || state === 'failed'" class="c-notice" :class="{ danger: state === 'interrupted' || state === 'failed' }">
      <Icon :name="awaiting ? 'user' : 'alert-triangle'" :size="14" /><span>{{ hint }}</span>
    </div>
    <div class="c-rail">
      <StepRail :project="project" :current-step="project.current_step" :awaiting="awaiting" compact />
      <div class="c-railmeta"><span>{{ position }}</span><span class="mono" :class="{ done: state === 'completed' }">{{ progress }}%</span></div>
    </div>
    <div class="c-evidence">
      <span :class="{ valid: project.evidence_validity === 'VALID', invalid: project.evidence_validity === 'INVALID' }"><Icon name="shield" :size="12" />{{ evidence }}</span>
      <span :class="{ valid: project.delivery_allowed }"><Icon :name="project.delivery_allowed ? 'check-circle' : 'lock'" :size="12" />{{ project.delivery_allowed ? '允许交付' : '未获交付许可' }}</span>
    </div>
    <div class="c-foot">
      <span class="c-time mono"><Icon name="clock" :size="12" />{{ rel(project.last_updated) }}</span>
      <div class="c-actions" @click.stop>
        <button v-if="control" class="btn btn-sm" :class="awaiting ? 'btn-amber' : 'btn-ghost'" @click="perform"><Icon :name="control.icon" :size="13" />{{ control.label }}</button>
        <button v-else class="btn btn-sm btn-ghost" @click="$emit('open', project)">查看项目<Icon name="chevron-right" :size="13" /></button>
      </div>
    </div>
  </article>
</template>
<script>
import Icon from './Icon.vue'
import StepRail from './StepRail.vue'
import { relativeTime } from '../lib/api.js'
import { executionStatus, executionLabel, statusTone, needsHuman, currentStage, stepText, projectProgress, primaryControl, executionHint, evidenceLabel } from '../lib/projectState.js'
export default {
  name: 'ProjectCard', components: { Icon, StepRail }, props: { project: { type: Object, required: true } }, emits: ['open', 'action'],
  computed: {
    state() { return executionStatus(this.project) }, label() { return executionLabel(this.project) }, tone() { return statusTone(this.project) },
    awaiting() { return needsHuman(this.project) }, stage() { return currentStage(this.project) }, position() { return stepText(this.project) },
    progress() { return projectProgress(this.project) }, control() { return primaryControl(this.project) }, hint() { return executionHint(this.project) },
    evidence() { return evidenceLabel(this.project) },
  },
  methods: { rel: relativeTime, perform() { if (this.control?.kind === 'command') this.$emit('action', this.project, this.control.action); else this.$emit('open', this.project, this.control?.tab) } },
}
</script>
<style scoped>
.card { min-width: 0; padding: 22px 22px 16px; display: flex; flex-direction: column; gap: 15px; cursor: pointer; border-top: 2px solid var(--line-2); transition: background .2s, border-color .2s, transform .2s; }
.card:hover { background: var(--panel-2); transform: translateY(-2px); }
.card:focus-visible { outline: 2px solid var(--live); outline-offset: 3px; }
.c-top { display: flex; align-items: center; gap: 8px; }
.c-run { font-size: 11px; color: var(--ink-3); flex: 1; min-width: 0; text-overflow: ellipsis; overflow: hidden; white-space: nowrap; }
h3 { font-size: 17px; line-height: 1.5; font-weight: 600; margin: 0; }
.c-stage { display: flex; align-items: center; gap: 10px; color: var(--ink-2); font-size: 13px; }
.c-stage .mono { color: var(--ink-3); font-size: 12px; }
.c-notice { display: flex; align-items: flex-start; gap: 8px; color: var(--amber); background: var(--amber-dim); border-radius: 6px; padding: 10px; font-size: 12px; line-height: 1.6; }
.c-notice svg { flex-shrink: 0; margin-top: 3px; }
.c-notice.danger { color: var(--bad); background: var(--bad-dim); }
.c-rail { margin-top: auto; padding-top: 5px; display: grid; gap: 12px; }
.c-railmeta { display: flex; justify-content: space-between; gap: 8px; font-size: 12px; color: var(--ink-2); }
.c-evidence { display: flex; flex-wrap: wrap; gap: 8px 14px; color: var(--ink-3); font-size: 11px; }
.c-evidence span { display: inline-flex; align-items: center; gap: 5px; }
.c-evidence .valid, .done { color: var(--ok); }
.c-evidence .invalid { color: var(--bad); }
.c-foot { border-top: 1px solid var(--line); padding-top: 13px; display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.c-time { font-size: 11px; color: var(--ink-3); display: inline-flex; gap: 5px; align-items: center; }
.c-actions .btn { font-size: 12px; }
.tag { font-size: 11px; padding: 5px 8px; white-space: nowrap; }
.ac-running, .ac-archiving { border-top-color: var(--live); }
.ac-completed { border-top-color: var(--ok); }
.ac-interrupted, .ac-failed { border-top-color: var(--bad); }
.ac-retrying, .pending { border-top-color: var(--amber); }
.pending { background: color-mix(in srgb, var(--amber-dim) 32%, var(--panel)); }
.st-running, .st-archiving { color: var(--live); background: var(--live-dim); }
.st-completed { color: var(--ok); background: var(--ok-dim); }
.st-interrupted, .st-failed, .st-killed { color: var(--bad); background: var(--bad-dim); }
.st-retrying, .st-awaiting_selection, .st-awaiting_consultation { color: var(--amber); background: var(--amber-dim); }
.st-paused { color: var(--paused); background: var(--paused-dim); }
</style>
