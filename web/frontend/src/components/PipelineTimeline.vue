<template>
  <div class="pipeline panel">
    <!-- header -->
    <div class="pl-head">
      <div class="pl-title">
        <Icon name="layers" :size="15" />
        <span class="label">流水线 · PIPELINE</span>
      </div>
      <div class="pl-meta">
        <span class="mono tnum step-counter">
          STEP {{ String(Math.max(0, displayStep)).padStart(2, '0') }} / 16
        </span>
        <span v-if="verdict" class="tag" :class="verdictClass">
          <Icon name="scale" :size="12" /> {{ verdictLabel }}
        </span>
        <button
          v-if="openIssues > 0"
          type="button"
          class="tag tag-amber issue-toggle"
          :aria-expanded="issuesOpen ? 'true' : 'false'"
          aria-controls="pipeline-open-issues"
          @click="issuesOpen = !issuesOpen"
        >
          <Icon name="alert-triangle" :size="12" />
          {{ openIssues }} 待办
          <Icon :name="issuesOpen ? 'chevron-up' : 'chevron-down'" :size="12" />
        </button>
        <button v-if="paperAvailable" class="btn btn-sm btn-amber" @click="$emit('open-paper')">
          <Icon name="book-open" :size="13" /> 查看论文
        </button>
      </div>
    </div>

    <section v-if="issuesOpen" id="pipeline-open-issues" class="issue-panel">
      <div class="issue-head">
        <div>
          <div class="issue-title">未解决事项</div>
          <div class="issue-summary mono">{{ openIssues }} 项来自审计台账</div>
        </div>
        <button
          type="button"
          class="btn btn-sm btn-ghost"
          @click="$emit('open-file', { path: 'audit_issue_ledger.md', type: 'markdown', name: 'audit_issue_ledger.md' })"
        >
          <Icon name="file-text" :size="13" /> 查看完整台账
        </button>
      </div>

      <div v-if="openIssueItems.length" class="issue-list">
        <article v-for="(item, index) in openIssueItems" :key="item.id || index" class="issue-row">
          <div class="issue-row-top">
            <span class="issue-id mono">{{ item.id || `ISSUE-${index + 1}` }}</span>
            <span v-if="item.step" class="issue-step mono">STEP {{ item.step }}</span>
            <span v-if="item.severity" class="issue-severity mono">{{ item.severity }}</span>
            <span class="issue-status mono">{{ item.status || 'OPEN' }}</span>
          </div>
          <div class="issue-text issue-rich" v-html="md(item.issue)"></div>
          <div v-if="item.location" class="issue-meta">
            <span class="issue-meta-label">位置</span>
            <div class="issue-rich mono" v-html="md(item.location)"></div>
          </div>
          <div v-if="item.required_action" class="issue-action">
            <span class="issue-meta-label">处理</span>
            <div class="issue-rich" v-html="md(item.required_action)"></div>
          </div>
        </article>
      </div>
      <div v-else class="issue-empty">待办明细未返回，请查看完整台账。</div>
    </section>

    <!-- track -->
    <div class="track-scroll">
      <div class="track">
        <div
          v-for="s in timelineSteps"
          :key="s.key || s.index"
          class="col"
          :class="{ 'seg-on': isSegmentOn(s) }"
        >
          <button
            class="node"
            :class="['st-' + state(s), 'kind-' + s.kind, { sel: stepId(s) === selectedIndex }]"
            :title="tip(s)"
            @click="select(stepId(s))"
          >
            <span class="num">{{ s.key === '8_5' ? '8.5' : s.index }}</span>
          </button>
          <div class="cap" :class="{ show: capFor(s) }">{{ capFor(s) }}</div>
        </div>
      </div>
    </div>

    <!-- selected step detail -->
    <div class="detail" :key="selectedIndex">
      <div class="d-left">
        <div class="d-badge" :class="'kind-' + sel.kind">
          <span class="mono">{{ sel.key === '8_5' ? '8.5' : sel.index }}</span>
        </div>
        <div class="d-titles">
          <div class="d-name">
            {{ sel.name }}
            <span v-if="sel.kind === 'gate'" class="kind-pill gate">质检关卡</span>
            <span v-if="sel.kind === 'human'" class="kind-pill human">人工介入</span>
          </div>
          <div class="d-en mono">{{ sel.en }}</div>
        </div>
      </div>

      <div class="d-right">
        <span class="tag" :class="'state-' + state(sel)">
          <span class="dot" :class="dotFor(state(sel))"></span>
          {{ stateLabel(state(sel)) }}
        </span>

        <div v-if="selectedIndex === 13 && verdict" class="tag" :class="verdictClass">
          {{ verdictLabel }}
        </div>

        <div class="artifacts">
          <button
            v-for="a in selArtifacts"
            :key="a.path"
            class="chip"
            :title="a.path"
            @click="$emit('open-file', a)"
          >
            <Icon :name="iconFor(a.type)" :size="12" />
            <span class="chip-name">{{ a.name }}</span>
          </button>
          <span v-if="selArtifacts.length === 0" class="no-art">— 暂无产物文件 —</span>
        </div>
      </div>

      <!-- per-step model selection (this project) -->
      <div v-if="selMeta.overridable" class="d-models">
        <div class="dm-head">
          <Icon name="cpu" :size="13" />
          <span class="label">本步模型</span>
          <span class="dm-def mono">内置默认：{{ selMeta.default }}</span>
          <button class="dm-manage" @click="$emit('manage-models')">管理模型库 →</button>
        </div>
        <div class="dm-row">
          <label class="dm-field">
            <span class="dm-l">主模型</span>
            <select class="field dm-sel" :value="selAssign.primary || ''" @change="emitAssign('primary', $event.target.value)">
              <option value="">跟随默认</option>
              <option v-for="m in pickableModels" :key="m.id" :value="m.id">{{ m.label }}</option>
            </select>
          </label>
          <label class="dm-field">
            <span class="dm-l">备用</span>
            <select class="field dm-sel" :value="selAssign.fallback || ''" @change="emitAssign('fallback', $event.target.value)" :disabled="!selAssign.primary">
              <option value="">无</option>
              <option v-for="m in pickableModels" :key="m.id" :value="m.id">{{ m.label }}</option>
            </select>
          </label>
          <span class="dm-note" :class="{ api: selMeta.apiOk }">
            {{ selMeta.apiOk ? '本步可用 API 模型（评委/评审/评价）' : '本步仅 agentic 模型（claude/codex/agy）' }}
          </span>
        </div>
      </div>
      <div v-else class="d-models locked">
        <Icon name="lock" :size="12" /> 本步骤不支持单独选模型 · {{ selMeta.default }}
      </div>
    </div>
  </div>
</template>

<script>
import Icon from './Icon.vue'
import { STEPS, EDITORIAL_GATE_STEP, stepStatus, VERDICT_LABEL, stepModelMeta, stepConfigKey } from '../lib/steps.js'
import { renderMarkdown } from '../lib/markdown.js'

export default {
  name: 'PipelineTimeline',
  components: { Icon },
  props: {
    currentStep: { type: Number, default: -1 },
    stepsData: { type: Object, default: null },
    awaiting: { type: Boolean, default: false },
    registry: { type: Array, default: () => [] },
    assignments: { type: Object, default: () => ({}) },
  },
  emits: ['open-file', 'open-paper', 'assign', 'manage-models'],
  data() {
    return { selectedIndex: this.defaultIndex(), userPicked: false, issuesOpen: false }
  },
  computed: {
    displayStep() { return this.currentStep },
    verdict() { return this.stepsData?.verdict || null },
    verdictLabel() { return VERDICT_LABEL[this.verdict] || this.verdict },
    verdictClass() { return this.verdict === 'PASS' ? 'tag-ok' : 'tag-amber' },
    openIssues() { return this.stepsData?.open_issues || 0 },
    openIssueItems() { return this.stepsData?.open_issue_items || [] },
    paperAvailable() { return !!this.stepsData?.paper_available },
    timelineSteps() {
      return [...STEPS.slice(0, 9), EDITORIAL_GATE_STEP, ...STEPS.slice(9)]
    },
    sel() { return this.timelineSteps.find((s) => this.stepId(s) === this.selectedIndex) || this.timelineSteps[0] },
    selArtifacts() {
      if (this.sel.key === '8_5') return this.stepsData?.editorial_gate?.artifacts || []
      return this.stepsData?.steps?.[this.sel.index]?.artifacts || []
    },
    selMeta() { return stepModelMeta(this.sel.key || this.sel.index) },
    selAssign() { return this.assignments?.[stepConfigKey(this.sel)] || {} },
    // Models offerable for the selected step: enabled, plus API backends only
    // where the step accepts a non-agentic model.
    pickableModels() {
      const apiOk = this.selMeta.apiOk
      const AG = ['claude', 'codex', 'agy']
      return (this.registry || []).filter(
        (m) => m.enabled && m.id && (apiOk || AG.includes(m.backend))
      )
    },
  },
  watch: {
    currentStep() { if (!this.userPicked) this.selectedIndex = this.defaultIndex() },
    stepsData() { if (!this.userPicked) this.selectedIndex = this.defaultIndex() },
  },
  methods: {
    md(value) { return renderMarkdown(value) },
    stepId(s) { return s.key || s.index },
    isSegmentOn(s) {
      if (s.key === '8_5') return this.currentStep >= 8
      return s.index <= this.currentStep && s.index > 0
    },
    defaultIndex() {
      const c = this.currentStep
      const gate = this.stepsData?.editorial_gate
      if (c === 8 && gate && !gate.ready) return '8_5'
      if (c >= 16) return 16
      return Math.min(16, Math.max(0, c + 1))
    },
    select(i) { this.selectedIndex = i; this.userPicked = true },
    emitAssign(field, val) {
      const cur = { primary: this.selAssign.primary || '', fallback: this.selAssign.fallback || '' }
      cur[field] = val
      // Clearing the primary clears the whole override for this step.
      if (field === 'primary' && !val) cur.fallback = ''
      this.$emit('assign', this.sel, cur)
    },
    state(s) {
      if (s.key === '8_5') {
        const gate = this.stepsData?.editorial_gate
        if (!gate) return 'pending'
        if (gate.ready) return 'done'
        if (this.currentStep >= 8) return this.awaiting ? 'attention' : 'live'
        return 'pending'
      }
      const st = stepStatus(s.index, this.currentStep)
      if (st === 'active') return this.awaiting ? 'attention' : 'live'
      return st
    },
    stateLabel(st) {
      return { done: '已完成', live: '进行中', attention: '等待你', pending: '待执行' }[st] || st
    },
    dotFor(st) {
      return { done: 'ok', live: 'live', attention: 'amber', pending: '' }[st] || ''
    },
    capFor(s) {
      if (s.key === '8_5') return 'ENTRY'
      if (s.index === 10) return 'GATE 1'
      if (s.index === 13) return 'GATE 2'
      if (s.index === 3) return '选型'
      if (s.index === 14) return '摘要'
      if (s.index === 0) return 'SETUP'
      if (s.index === 16) return 'DONE'
      return ''
    },
    tip(s) {
      const k = { gate: ' · 质检关卡', human: ' · 人工介入' }[s.kind] || ''
      const id = s.key === '8_5' ? '8.5' : s.index
      return `Step ${id} · ${s.name} (${s.en})${k}`
    },
    iconFor(t) {
      return { image: 'image', pdf: 'file-text', csv: 'table', json: 'code', code: 'code', markdown: 'file-text', text: 'file-text' }[t] || 'file'
    },
  },
}
</script>

<style scoped>
.pipeline { min-width: 0; max-width: 100%; padding: 16px 18px 18px; }

.pl-head {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; flex-wrap: wrap; margin-bottom: 18px;
}
.pl-title { display: flex; align-items: center; gap: 8px; color: var(--ink-2); }
.pl-meta { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }
.step-counter { font-size: 12px; color: var(--ink-2); letter-spacing: 0.08em; }
.issue-toggle { cursor: pointer; }
.issue-toggle:hover { border-color: var(--amber); }

.issue-panel { margin: -4px 0 18px; padding: 0 0 16px; border-bottom: 1px solid var(--line); }
.issue-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.issue-title { font-size: 14px; font-weight: 700; color: var(--ink); }
.issue-summary { margin-top: 3px; font-size: 10.5px; color: var(--ink-3); }
.issue-list { max-height: 360px; overflow-y: auto; border-top: 1px solid var(--line); }
.issue-row { padding: 12px 2px; border-bottom: 1px solid var(--line); }
.issue-row:last-child { border-bottom: 0; }
.issue-row-top { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.issue-id { font-size: 11px; font-weight: 700; color: var(--ink); }
.issue-step, .issue-severity, .issue-status { font-size: 9.5px; padding: 3px 6px; border-radius: var(--r-xs); background: var(--panel-2); color: var(--ink-3); }
.issue-severity, .issue-status { color: var(--amber); background: var(--amber-dim); }
.issue-text { margin-top: 7px; font-size: 13px; line-height: 1.55; color: var(--ink); overflow-wrap: anywhere; }
.issue-meta, .issue-action { display: grid; grid-template-columns: 42px minmax(0, 1fr); gap: 8px; margin-top: 7px; font-size: 11.5px; line-height: 1.5; color: var(--ink-2); overflow-wrap: anywhere; }
.issue-meta-label { color: var(--ink-3); }
.issue-rich { min-width: 0; max-width: 100%; overflow-x: auto; overflow-y: hidden; }
.issue-rich :deep(p) { margin: 0; }
.issue-rich :deep(.md-code) { font-family: var(--mono); font-size: 0.9em; white-space: normal; overflow-wrap: anywhere; background: var(--panel-2); border: 1px solid var(--line); padding: 0.08em 0.35em; border-radius: var(--r-xs); color: var(--live); }
.issue-rich :deep(.katex) { max-width: 100%; font-size: 0.98em; }
.issue-rich :deep(.katex-display) { max-width: 100%; overflow-x: auto; overflow-y: hidden; margin: 0.45em 0; }
.issue-empty { padding: 16px 0; color: var(--ink-3); font-size: 12px; }

/* ---- track ---- */
.track-scroll { width: 100%; max-width: 100%; overflow-x: auto; padding: 4px 2px 2px; margin: 0 -4px; }
.track { display: flex; min-width: 640px; }
.col {
  position: relative;
  flex: 1;
  display: flex; flex-direction: column; align-items: center;
  padding-top: 2px;
}
/* connector entering from the left */
.col::before {
  content: '';
  position: absolute;
  top: 15px; left: -50%; width: 100%; height: 2px;
  background: var(--line-2);
  z-index: 0;
}
.col:first-child::before { display: none; }
.col.seg-on::before { background: var(--ok); }

.node {
  position: relative; z-index: 1;
  width: 28px; height: 28px;
  border-radius: 50%;
  border: 1.5px solid var(--c, var(--ink-3));
  background: var(--panel);
  color: var(--c, var(--ink-3));
  display: flex; align-items: center; justify-content: center;
  cursor: pointer;
  transition: all 0.25s var(--ease);
}
.node:hover { transform: translateY(-2px); }
.node .num { font: 700 11px/1 var(--mono); }

.kind-gate { border-radius: 4px; transform: rotate(45deg); }
.kind-gate:hover { transform: rotate(45deg) translateY(-2px); }
.kind-gate .num { transform: rotate(-45deg); }
.kind-human { box-shadow: 0 0 0 3px var(--panel), 0 0 0 4.5px var(--c, var(--ink-3)); }

.st-done   { --c: var(--ok);    background: var(--ok); color: #06140f; }
.st-live   { --c: var(--live);  background: var(--live); color: #04161c; animation: lp 2s var(--ease) infinite; }
.st-attention { --c: var(--amber); background: var(--amber); color: var(--amber-ink); animation: ap 1.3s var(--ease) infinite; }
.st-pending { --c: var(--ink-3); background: var(--panel); }

.node.sel { outline: 2px solid var(--ink); outline-offset: 3px; }
.kind-gate.sel { outline-offset: 3px; }

@keyframes lp { 0%,100% { box-shadow: 0 0 0 4px var(--live-dim); } 50% { box-shadow: 0 0 0 7px transparent; } }
@keyframes ap { 0%,100% { box-shadow: 0 0 0 4px var(--amber-dim); } 50% { box-shadow: 0 0 0 8px transparent; } }

.cap {
  margin-top: 9px; height: 12px;
  font: 500 9px/1 var(--mono); letter-spacing: 0.1em;
  color: var(--ink-3); text-transform: uppercase;
  white-space: nowrap; opacity: 0;
}
.cap.show { opacity: 1; }

/* ---- detail ---- */
.detail {
  margin-top: 20px; padding-top: 18px;
  border-top: 1px solid var(--line);
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 18px; flex-wrap: wrap;
  animation: fade 0.3s var(--ease);
}
@keyframes fade { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }

.d-left { display: flex; align-items: center; gap: 14px; min-width: 0; }
.d-badge {
  width: 42px; height: 42px; border-radius: var(--r);
  display: flex; align-items: center; justify-content: center;
  background: var(--panel-2); border: 1px solid var(--line-2);
  font-size: 16px; font-weight: 700; color: var(--ink);
  flex-shrink: 0;
}
.d-badge.kind-gate { border-color: var(--amber-line); color: var(--amber); }
.d-badge.kind-human { border-color: var(--live); color: var(--live); }
.d-titles { min-width: 0; }
.d-name { font-size: 16px; font-weight: 700; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.d-en { font-size: 11px; color: var(--ink-3); letter-spacing: 0.08em; margin-top: 2px; }

.kind-pill { font: 600 9.5px/1 var(--mono); letter-spacing: 0.08em; padding: 3px 6px; border-radius: var(--r-xs); }
.kind-pill.gate { background: var(--amber-dim); color: var(--amber); }
.kind-pill.human { background: var(--live-dim); color: var(--live); }

.d-right { display: flex; flex-direction: column; align-items: flex-end; gap: 10px; }

.artifacts { display: flex; flex-wrap: wrap; gap: 6px; justify-content: flex-end; max-width: 460px; }
.chip {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 5px 9px; border-radius: var(--r-sm);
  background: var(--panel-2); border: 1px solid var(--line);
  color: var(--ink-2); cursor: pointer;
  font: 500 11.5px/1 var(--mono);
  transition: all 0.14s var(--ease);
}
.chip:hover { background: var(--panel-3); color: var(--ink); border-color: var(--live); }
.chip-name { max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.no-art { font: 500 11px/1 var(--mono); color: var(--ink-3); }

/* ---- per-step model selection ---- */
.d-models { flex-basis: 100%; margin-top: 4px; padding-top: 14px; border-top: 1px dashed var(--line); }
.d-models.locked { display: flex; align-items: center; gap: 7px; font: 500 11.5px/1.4 var(--mono); color: var(--ink-3); }
.dm-head { display: flex; align-items: center; gap: 9px; margin-bottom: 9px; }
.dm-def { font-size: 10.5px; color: var(--ink-3); }
.dm-manage { margin-left: auto; background: none; border: none; color: var(--live); font: 600 11px/1 var(--sans); cursor: pointer; padding: 0; }
.dm-manage:hover { text-decoration: underline; }
.dm-row { display: flex; align-items: flex-end; gap: 10px; flex-wrap: wrap; }
.dm-field { display: flex; flex-direction: column; gap: 4px; }
.dm-l { font: 500 9.5px/1 var(--mono); letter-spacing: 0.1em; text-transform: uppercase; color: var(--ink-3); }
.dm-sel { width: 210px; padding: 8px 26px 8px 10px; font-size: 12.5px; cursor: pointer;
  appearance: none;
  background-image: linear-gradient(45deg, transparent 50%, var(--ink-3) 50%), linear-gradient(135deg, var(--ink-3) 50%, transparent 50%);
  background-position: calc(100% - 14px) center, calc(100% - 9px) center;
  background-size: 5px 5px, 5px 5px; background-repeat: no-repeat; }
.dm-sel:disabled { opacity: 0.45; cursor: not-allowed; }
.dm-note { font: 500 10.5px/1.4 var(--mono); color: var(--ink-3); padding-bottom: 9px; }
.dm-note.api { color: var(--amber); }

/* ---- tags ---- */
.tag {
  display: inline-flex; align-items: center; gap: 6px;
  font: 600 11px/1 var(--mono); letter-spacing: 0.06em; text-transform: uppercase;
  padding: 6px 9px; border-radius: var(--r-sm);
  border: 1px solid var(--line); background: var(--panel-2); color: var(--ink-2);
}
.tag-ok { color: var(--ok); border-color: var(--ok-dim); background: var(--ok-dim); }
.tag-amber { color: var(--amber); border-color: var(--amber-line); background: var(--amber-dim); }
.state-done { color: var(--ok); }
.state-live { color: var(--live); }
.state-attention { color: var(--amber); border-color: var(--amber-line); background: var(--amber-dim); }
.state-pending { color: var(--ink-3); }
@media (max-width: 640px) {
  .pl-meta { width: 100%; min-width: 0; }
  .issue-head { align-items: flex-start; }
  .issue-head { flex-wrap: wrap; }
  .issue-head .btn { flex-shrink: 0; }
  .d-right, .artifacts { min-width: 0; max-width: 100%; }
  .issue-meta, .issue-action { grid-template-columns: 36px minmax(0, 1fr); }
}
</style>
