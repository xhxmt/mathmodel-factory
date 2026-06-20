<template>
  <transition name="pal">
    <div v-if="visible" class="pal-overlay" @click.self="$emit('close')">
      <div class="pal panel">
        <div class="pal-in">
          <Icon name="command" :size="16" />
          <input
            ref="input" v-model="query" class="pal-input"
            placeholder="搜索项目、执行命令…" spellcheck="false"
            @keydown.down.prevent="move(1)"
            @keydown.up.prevent="move(-1)"
            @keydown.enter.prevent="run()"
            @keydown.esc.prevent="$emit('close')"
          />
          <kbd class="pal-esc mono">ESC</kbd>
        </div>
        <div ref="list" class="pal-list">
          <div v-if="!items.length" class="pal-empty">无匹配</div>
          <button
            v-for="(it, i) in items" :key="it.id"
            class="pal-item" :class="{ active: i === active }"
            @click="exec(it)" @mousemove="active = i"
          >
            <Icon :name="it.icon" :size="15" class="pi-ic" />
            <span class="pi-label">{{ it.label }}</span>
            <span v-if="it.sub" class="pi-sub mono">{{ it.sub }}</span>
            <span class="pi-kind mono">{{ it.kindLabel }}</span>
          </button>
        </div>
      </div>
    </div>
  </transition>
</template>

<script>
import Icon from './Icon.vue'

const STATUS_LABEL = {
  running: '运行中', paused: '已暂停', completed: '已完成', awaiting_consultation: '等待咨询',
  ready: '就绪', setup: '初始化', failed: '失败', killed: '已终止',
}

export default {
  name: 'CommandPalette',
  components: { Icon },
  props: {
    visible: { type: Boolean, default: false },
    projects: { type: Array, default: () => [] },
  },
  emits: ['close', 'open-project', 'new-project', 'toggle-theme'],
  data() { return { query: '', active: 0 } },
  computed: {
    commands() {
      return [
        { id: 'cmd-new', icon: 'plus', label: '新建建模项目', kind: 'cmd', kindLabel: '命令', run: () => this.$emit('new-project') },
        { id: 'cmd-theme', icon: 'sun', label: '切换明 / 暗主题', kind: 'cmd', kindLabel: '命令', run: () => this.$emit('toggle-theme') },
      ]
    },
    projectItems() {
      return this.projects.map((p) => ({
        id: 'p-' + p.base_name,
        icon: p.consultation_pending ? 'alert-triangle' : 'cpu',
        label: p.base_name,
        sub: STATUS_LABEL[p.status] || p.status,
        kind: 'project', kindLabel: '项目',
        run: () => this.$emit('open-project', p.base_name),
      }))
    },
    items() {
      const q = this.query.trim().toLowerCase()
      const all = [...this.commands, ...this.projectItems]
      if (!q) return all
      return all.filter((it) => it.label.toLowerCase().includes(q) || (it.sub || '').toLowerCase().includes(q))
    },
  },
  watch: {
    visible(v) { if (v) { this.query = ''; this.active = 0; this.$nextTick(() => this.$refs.input?.focus()) } },
    query() { this.active = 0 },
  },
  methods: {
    move(d) {
      if (!this.items.length) return
      this.active = (this.active + d + this.items.length) % this.items.length
      this.$nextTick(() => {
        const el = this.$refs.list?.children[this.active]
        el?.scrollIntoView({ block: 'nearest' })
      })
    },
    run() { const it = this.items[this.active]; if (it) this.exec(it) },
    exec(it) { it.run(); this.$emit('close') },
  },
}
</script>

<style scoped>
.pal-overlay { position: fixed; inset: 0; z-index: 500; background: rgba(0, 0, 0, 0.45); backdrop-filter: blur(3px); display: flex; align-items: flex-start; justify-content: center; padding-top: 14vh; }
.pal { width: 100%; max-width: 560px; overflow: hidden; box-shadow: var(--shadow-lg); }
.pal-in { display: flex; align-items: center; gap: 11px; padding: 15px 16px; border-bottom: 1px solid var(--line); color: var(--ink-3); }
.pal-input { flex: 1; background: none; border: none; outline: none; color: var(--ink); font: 15px var(--sans); }
.pal-esc { font-size: 10px; padding: 2px 6px; border: 1px solid var(--line-2); border-radius: var(--r-xs); color: var(--ink-3); }

.pal-list { max-height: 52vh; overflow-y: auto; padding: 6px; }
.pal-empty { padding: 28px; text-align: center; color: var(--ink-3); font-size: 13px; }
.pal-item { width: 100%; display: flex; align-items: center; gap: 11px; padding: 10px 11px; background: none; border: none; border-radius: var(--r); cursor: pointer; color: var(--ink-2); text-align: left; }
.pal-item.active { background: var(--panel-2); color: var(--ink); }
.pal-item.active .pi-ic { color: var(--amber); }
.pi-ic { color: var(--ink-3); flex-shrink: 0; }
.pi-label { flex: 1; font-size: 13.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pi-sub { font-size: 11px; color: var(--ink-3); }
.pi-kind { font-size: 9.5px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--ink-3); padding: 2px 6px; border: 1px solid var(--line); border-radius: var(--r-xs); }

.pal-enter-active, .pal-leave-active { transition: opacity 0.2s var(--ease); }
.pal-enter-active .pal, .pal-leave-active .pal { transition: transform 0.24s var(--ease-out), opacity 0.24s var(--ease-out); }
.pal-enter-from, .pal-leave-to { opacity: 0; }
.pal-enter-from .pal, .pal-leave-to .pal { transform: translateY(-12px) scale(0.98); opacity: 0; }
</style>
