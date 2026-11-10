<template>
  <section class="todo panel" :class="{ clear: !actions.length }" aria-label="待办">
    <header class="todo-head">
      <Icon :name="actions.length ? 'alert-triangle' : 'check-circle'" :size="15" />
      <span>{{ actions.length ? `待办 ${actions.length} 项` : '没有需要你处理的事项' }}</span>
    </header>
    <div v-if="actions.length" class="todo-list">
      <button
        v-for="item in visibleActions"
        :key="item.id"
        class="todo-item"
        :class="`sev-${item.severity}`"
        @click="$emit('navigate', item)"
      >
        <span class="sev-dot"></span>
        <span class="copy"><strong>{{ item.title }}</strong><small>{{ item.summary }}</small></span>
        <Icon name="chevron-right" :size="15" class="go" />
      </button>
      <button v-if="actions.length > limit" class="more" @click="expanded = !expanded">
        {{ expanded ? '收起' : `显示其余 ${actions.length - limit} 项` }}
      </button>
    </div>
  </section>
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
.todo { overflow: hidden; }
.todo-head { display: flex; align-items: center; gap: 8px; padding: 11px 16px; font-weight: 600; font-size: 13.5px; color: var(--amber); background: var(--amber-dim); }
.clear .todo-head { color: var(--ok); background: transparent; font-weight: 500; }
.todo-list { display: flex; flex-direction: column; }
.todo-item {
  display: grid; grid-template-columns: 8px minmax(0, 1fr) 16px; align-items: center; gap: 12px;
  padding: 11px 16px; border: 0; border-top: 1px solid var(--line);
  background: var(--surface); color: var(--ink); text-align: left; cursor: pointer; font: inherit;
}
.todo-item:hover { background: var(--panel-2); }
.sev-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--ink-3); }
.sev-critical .sev-dot, .sev-expired .sev-dot { background: var(--bad); }
.sev-warning .sev-dot { background: var(--amber); }
.sev-info .sev-dot { background: var(--accent); }
.copy { display: flex; flex-direction: column; min-width: 0; }
.copy strong { font-weight: 500; font-size: 13.5px; }
.copy small { color: var(--ink-2); font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.go { color: var(--ink-3); }
.more { border: 0; border-top: 1px solid var(--line); background: var(--surface); color: var(--accent); padding: 9px 16px; text-align: left; cursor: pointer; font: 500 13px var(--sans); }
.more:hover { background: var(--panel-2); }
</style>
