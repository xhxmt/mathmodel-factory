<template>
  <div class="toast-wrap" aria-live="polite">
    <transition-group name="toast">
      <div v-for="t in toasts" :key="t.id" class="toast panel" :class="'t-' + t.type" @click="dismiss(t.id)">
        <span class="t-ic"><Icon :name="iconFor(t.type)" :size="16" /></span>
        <div class="t-body">
          <div v-if="t.title" class="t-title mono">{{ t.title }}</div>
          <div class="t-msg">{{ t.message }}</div>
        </div>
        <button class="t-x" @click.stop="dismiss(t.id)"><Icon name="x" :size="13" /></button>
      </div>
    </transition-group>
  </div>
</template>

<script>
import Icon from './Icon.vue'
import { useToasts } from '../composables/useToasts.js'

export default {
  name: 'Toasts',
  components: { Icon },
  setup() { const { toasts, dismiss } = useToasts(); return { toasts, dismiss } },
  methods: {
    iconFor(t) { return { ok: 'check-circle', bad: 'alert-triangle', amber: 'alert-triangle', info: 'info' }[t] || 'info' },
  },
}
</script>

<style scoped>
.toast-wrap { position: fixed; right: 18px; bottom: 18px; z-index: 400; display: flex; flex-direction: column; gap: 10px; max-width: 380px; }
.toast {
  display: flex; align-items: flex-start; gap: 11px;
  padding: 12px 13px; cursor: pointer;
  border-left: 3px solid var(--ink-3);
  box-shadow: var(--shadow);
}
.t-ic { flex-shrink: 0; margin-top: 1px; color: var(--ink-2); }
.t-body { min-width: 0; flex: 1; }
.t-title { font-size: 11px; letter-spacing: 0.04em; color: var(--ink-3); margin-bottom: 2px; }
.t-msg { font-size: 13px; color: var(--ink); line-height: 1.45; }
.t-x { background: none; border: none; color: var(--ink-3); cursor: pointer; padding: 2px; flex-shrink: 0; }
.t-x:hover { color: var(--ink); }

.t-ok { border-left-color: var(--ok); } .t-ok .t-ic { color: var(--ok); }
.t-bad { border-left-color: var(--bad); } .t-bad .t-ic { color: var(--bad); }
.t-amber { border-left-color: var(--amber); } .t-amber .t-ic { color: var(--amber); }
.t-info { border-left-color: var(--live); } .t-info .t-ic { color: var(--live); }

.toast-enter-active, .toast-leave-active { transition: all 0.32s var(--ease-out); }
.toast-enter-from { opacity: 0; transform: translateX(40px); }
.toast-leave-to { opacity: 0; transform: translateX(40px); }
</style>
