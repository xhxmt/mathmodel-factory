<template>
  <div class="rail" :class="{ compact }" role="img" :aria-label="`流水线进度：第 ${Math.max(0, currentStep)} / 16 步`">
    <template v-for="s in STEPS" :key="s.index">
      <span class="node" :class="['st-' + state(s), 'kind-' + s.kind]" :title="tip(s)">
        <span v-if="!compact" class="num">{{ s.index }}</span>
      </span>
      <span v-if="s.index < 16" class="seg" :class="{ on: s.index <= currentStep }"></span>
    </template>
  </div>
</template>

<script>
import { STEPS, stepStatus } from '../lib/steps.js'

export default {
  name: 'StepRail',
  props: {
    currentStep: { type: Number, default: -1 },
    awaiting: { type: Boolean, default: false },
    compact: { type: Boolean, default: false },
  },
  data() { return { STEPS } },
  methods: {
    state(s) {
      const st = stepStatus(s.index, this.currentStep)
      if (st === 'active') return this.awaiting ? 'attention' : 'live'
      return st
    },
    tip(s) {
      const kind = { gate: ' · 质检关卡', human: ' · 人工介入', setup: '', normal: '' }[s.kind] || ''
      return `Step ${s.index} · ${s.name} (${s.en})${kind}`
    },
  },
}
</script>

<style scoped>
.rail {
  display: flex;
  align-items: center;
  width: 100%;
  gap: 0;
}

.node {
  --c: var(--ink-3);
  position: relative;
  width: 22px; height: 22px;
  flex-shrink: 0;
  border-radius: 50%;
  border: 1.5px solid var(--c);
  background: var(--panel);
  display: flex; align-items: center; justify-content: center;
  color: var(--c);
  transition: all 0.3s var(--ease);
  z-index: 1;
}
.rail.compact .node { width: 9px; height: 9px; border-width: 1.5px; }

.num { font: 600 9.5px/1 var(--mono); }

.seg {
  flex: 1;
  height: 2px;
  min-width: 4px;
  background: var(--line-2);
  transition: background 0.4s var(--ease);
}
.seg.on { background: var(--ok); }

/* shapes per kind */
.kind-gate { border-radius: 3px; transform: rotate(45deg); }
.kind-gate .num { transform: rotate(-45deg); }
.kind-human { box-shadow: 0 0 0 3px var(--panel), 0 0 0 4.5px var(--c); }

/* states */
.st-done   { --c: var(--ok);    background: var(--ok); color: #06140f; }
.st-done .num { color: #06140f; }
.st-live   { --c: var(--live);  background: var(--live); color: #04161c; box-shadow: 0 0 0 4px var(--live-dim); animation: livepulse 2s var(--ease) infinite; }
.st-live .num { color: #04161c; }
.st-attention { --c: var(--amber); background: var(--amber); color: var(--amber-ink); box-shadow: 0 0 0 4px var(--amber-dim); animation: attnpulse 1.3s var(--ease) infinite; }
.st-attention .num { color: var(--amber-ink); }
.st-pending { --c: var(--ink-3); background: var(--panel); }

.kind-human.st-live, .kind-human.st-attention { box-shadow: 0 0 0 3px var(--panel), 0 0 0 4.5px var(--c); }

@keyframes livepulse { 0%,100% { box-shadow: 0 0 0 4px var(--live-dim); } 50% { box-shadow: 0 0 0 6px transparent; } }
@keyframes attnpulse { 0%,100% { box-shadow: 0 0 0 4px var(--amber-dim); } 50% { box-shadow: 0 0 0 7px transparent; } }
</style>
