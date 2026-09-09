<template>
  <section class="audit-status panel" data-testid="audit-status" aria-label="执行与审计状态">
    <header><h2>执行与审计</h2><span class="audit-revision mono">当前状态 · r{{ project.revision ?? '—' }}</span></header>
    <div class="audit-grid" aria-live="polite">
      <article>
        <div class="metric-label"><Icon name="activity" :size="15" />执行状态</div>
        <strong :class="tone">{{ execution }}</strong>
        <p>{{ hint }}</p>
      </article>
      <article>
        <div class="metric-label"><Icon name="shield" :size="15" />证据有效性</div>
        <strong :class="project.evidence_validity === 'VALID' ? 'ok' : project.evidence_validity === 'INVALID' ? 'bad' : ''">{{ evidence }}</strong>
        <p>{{ project.evidence_validity === 'VALID' ? '当前审查所依赖的证据已通过核验。' : project.evidence_validity === 'INVALID' ? '材料或审查绑定发生变化，需要重新核验。' : '等待当前审查结果与证据绑定。' }}</p>
      </article>
      <article>
        <div class="metric-label"><Icon name="scale" :size="15" />科学审查结论</div>
        <strong :class="verdictTone">{{ verdict }}</strong>
        <p v-if="project.review_mode === 'math_only'">仅完成数学预审，后续仍需最终审计。</p>
        <p v-else>{{ project.review_mode === 'final' ? '最终审计结果以当前有效证据为依据。' : '有效结论将在审查完成后显示。' }}</p>
      </article>
      <article>
        <div class="metric-label"><Icon :name="project.delivery_allowed ? 'package' : 'lock'" :size="15" />交付许可</div>
        <strong :class="project.delivery_allowed ? 'ok' : 'muted'">{{ project.delivery_allowed ? '允许交付' : '未获交付许可' }}</strong>
        <p>{{ project.delivery_allowed ? '当前交付检查已通过，可查看交付包。' : '最终审计、冻结与交付检查通过后开放。' }}</p>
      </article>
    </div>
    <div v-if="project.workflow_error || project.evidence_validity === 'INVALID'" class="audit-warning">
      <Icon name="alert-triangle" :size="16" /><span>{{ project.workflow_error === 'RUNNER_EXIT_UNVERIFIED' ? '原运行进程已失联，子进程退出状态尚未确认。请先核对诊断与进程归属。' : project.evidence_validity === 'INVALID' ? '当前证据已失效，需要重新核验后才能确认审查结论与交付许可。' : project.reason_summary || '本次执行遇到错误，请查看诊断。' }}</span>
      <button class="btn btn-sm btn-ghost" @click="$emit('navigate', 'diagnostics')">查看诊断<Icon name="chevron-right" :size="13" /></button>
    </div>
    <footer>
      <details><summary>状态详情</summary><dl>
        <div><dt>执行状态</dt><dd>{{ project.execution_state || project.status }}</dd></div>
        <div><dt>记录状态</dt><dd>{{ project.recorded_workflow_state || '—' }}</dd></div>
        <div v-if="project.workflow_error"><dt>错误代码</dt><dd>{{ project.workflow_error }}</dd></div>
        <div><dt>诊断分数</dt><dd>{{ project.score_available ? project.diagnostic_score : '不可用' }}（仅供诊断）</dd></div>
        <div><dt>正式分数</dt><dd>{{ project.official_score ?? '不可用' }}</dd></div>
        <div v-for="error in project.evidence_errors" :key="error"><dt>证据问题</dt><dd>{{ error }}</dd></div>
      </dl></details>
      <button class="btn btn-sm btn-ghost" @click="$emit('navigate', 'delivery')">查看交付检查<Icon name="chevron-right" :size="13" /></button>
    </footer>
  </section>
</template>
<script setup>
import { computed } from 'vue'
import Icon from './Icon.vue'
import { executionLabel, executionHint, statusTone, evidenceLabel, verdictLabel } from '../lib/projectState.js'
const props = defineProps({ project: { type: Object, required: true } })
defineEmits(['navigate'])
const execution = computed(() => executionLabel(props.project))
const hint = computed(() => executionHint(props.project))
const tone = computed(() => statusTone(props.project))
const evidence = computed(() => evidenceLabel(props.project))
const verdict = computed(() => verdictLabel(props.project))
const verdictTone = computed(() => ['PASS', 'PRECHECK_PASS'].includes(props.project.scientific_verdict) ? 'ok' : String(props.project.scientific_verdict).startsWith('REOPEN') ? 'amber' : '')
</script>
<style scoped>
.audit-status { padding: 22px 24px 12px; }
header { display: flex; justify-content: space-between; gap: 16px; align-items: center; padding-bottom: 20px; }
h2 { font-size: 15px; font-weight: 600; }
.audit-revision { color: var(--ink-3); font-size: 11px; }
.audit-grid { display: grid; grid-template-columns: repeat(4,minmax(0,1fr)); }
article { padding: 0 22px; border-left: 1px solid var(--line); }
article:first-child { padding-left: 0; border-left: 0; }
article:last-child { padding-right: 0; }
.metric-label { display: flex; align-items: center; gap: 7px; font-size: 12px; color: var(--ink-2); }
strong { display: block; margin-top: 14px; font-size: 20px; font-weight: 600; line-height: 1.4; }
p { color: var(--ink-3); margin-top: 9px; line-height: 1.8; font-size: 12px; max-width: 28em; }
.live { color: var(--live); }.ok { color: var(--ok); }.bad { color: var(--bad); }.amber { color: var(--amber); }.muted,.paused { color: var(--ink-2); }
.audit-warning { display: flex; align-items: center; gap: 10px; margin-top: 20px; padding: 12px 14px; background: var(--bad-dim); border-radius: 6px; color: var(--bad); font-size: 13px; }
.audit-warning > svg { flex-shrink: 0; }.audit-warning span { flex: 1; }.audit-warning button { color: var(--bad); white-space: nowrap; }
footer { display: flex; align-items: flex-start; justify-content: space-between; border-top: 1px solid var(--line); margin-top: 22px; padding-top: 10px; gap: 10px; }
summary { font-size: 12px; color: var(--ink-3); cursor: pointer; padding: 6px 0; }
dl { display: grid; gap: 8px; padding: 10px 0; font-size: 12px; }
dl > div { display: flex; gap: 16px; }dt { min-width: 5em; color: var(--ink-3); }dd { color: var(--ink-2); overflow-wrap: anywhere; }
@media(max-width: 1000px) { .audit-grid { grid-template-columns: 1fr 1fr; gap: 24px 0; }article:nth-child(3) { border-left: 0; padding-left: 0; } }
@media(max-width: 600px) { .audit-status { padding: 18px; } .audit-grid { grid-template-columns: 1fr; gap: 20px; } article { padding: 0 0 18px; border-left: 0; border-bottom: 1px solid var(--line); } article:last-child { border-bottom: 0; padding-bottom: 0; }strong { margin-top: 8px; } .audit-warning { flex-wrap: wrap; } .audit-warning span { flex-basis: 80%; } }
</style>
