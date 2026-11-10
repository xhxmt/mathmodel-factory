<template>
  <section class="stage-panel panel" aria-label="十阶段调度进度">
    <header><span class="stage-count">已完成 {{ Math.min(10, project.last_completed_stage || 0) }} / 10 个阶段</span></header>
    <ol><li v-for="stage in stages" :key="stage.id" :class="state(stage)" :aria-current="stage.id === active?.id && executionStatus(project) !== 'completed' ? 'step' : undefined">
      <span class="stage-number mono"><Icon v-if="state(stage) === 'done'" name="check" :size="13" /><template v-else>{{ String(stage.id).padStart(2, '0') }}</template></span>
      <span class="stage-name">{{ stage.name }}</span>
      <span class="stage-steps mono">Step {{ stepRangeLabel(stage.steps) }}</span>
    </li></ol>
  </section>
</template>
<script setup>
import { computed } from 'vue'
import Icon from './Icon.vue'
import { currentStage, executionStatus, stepText, statusTone } from '../lib/projectState.js'
import { stagesFor, stepRangeLabel } from '../lib/stageCatalog.js'
const props = defineProps({ project: { type: Object, required: true } })
const stages = computed(() => stagesFor(props.project))
const active = computed(() => currentStage(props.project))
function state(stage) {
  if (stage.id === active.value?.id && executionStatus(props.project) !== 'completed') return 'current ' + statusTone(props.project)
  return stage.id <= Number(props.project.last_completed_stage || 0) || executionStatus(props.project) === 'completed' ? 'done' : 'pending'
}
</script>
<style scoped>
.stage-panel { padding: 22px 24px; }header { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.stage-kicker { font-size: 11.5px; letter-spacing: 0; color: var(--ink-3); }h2 { margin-top: 5px; font-size: 18px; font-weight: 600; }.stage-count { color: var(--ink-3); font-size: 11px; white-space: nowrap; }
ol { list-style: none; display: grid; grid-template-columns: repeat(10,minmax(0,1fr)); gap: 6px; margin-top: 22px; }
li { display: flex; flex-direction: column; gap: 7px; padding: 12px 7px; border-top: 2px solid var(--line); border-radius: 0 0 5px 5px; color: var(--ink-3); }
.stage-number { height: 20px; font-size: 12px; }.stage-name { font-size: 12px; white-space: nowrap; }.stage-steps { font-size: 11.5px; opacity: .7; }
.done { color: var(--ok); border-color: color-mix(in srgb,var(--ok) 45%,var(--line)); }.current { border-color: var(--live); background: var(--live-dim); color: var(--live); }
.current.amber { border-color: var(--amber); background: var(--amber-dim); color: var(--amber); }.current.bad { border-color: var(--bad); background: var(--bad-dim); color: var(--bad); }.current.paused { border-color: var(--paused); background: var(--paused-dim); color: var(--paused); }
@media(max-width: 1100px) { ol { grid-template-columns: repeat(5,1fr); row-gap: 10px; } }
@media(max-width: 600px) { .stage-panel { padding: 18px; }header { align-items: flex-start; }.stage-count { font-size: 11.5px; }h2 { font-size: 15px; }.stage-name { font-size: 11px; }li { padding: 9px 2px; }.stage-steps { font-size: 11.5px; } }
</style>
