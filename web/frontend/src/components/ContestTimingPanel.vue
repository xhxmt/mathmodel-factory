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
.timing { padding: 14px; border-left: 3px solid var(--line-2); }
.risk-safe { border-left-color: var(--ok); }
.risk-guarded, .risk-warning { border-left-color: var(--amber); }
.risk-critical, .risk-expired { border-left-color: var(--bad); }
.timing-head, .mode-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.timing-title { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 800; }
.risk-pill { padding: 3px 7px; border: 1px solid currentColor; border-radius: 999px; color: var(--ink-2); font-size: 10px; }
.mode-row { margin-top: 10px; padding: 9px 10px; border-radius: var(--r-sm); background: var(--panel-2); }
.mode-row strong { font-size: 12px; }
.mode-row span { color: var(--ink-2); font-size: 11px; text-align: right; }
.timing-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin-top: 10px; }
.timing-grid div { display: flex; flex-direction: column; gap: 4px; padding: 9px 10px; border: 1px solid var(--line); border-radius: var(--r-sm); }
.timing-grid span { color: var(--ink-3); font-size: 9.5px; }
.timing-grid b { font-size: 11px; overflow-wrap: anywhere; }
@media (max-width: 760px) {
  .mode-row { align-items: flex-start; flex-direction: column; }
  .mode-row span { text-align: left; }
  .timing-grid { grid-template-columns: 1fr 1fr; }
}
</style>
