<template>
  <aside class="action-center" :class="{ clear: !actions.length }" aria-label="行动中心">
    <div class="ac-label">
      <Icon :name="actions.length ? 'alert-triangle' : 'check-circle'" :size="14" />
      <span>行动中心</span>
      <b class="mono">{{ actions.length }}</b>
    </div>
    <div v-if="actions.length" class="ac-list">
      <button
        v-for="item in visibleActions"
        :key="item.id"
        class="ac-item"
        :class="`sev-${item.severity}`"
        @click="$emit('navigate', item)"
      >
        <span class="ac-dot"></span>
        <span class="ac-copy"><strong>{{ item.title }}</strong><small>{{ item.summary }}</small></span>
        <Icon name="chevron-right" :size="13" />
      </button>
      <button v-if="actions.length > limit" class="ac-more" @click="expanded = !expanded">
        {{ expanded ? '收起' : `另有 ${actions.length - limit} 项` }}
      </button>
    </div>
    <span v-else class="ac-clear">当前没有需要立即处理的事项</span>
  </aside>
</template>

<script>
import Icon from './Icon.vue'

export default {
  name: 'ActionCenter',
  components: { Icon },
  props: { actions: { type: Array, default: () => [] } },
  emits: ['navigate'],
  data() { return { expanded: false, limit: 3 } },
  computed: {
    visibleActions() { return this.expanded ? this.actions : this.actions.slice(0, this.limit) },
  },
}
</script>

<style scoped>
.action-center { display: flex; align-items: center; gap: 10px; padding: 7px 20px; border-bottom: 1px solid var(--amber); background: var(--amber-dim); flex-shrink: 0; overflow-x: auto; }
.action-center.clear { border-color: var(--line); background: var(--ok-dim); }
.ac-label { display: flex; align-items: center; gap: 7px; color: var(--amber); font-size: 11px; font-weight: 800; white-space: nowrap; }
.clear .ac-label { color: var(--ok); }
.ac-label b { min-width: 18px; padding: 2px 5px; border: 1px solid currentColor; border-radius: 999px; text-align: center; font-size: 9px; }
.ac-list { display: flex; align-items: center; gap: 7px; min-width: 0; }
.ac-item { max-width: 330px; display: flex; align-items: center; gap: 7px; padding: 5px 8px; border: 1px solid var(--line); border-radius: var(--r-sm); background: var(--panel); color: var(--ink-2); cursor: pointer; text-align: left; }
.ac-item:hover { border-color: var(--amber); color: var(--ink); }
.ac-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--ink-3); flex-shrink: 0; }
.sev-critical .ac-dot, .sev-expired .ac-dot { background: var(--bad); box-shadow: 0 0 0 3px var(--bad-dim); }
.sev-warning .ac-dot { background: var(--amber); }
.ac-copy { display: flex; gap: 6px; align-items: baseline; min-width: 0; }
.ac-copy strong { font-size: 10.5px; white-space: nowrap; }
.ac-copy small { max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ink-3); font-size: 9.5px; }
.ac-more { border: 0; background: transparent; color: var(--ink-2); font: 600 10px var(--sans); white-space: nowrap; cursor: pointer; }
.ac-clear { color: var(--ok); font-size: 11px; }
@media (max-width: 720px) {
  .action-center { align-items: flex-start; padding: 8px 12px; }
  .ac-list { align-items: stretch; }
  .ac-copy { display: block; }
  .ac-copy small { display: block; margin-top: 2px; }
}
</style>
