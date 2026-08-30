<template>
  <section
    class="phase6-snapshot panel"
    aria-labelledby="phase6-snapshot-title"
    :aria-busy="loading ? 'true' : 'false'"
  >
    <header class="phase6-head">
      <div>
        <span class="phase6-eyebrow mono">VERIFIED FULL SHADOW</span>
        <h2 id="phase6-snapshot-title">项目快照</h2>
      </div>
      <button
        type="button"
        class="btn btn-sm btn-ghost"
        :disabled="loading"
        aria-label="刷新 Phase 6 项目快照"
        @click="reload"
      >
        {{ loading ? '读取中…' : '刷新' }}
      </button>
    </header>

    <div
      v-if="viewModel.state !== 'ready'"
      ref="statusRegion"
      class="phase6-status"
      :class="`state-${viewModel.state}`"
      :role="errorState ? 'alert' : 'status'"
      :aria-live="errorState ? 'assertive' : 'polite'"
      tabindex="-1"
    >
      <strong>{{ stateLabel }}</strong>
      <button
        v-if="retryable"
        type="button"
        class="btn btn-sm btn-ghost"
        @click="reload"
      >
        重试读取
      </button>
    </div>

    <template v-else>
      <dl class="phase6-coordinate mono" aria-label="快照坐标">
        <div><dt>snapshot</dt><dd>{{ viewModel.snapshot_id }}</dd></div>
        <div><dt>revision</dt><dd>{{ viewModel.revision }}</dd></div>
      </dl>

      <div v-if="viewModel.sections.length" class="phase6-sections">
        <article
          v-for="(section, sectionIndex) in viewModel.sections"
          :key="section.key"
          class="phase6-section"
          :aria-labelledby="`phase6-section-${sectionIndex}`"
        >
          <h3 :id="`phase6-section-${sectionIndex}`">{{ section.key }}</h3>
          <pre>{{ pretty(section.data) }}</pre>
        </article>
      </div>
      <div v-else class="phase6-status" role="status">当前快照没有可展示区段</div>

      <aside class="phase6-actions" aria-labelledby="phase6-actions-title">
        <h3 id="phase6-actions-title">快照行动中心</h3>
        <p v-if="viewModel.actionCenter.clear" role="status">当前快照没有待处理事项</p>
        <ul v-else role="toolbar" aria-label="快照行动" aria-orientation="vertical">
          <li
            v-for="(action, actionIndex) in viewModel.actionCenter.actions"
            :key="action.id"
            role="none"
          >
            <button
              :ref="(element) => setActionButton(element, actionIndex)"
              type="button"
              :data-action-id="action.id"
              :disabled="action.disabled === true"
              :aria-disabled="action.disabled === true ? 'true' : 'false'"
              :tabindex="action.disabled !== true && actionIndex === activeActionIndex ? 0 : -1"
              :aria-label="actionLabel(action)"
              @click="activateAction(action)"
              @focus="activeActionIndex = actionIndex"
              @keydown="moveActionFocus($event, actionIndex)"
              @keydown.enter.prevent="activateAction(action)"
              @keydown.space.prevent="activateAction(action)"
            >
              <strong>{{ action.title || action.id }}</strong>
              <span v-if="action.summary">{{ action.summary }}</span>
            </button>
          </li>
        </ul>
      </aside>
    </template>
  </section>
</template>

<script>
import { computed, nextTick, ref, watch } from 'vue'

import { usePhase6ProjectSnapshot } from '../composables/usePhase6ProjectSnapshot.js'
import { nextPhase6ActionIndex } from '../lib/phase6RovingFocus.js'

const STATE_LABELS = Object.freeze({
  loading: '正在读取项目快照',
  empty: '尚无项目快照',
  legacy_unavailable: '此项目暂不提供快照',
  auth_error: '无权读取项目快照',
  api_error: '项目快照服务暂不可用',
  unknown: '项目快照校验失败',
})
const REASON_LABELS = Object.freeze({
  PHASE6_SNAPSHOT_INCONSISTENT: '快照完整性校验失败',
  PHASE6_SNAPSHOT_TAMPERED: '快照完整性校验失败',
})

export default {
  name: 'Phase6ProjectSnapshotPanel',
  props: {
    baseName: { type: String, required: true },
    deadlineMs: { type: Number, default: undefined },
  },
  emits: ['navigate'],
  setup(props, { emit }) {
    const statusRegion = ref(null)
    const actionButtons = ref([])
    const activeActionIndex = ref(0)
    const { viewModel, loading, load } = usePhase6ProjectSnapshot({
      deadlineMs: props.deadlineMs,
    })
    const errorState = computed(() => ['auth_error', 'api_error', 'unknown'].includes(viewModel.value.state))
    const retryable = computed(() => ['api_error', 'unknown'].includes(viewModel.value.state))
    const stateLabel = computed(() => (
      REASON_LABELS[viewModel.value.reason_code]
      || STATE_LABELS[viewModel.value.state]
      || '项目快照状态未知'
    ))

    function reload() {
      return load(props.baseName)
    }
    function pretty(value) {
      try { return JSON.stringify(value, null, 2) }
      catch { return '无法展示此区段' }
    }
    function actionLabel(action) {
      return `${action.title || action.id}${action.summary ? `：${action.summary}` : ''}`
    }
    function activateAction(action) {
      if (viewModel.value.actionCenter.interactive && action.disabled !== true) {
        emit('navigate', action)
      }
    }
    function setActionButton(element, index) {
      if (element) actionButtons.value[index] = element
      else delete actionButtons.value[index]
    }
    async function moveActionFocus(event, index) {
      const nextIndex = nextPhase6ActionIndex(
        event.key,
        index,
        viewModel.value.actionCenter.actions,
      )
      if (nextIndex === null) return
      event.preventDefault()
      activeActionIndex.value = nextIndex
      await nextTick()
      actionButtons.value[nextIndex]?.focus()
    }

    watch(() => props.baseName, () => load(props.baseName, { retainCoordinate: false }), { immediate: true })
    watch(() => viewModel.value.state, async (state) => {
      if (!['auth_error', 'api_error', 'unknown'].includes(state)) return
      await nextTick()
      statusRegion.value?.focus()
    })
    watch(() => viewModel.value.actionCenter.actions, (actions) => {
      actionButtons.value.length = actions.length
      activeActionIndex.value = nextPhase6ActionIndex('Home', 0, actions) ?? 0
    })

    return {
      viewModel,
      loading,
      statusRegion,
      activeActionIndex,
      stateLabel,
      errorState,
      retryable,
      reload,
      pretty,
      actionLabel,
      activateAction,
      setActionButton,
      moveActionFocus,
    }
  },
}
</script>

<style scoped>
.phase6-snapshot { padding: 18px; }
.phase6-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.phase6-head h2 { margin: 3px 0 0; font-size: 20px; }
.phase6-eyebrow { color: var(--ink-3); font-size: 9px; letter-spacing: .12em; }
.phase6-status { display: flex; flex-direction: column; align-items: flex-start; gap: 8px; margin-top: 18px; padding: 18px; border: 1px solid var(--line); border-radius: var(--r-sm); }
.phase6-status[role="alert"] { border-color: var(--bad); background: var(--bad-dim); }
.phase6-coordinate { display: grid; gap: 5px; margin: 16px 0; font-size: 10px; }
.phase6-coordinate div { display: grid; grid-template-columns: 80px minmax(0, 1fr); gap: 8px; }
.phase6-coordinate dt { color: var(--ink-3); }
.phase6-coordinate dd { overflow-wrap: anywhere; margin: 0; }
.phase6-sections { display: grid; gap: 12px; }
.phase6-section { padding: 14px; border: 1px solid var(--line); border-radius: var(--r-sm); }
.phase6-section h3, .phase6-actions h3 { margin: 0 0 9px; font-size: 12px; }
.phase6-section pre { overflow: auto; margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--ink-2); font: 10px/1.55 var(--mono); }
.phase6-actions { margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--line); }
.phase6-actions p { color: var(--ok); }
.phase6-actions ul { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }
.phase6-actions button { width: 100%; display: flex; flex-direction: column; gap: 3px; padding: 10px; border: 1px solid var(--line); border-radius: var(--r-sm); background: var(--panel); color: var(--ink); text-align: left; cursor: pointer; }
.phase6-actions button:hover, .phase6-actions button:focus-visible { border-color: var(--amber); outline: none; }
.phase6-actions button span { color: var(--ink-3); font-size: 10px; }
</style>
