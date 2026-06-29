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
        <div class="level-tabs">
          <button
            v-for="item in LOG_LEVELS"
            :key="item.key"
            class="lvl-tab"
            :class="{ on: logLevelFilter === item.key }"
            @click="logLevelFilter = item.key"
          >
            {{ item.label }}
          </button>
        </div>
        <button class="btn btn-sm btn-ghost" :disabled="!firstErrorLine" @click="jumpError" title="跳到最近错误">
          <Icon name="alert-triangle" :size="13" />
        </button>
        <button class="btn btn-sm btn-ghost" :disabled="!firstErrorLine" @click="copyErrorContext" title="复制错误上下文">
          <Icon name="pin" :size="13" />
        </button>
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
      <div v-else-if="visibleLines.length === 0" class="logc-empty">
        {{ query || logLevelFilter !== 'all' ? '无匹配行' : '暂无日志输出' }}
      </div>
      <!-- Virtualized path: fixed row height, only the viewport + buffer is rendered.
           Wrap mode (variable row height) falls through to the flat v-for below. -->
      <div
        v-else-if="virtual"
        class="logc-vlist"
        :style="{ height: totalHeight + 'px', paddingTop: topPad + 'px' }"
      >
        <div
          v-for="ln in renderSlice"
          :key="ln.n"
          class="ll"
          :class="'lv-' + level(ln.text)"
        >
          <span class="ln-no">{{ ln.n }}</span>
          <span class="ln-tx">{{ ln.text || ' ' }}</span>
        </div>
      </div>
      <template v-else>
        <div
          v-for="ln in visibleLines"
          :key="ln.n"
          class="ll"
          :class="'lv-' + level(ln.text)"
        >
          <span class="ln-no">{{ ln.n }}</span>
          <span class="ln-tx">{{ ln.text || ' ' }}</span>
        </div>
      </template>
    </div>

    <button v-if="!atBottom && visibleLines.length" class="jump" @click="scrollBottom(true)">
      <Icon name="chevron-down" :size="14" /> 跳到底部
    </button>
  </div>
</template>

<script>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import Icon from './Icon.vue'
import { useToasts } from '../composables/useToasts.js'
import { useProjectLogs } from '../composables/useProjectLogs.js'
import { LOG_LEVELS, buildLogErrorContext, filterLogLines, logLineLevel } from '../lib/workspaceUi.js'

export default {
  name: 'LogConsole',
  components: { Icon },
  props: {
    base: { type: String, required: true },
    active: { type: Boolean, default: true },
  },
  setup(props) {
    const toasts = useToasts()
    const body = ref(null)
    const {
      lines,
      file,
      loading,
      following,
      query,
      wrap,
      atBottom,
      fetchNow: fetchProjectLogs,
      resetLogs,
      startPolling,
      stopPolling,
    } = useProjectLogs()
    const logLevelFilter = ref('all')
    const visibleLines = computed(() => filterLogLines(lines.value, {
      query: query.value,
      level: logLevelFilter.value,
    }))
    const firstErrorLine = computed(() => lines.value.find((line) => logLineLevel(line.text || '') === 'err') || null)

    // ---- virtual scrolling (fixed row height; wrap mode opts out) ----
    const ROW_H = 20      // 12.5px font * 1.6 line-height, .ll has no vertical padding
    const BUFFER = 20     // rows rendered above/below the viewport
    const scrollTop = ref(0)
    const viewportH = ref(0)
    const virtual = computed(() => !wrap.value && visibleLines.value.length > 0)
    const totalHeight = computed(() => visibleLines.value.length * ROW_H)
    const renderStart = computed(() => {
      if (!virtual.value) return 0
      const start = Math.floor(scrollTop.value / ROW_H) - BUFFER
      return Math.max(0, Math.min(start, visibleLines.value.length))
    })
    const renderCount = computed(() => {
      if (!virtual.value) return 0
      const cnt = Math.ceil(viewportH.value / ROW_H) + BUFFER * 2
      return Math.max(0, Math.min(cnt, visibleLines.value.length - renderStart.value))
    })
    const renderSlice = computed(() => visibleLines.value.slice(renderStart.value, renderStart.value + renderCount.value))
    const topPad = computed(() => renderStart.value * ROW_H)

    function fetchNow(silent = false) {
      return fetchProjectLogs(props.base, silent === true).then(() => {
        nextTick(() => {
          if (following.value && atBottom.value) scrollBottom()
        })
      })
    }

    function start() {
      startPolling(() => props.base)
    }

    function stop() {
      stopPolling()
    }

    function onScroll() {
      const el = body.value
      if (!el) return
      scrollTop.value = el.scrollTop
      viewportH.value = el.clientHeight
      atBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 40
    }

    function scrollBottom(force = false) {
      const el = body.value
      if (!el) return
      el.scrollTop = el.scrollHeight
      atBottom.value = true
      if (force) following.value = true
    }

    function toggleFollow() {
      following.value = !following.value
      if (following.value) {
        fetchNow()
        nextTick(() => scrollBottom())
      }
    }

    async function copyAll() {
      try {
        await navigator.clipboard.writeText(visibleLines.value.map((line) => line.text).join('\n'))
        toasts.success('日志已复制到剪贴板')
      } catch (error) {
        toasts.error('复制失败')
      }
    }

    async function copyErrorContext() {
      const context = buildLogErrorContext(lines.value, 3)
      if (!context.length) return
      try {
        await navigator.clipboard.writeText(context.map((line) => line.text).join('\n'))
        toasts.success('错误上下文已复制')
      } catch (error) {
        toasts.error('复制失败')
      }
    }

    function jumpError() {
      if (!firstErrorLine.value) return
      logLevelFilter.value = 'all'
      query.value = ''
      nextTick(() => {
        // Recompute against the now-unfiltered list, then scroll the (possibly
        // unmounted) error row into view via its index — virtualization means it
        // may not be in the DOM until we move scrollTop there.
        const idx = visibleLines.value.findIndex((line) => logLineLevel(line.text || '') === 'err')
        if (idx === -1) return
        const el = body.value
        if (!el) return
        el.scrollTop = Math.max(0, idx * ROW_H - el.clientHeight / 2)
      })
    }

    watch(() => props.active, (active) => { active ? start() : stop() })
    watch(() => props.base, () => {
      stop()
      resetLogs()
      fetchNow()
      if (props.active) start()
    })

    function measureViewport() {
      const el = body.value
      if (el) viewportH.value = el.clientHeight
    }
    function onResize() { measureViewport() }

    onMounted(() => {
      if (props.active) start()
      fetchNow()
      measureViewport()
      window.addEventListener('resize', onResize)
    })
    onBeforeUnmount(() => {
      stop()
      window.removeEventListener('resize', onResize)
    })

    return {
      body,
      lines,
      file,
      loading,
      following,
      query,
      wrap,
      atBottom,
      LOG_LEVELS,
      logLevelFilter,
      visibleLines,
      firstErrorLine,
      virtual,
      totalHeight,
      topPad,
      renderSlice,
      fetchNow,
      level: logLineLevel,
      onScroll,
      scrollBottom,
      toggleFollow,
      copyAll,
      copyErrorContext,
      jumpError,
    }
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
.level-tabs { display: flex; gap: 3px; padding: 3px; border: 1px solid var(--line); border-radius: var(--r-sm); background: var(--bg-2); }
.lvl-tab { border: none; background: transparent; color: var(--ink-3); border-radius: var(--r-xs); padding: 4px 7px; font: 600 11px/1 var(--sans); cursor: pointer; }
.lvl-tab:hover { color: var(--ink); }
.lvl-tab.on { background: var(--panel-3); color: var(--ink); }
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
/* Virtualized rows have a fixed height (ROW_H = 20px); wrap-mode rows stay auto. */
.logc-vlist .ll { height: 20px; }
.logc-vlist { box-sizing: border-box; }
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
