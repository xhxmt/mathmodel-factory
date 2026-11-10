<template>
  <section class="timing panel" :class="`risk-${view.level}`">
    <div class="timing-head">
      <div class="timing-title"><Icon name="clock" :size="15" /><span>时间风险预测</span></div>
      <span class="risk-pill">{{ view.label }}</span>
    </div>
    <div class="mode-row">
      <strong>{{ view.modeLabel }}</strong>
      <span>{{ view.recommendation }}</span>
    </div>
    <div class="timing-grid">
      <div><span>最近三步平均</span><b class="mono">{{ view.average }}</b></div>
      <div><span>预计内容完成</span><b class="mono">{{ view.projectedAt }}</b></div>
      <div><span>内容冻结余量</span><b class="mono">{{ view.slack }}</b></div>
      <div><span>预测依据</span><b>{{ timing.forecast_confidence === 'observed' ? '真实运行耗时' : '默认 1h / Step' }}</b></div>
    </div>
    <div v-if="view.split" class="timing-grid">
      <div><span>工作时间</span><b class="mono">{{ view.split.working }}</b></div>
      <div><span>等待外部依赖</span><b class="mono">{{ view.split.waitingExternal }}</b></div>
      <div><span>其中：等人工 / 等外部服务</span><b class="mono">{{ view.split.waitingHuman }} / {{ view.split.waitingProvider }}</b></div>
      <div><span>已停止（暂停、失败、空闲）</span><b class="mono">{{ view.split.stopped }}</b></div>
    </div>
  </section>
</template>

<script>
import { computed } from 'vue'
import Icon from './Icon.vue'
import { timingPresentation } from '../lib/workspaceUi.js'

export default {
  name: 'ContestTimingPanel',
  components: { Icon },
  props: { timing: { type: Object, default: () => ({}) } },
  setup(props) { return { view: computed(() => timingPresentation(props.timing)) } },
}
</script>

<style scoped>
.timing { padding: 16px 18px; }
.timing-head, .mode-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.timing-title { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 600; }
.risk-pill { padding: 3px 7px; border: 1px solid currentColor; border-radius: 999px; color: var(--ink-2); font-size: 11.5px; }
/* Deadline risk stays scannable: amber when it needs attention, red when the
   content freeze is at risk or past; a safe forecast keeps the neutral pill. */
.risk-guarded .risk-pill, .risk-warning .risk-pill { color: var(--amber); background: var(--amber-dim); border-color: var(--amber-line); }
.risk-critical .risk-pill, .risk-expired .risk-pill { color: var(--bad); background: var(--bad-dim); border-color: var(--bad); }
.risk-critical, .risk-expired { border-color: var(--bad); }
.mode-row { margin-top: 10px; padding: 9px 10px; border-radius: var(--r-sm); background: var(--panel-2); }
.mode-row strong { font-size: 12px; }
.mode-row span { color: var(--ink-2); font-size: 11px; text-align: right; }
.timing-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin-top: 10px; }
.timing-grid div { display: flex; flex-direction: column; gap: 4px; padding: 9px 10px; border: 1px solid var(--line); border-radius: var(--r-sm); }
.timing-grid span { color: var(--ink-3); font-size: 11.5px; }
.timing-grid b { font-size: 11px; overflow-wrap: anywhere; }
@media (max-width: 760px) {
  .mode-row { align-items: flex-start; flex-direction: column; }
  .mode-row span { text-align: left; }
  .timing-grid { grid-template-columns: 1fr 1fr; }
}
</style>
