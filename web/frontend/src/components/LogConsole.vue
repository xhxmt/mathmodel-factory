<template>
  <div class="logc panel">
    <div class="logc-head">
      <div class="lh-left">
        <Icon name="terminal" :size="14" />
        <span class="label">实时日志</span>
        <span v-if="file" class="file mono">{{ file }}</span>
      </div>
      <div class="lh-right">
        <div class="search">
          <Icon name="search" :size="12" />
          <input v-model="query" class="search-in mono" placeholder="过滤…" spellcheck="false" />
          <button v-if="query" class="clr" @click="query = ''"><Icon name="x" :size="11" /></button>
        </div>
        <button class="btn btn-sm btn-ghost" :class="{ on: wrap }" @click="wrap = !wrap" title="自动换行">
          <Icon name="corner-down-left" :size="13" />
        </button>
        <button class="btn btn-sm btn-ghost" @click="copyAll" title="复制全部">
          <Icon name="copy" :size="13" />
        </button>
        <button class="btn btn-sm btn-ghost" @click="fetchNow" title="刷新">
          <Icon name="refresh" :size="13" :class="{ spin: loading }" />
        </button>
        <button class="btn btn-sm" :class="following ? 'btn-amber' : 'btn-ghost'" @click="toggleFollow" title="实时跟随">
          <span class="dot" :class="{ live: following }"></span> {{ following ? '跟随中' : '已暂停' }}
        </button>
      </div>
    </div>

    <div ref="body" class="logc-body mono" :class="{ wrap }" @scroll="onScroll">
      <div v-if="loading && lines.length === 0" class="logc-empty">加载日志…</div>
      <div v-else-if="filtered.length === 0" class="logc-empty">
        {{ query ? '无匹配行' : '暂无日志输出' }}
      </div>
      <template v-else>
        <div
          v-for="(ln, i) in filtered"
          :key="i"
          class="ll"
          :class="'lv-' + level(ln.text)"
        >
          <span class="ln-no">{{ ln.n }}</span>
          <span class="ln-tx">{{ ln.text || ' ' }}</span>
        </div>
      </template>
    </div>

    <button v-if="!atBottom && filtered.length" class="jump" @click="scrollBottom(true)">
      <Icon name="chevron-down" :size="14" /> 跳到底部
    </button>
  </div>
</template>

<script>
import Icon from './Icon.vue'
import { Projects } from '../lib/api.js'
import { useToasts } from '../composables/useToasts.js'

export default {
  name: 'LogConsole',
  components: { Icon },
  props: {
    base: { type: String, required: true },
    active: { type: Boolean, default: true },
  },
  setup() { return { toasts: useToasts() } },
  data() {
    return { lines: [], file: '', loading: false, following: true, query: '', wrap: false, atBottom: true, timer: null }
  },
  computed: {
    filtered() {
      const q = this.query.trim().toLowerCase()
      if (!q) return this.lines
      return this.lines.filter((l) => l.text.toLowerCase().includes(q))
    },
  },
  watch: {
    active(v) { v ? this.start() : this.stop() },
    base() { this.lines = []; this.fetchNow() },
  },
  mounted() { this.fetchNow(); if (this.active) this.start() },
  beforeUnmount() { this.stop() },
  methods: {
    start() { this.stop(); this.timer = setInterval(() => { if (this.following) this.fetchNow() }, 3000) },
    stop() { if (this.timer) { clearInterval(this.timer); this.timer = null } },
    async fetchNow() {
      this.loading = true
      try {
        const d = await Projects.logs(this.base, 400)
        this.file = d.file || ''
        const raw = (d.logs || [])
        // drop a single trailing empty line from tail()
        while (raw.length && raw[raw.length - 1] === '') raw.pop()
        this.lines = raw.map((text, i) => ({ n: i + 1, text }))
        this.$nextTick(() => { if (this.following && this.atBottom) this.scrollBottom() })
      } catch (e) {
        // keep prior lines; surface only on explicit refresh
      } finally {
        this.loading = false
      }
    },
    level(text) {
      if (/\b(error|fail(ed)?|traceback|exception|fatal)\b|❌/i.test(text)) return 'err'
      if (/\b(warn(ing)?)\b|⚠/i.test(text)) return 'warn'
      if (/\b(ok|success|done|completed|pass(ed)?|converged)\b|✓|✅/i.test(text)) return 'ok'
      if (/^\s*[>$#]/.test(text) || /\b(step|info)\b/i.test(text)) return 'info'
      return ''
    },
    onScroll() {
      const el = this.$refs.body
      if (!el) return
      this.atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40
    },
    scrollBottom(force = false) {
      const el = this.$refs.body
      if (!el) return
      el.scrollTop = el.scrollHeight
      this.atBottom = true
      if (force) this.following = true
    },
    toggleFollow() {
      this.following = !this.following
      if (this.following) { this.fetchNow(); this.$nextTick(() => this.scrollBottom()) }
    },
    async copyAll() {
      try {
        await navigator.clipboard.writeText(this.filtered.map((l) => l.text).join('\n'))
        this.toasts.success('日志已复制到剪贴板')
      } catch (e) {
        this.toasts.error('复制失败')
      }
    },
  },
}
</script>

<style scoped>
.logc { display: flex; flex-direction: column; overflow: hidden; min-height: 0; }

.logc-head {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  padding: 11px 14px; border-bottom: 1px solid var(--line);
  background: var(--panel-2); flex-wrap: wrap;
}
.lh-left { display: flex; align-items: center; gap: 8px; color: var(--ink-2); }
.file { font-size: 11px; color: var(--ink-3); }
.lh-right { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }

.search { display: flex; align-items: center; gap: 6px; padding: 5px 9px; background: var(--bg-2); border: 1px solid var(--line-2); border-radius: var(--r-sm); color: var(--ink-3); }
.search-in { background: none; border: none; outline: none; color: var(--ink); font-size: 12px; width: 110px; }
.clr { background: none; border: none; color: var(--ink-3); cursor: pointer; display: flex; padding: 0; }
.clr:hover { color: var(--ink); }
.btn.on { color: var(--amber); border-color: var(--amber-line); }
.spin { animation: spin 0.7s linear infinite; }

.logc-body {
  flex: 1; min-height: 0; overflow: auto;
  padding: 10px 0;
  background: var(--bg);
  font-size: 12.5px; line-height: 1.6;
}
.logc-body.wrap .ln-tx { white-space: pre-wrap; word-break: break-word; }
.logc-empty { padding: 40px; text-align: center; color: var(--ink-3); font-size: 13px; }

.ll {
  display: flex; gap: 12px;
  padding: 0 14px;
  white-space: pre;
}
.ll:hover { background: var(--panel); }
.ln-no { color: var(--ink-3); opacity: 0.5; text-align: right; min-width: 34px; user-select: none; flex-shrink: 0; }
.ln-tx { color: var(--ink-2); }

.lv-err .ln-tx { color: var(--bad); }
.lv-warn .ln-tx { color: var(--amber); }
.lv-ok .ln-tx { color: var(--ok); }
.lv-info .ln-tx { color: var(--ink); }
.lv-err { background: var(--bad-dim); }

.jump {
  position: absolute; bottom: 14px; left: 50%; transform: translateX(-50%);
  display: inline-flex; align-items: center; gap: 6px;
  padding: 7px 13px; border-radius: 100px;
  background: var(--amber); color: var(--amber-ink); border: none;
  font: 600 12px/1 var(--sans); cursor: pointer;
  box-shadow: 0 6px 18px var(--amber-glow);
}
.logc { position: relative; }
</style>
