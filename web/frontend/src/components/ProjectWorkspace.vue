<template>
  <div class="ws">
    <!-- header -->
    <header class="ws-head">
      <div class="wh-left">
        <button class="btn btn-icon btn-ghost" @click="$emit('close')" title="返回 (Esc)">
          <Icon name="arrow-left" :size="16" />
        </button>
        <div class="wh-id">
          <div class="wh-name mono">{{ project.base_name }}</div>
          <div class="wh-sub">
            <span class="tag" :class="'st-' + project.status">
              <span class="dot" :class="dotClass"></span>{{ statusLabel }}
            </span>
            <span class="wh-step mono">{{ stepLabel }}</span>
            <span class="wh-time mono" v-if="project.last_updated">· {{ rel(project.last_updated) }}</span>
            <span class="wh-pid mono" v-if="project.pid">· PID {{ project.pid }}</span>
          </div>
        </div>
      </div>

      <div class="wh-right">
        <button
          class="cloud-switch"
          :class="{ on: cloudEnabled, busy: cloudSaving }"
          role="switch"
          :aria-checked="cloudEnabled ? 'true' : 'false'"
          :disabled="cloudSaving || cloudConfigLoading"
          :title="cloudSwitchTitle"
          @click="toggleCloudAcceleration"
        >
          <span class="switch-track"><span class="switch-thumb"></span></span>
          <Icon name="zap" :size="14" />
          <span class="hide-xs">{{ cloudSwitchLabel }}</span>
        </button>
        <button class="btn btn-sm btn-ghost" @click="refresh" title="刷新">
          <Icon name="refresh" :size="14" :class="{ spin: loading }" />
        </button>
        <button v-if="isAdmin" class="btn btn-sm btn-ghost" @click="showModels = true" title="模型管理">
          <Icon name="cpu" :size="14" /> <span class="hide-xs">模型</span>
        </button>
        <button v-if="project.is_running" class="btn btn-sm btn-ghost" @click="act('pause')">
          <Icon name="pause" :size="14" /> 暂停
        </button>
        <button v-else-if="canResume" class="btn btn-sm btn-amber" @click="act('resume')">
          <Icon name="play" :size="14" /> 恢复
        </button>
        <template v-if="project.is_running || project.status === 'paused'">
          <button v-if="!killArm" class="btn btn-sm btn-danger" @click="killArm = true">
            <Icon name="stop" :size="14" /> 终止
          </button>
          <button v-else class="btn btn-sm btn-danger" @click="act('kill')" @blur="killArm = false">
            确认终止？
          </button>
        </template>
      </div>
    </header>

    <nav class="ws-tabs" aria-label="项目工作区视图">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="ws-tab"
        :class="{ on: activeTab === tab.key, attention: tab.attention }"
        @click="activeTab = tab.key"
      >
        <Icon :name="tab.icon" :size="13" />
        <span>{{ tab.label }}</span>
      </button>
    </nav>

    <div class="ws-scroll">
      <div v-if="activeTab === 'overview'" class="overview rise">
        <div class="overview-grid">
          <button class="ov-card panel" @click="activeTab = 'pipeline'">
            <span class="ov-l label">当前阶段</span>
            <span class="ov-v mono">{{ stepLabel }}</span>
          </button>
          <button class="ov-card panel" @click="activeTab = project.consultation_pending ? 'consultation' : 'diagnostics'">
            <span class="ov-l label">人工/诊断</span>
            <span class="ov-v mono">{{ project.consultation_pending ? '等待你处理' : diagnostics?.status?.reason_code || '无阻塞' }}</span>
          </button>
          <button class="ov-card panel" @click="activeTab = 'logs'">
            <span class="ov-l label">日志</span>
            <span class="ov-v mono">实时跟随</span>
          </button>
          <button class="ov-card panel" @click="activeTab = 'cloud'">
            <span class="ov-l label">云端</span>
            <span class="ov-v mono">{{ cloudEnabled ? '已启用' : '未启用' }}</span>
          </button>
        </div>
        <DiagnosticsCard
          v-if="diagnostics && diagnostics.status && diagnostics.status.reason_code"
          class="rise"
          :diagnostics="diagnostics"
          @action="onDiagnosticsAction"
        />
        <ConsultationPanel
          v-if="project.consultation_pending"
          class="rise"
          :base="project.base_name"
          :gate="project.consultation_gate || ''"
          @open-file="requestFile"
          @answered="onAnswered"
        />
        <ModelingDirectionPanel
          v-if="project.current_step <= 1"
          class="rise"
          :base="project.base_name"
          :current-step="project.current_step"
          @changed="onModelingDirectionChanged"
        />
      </div>

      <PipelineTimeline
        v-else-if="activeTab === 'pipeline'"
        class="rise"
        :current-step="project.current_step"
        :steps-data="stepsData"
        :awaiting="project.consultation_pending"
        :registry="modelRegistry"
        :assignments="projectAssignments"
        @open-file="requestFile"
        @open-paper="requestPaper"
        @assign="onAssign"
        @manage-models="isAdmin ? (showModels = true) : null"
      />

      <LogConsole
        v-else-if="activeTab === 'logs'"
        class="ws-logs tab-panel"
        :base="project.base_name"
        :active="activeTab === 'logs'"
      />

      <ArtifactBrowser
        v-else-if="activeTab === 'artifacts'"
        class="ws-art panel tab-panel"
        :base="project.base_name"
        :requested="artifactRequest"
        :current-step="project.current_step"
      />

      <div v-else-if="activeTab === 'diagnostics'" class="tab-stack">
        <DiagnosticsCard
          v-if="diagnostics && diagnostics.status && diagnostics.status.reason_code"
          class="rise"
          :diagnostics="diagnostics"
          @action="onDiagnosticsAction"
        />
        <div v-else class="empty-panel panel">
          <Icon name="check-circle" :size="28" />
          <span>当前没有阻塞诊断</span>
        </div>
      </div>

      <ConsultationPanel
        v-else-if="activeTab === 'consultation'"
        class="rise"
        :base="project.base_name"
        :gate="project.consultation_gate || ''"
        @open-file="requestFile"
        @answered="onAnswered"
      />

      <CloudTaskPanel
        v-else-if="activeTab === 'cloud'"
        class="rise"
        :base="project.base_name"
        :project-config="cloudConfig"
        @changed="onCloudPanelChanged"
      />

      <div v-else class="empty-panel panel">
        <Icon name="folder" :size="28" />
        <span>选择一个工作区视图</span>
      </div>
    </div>

    <ModelManager v-if="showModels && isAdmin" @close="showModels = false" />
    <CloudAcceleratorDialog
      v-if="showCloudDialog"
      :base="project.base_name"
      :step="project.current_step || 0"
      :estimated-local="cloudEstimate.local"
      :estimated-cloud="cloudEstimate.cloud"
      @close="showCloudDialog = false"
      @enabled="onCloudEnabled"
    />
  </div>
</template>

<script>
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Icon from './Icon.vue'
import ModelingDirectionPanel from './ModelingDirectionPanel.vue'
import { relativeTime } from '../lib/api.js'
import { stepByIndex, stepConfigKey } from '../lib/steps.js'
import { workspaceTabs } from '../lib/workspaceUi.js'
import { useToasts } from '../composables/useToasts.js'
import { useModels } from '../composables/useModels.js'
import { statusLabel as mapStatusLabel } from '../lib/status.js'
import { useProjectCloudConfig } from '../composables/useProjectCloudConfig.js'
import { useProjectDiagnostics } from '../composables/useProjectDiagnostics.js'
import { useProjectPolling } from '../composables/useProjectPolling.js'
import { useProjectSteps } from '../composables/useProjectSteps.js'
import { useRealtime } from '../composables/useRealtime.js'

// Heavy sub-views are lazy so each tab's code (and KaTeX, via markdown.js used by
// PipelineTimeline/ArtifactBrowser/ConsultationPanel) loads on demand.
const TabFallback = { template: '<div class="tab-fallback"><div class="spinner"></div></div>' }
const asyncOpts = { loadingComponent: TabFallback, delay: 120 }
const PipelineTimeline = defineAsyncComponent({ loader: () => import('./PipelineTimeline.vue'), ...asyncOpts })
const LogConsole = defineAsyncComponent({ loader: () => import('./LogConsole.vue'), ...asyncOpts })
const ArtifactBrowser = defineAsyncComponent({ loader: () => import('./ArtifactBrowser.vue'), ...asyncOpts })
const ConsultationPanel = defineAsyncComponent({ loader: () => import('./ConsultationPanel.vue'), ...asyncOpts })
const DiagnosticsCard = defineAsyncComponent({ loader: () => import('./DiagnosticsCard.vue'), ...asyncOpts })
const ModelManager = defineAsyncComponent({ loader: () => import('./ModelManager.vue'), ...asyncOpts })
const CloudAcceleratorDialog = defineAsyncComponent({ loader: () => import('./CloudAcceleratorDialog.vue'), ...asyncOpts })
const CloudTaskPanel = defineAsyncComponent({ loader: () => import('./CloudTaskPanel.vue'), ...asyncOpts })

export default {
  name: 'ProjectWorkspace',
  components: { Icon, ModelingDirectionPanel, PipelineTimeline, LogConsole, ArtifactBrowser, ConsultationPanel, DiagnosticsCard, ModelManager, CloudAcceleratorDialog, CloudTaskPanel },
  props: {
    project: { type: Object, required: true },
    isAdmin: { type: Boolean, default: false },
  },
  emits: ['close', 'action', 'refresh'],
  setup(props, { emit }) {
    const route = useRoute()
    const router = useRouter()
    const toasts = useToasts()
    const { wsConnected } = useRealtime()
    const { models, load: loadModels, saveConfig } = useModels()
    const { stepsData, loading, fetchSteps: fetchProjectSteps, resetSteps, stopSteps } = useProjectSteps()
    const { diagnostics, diagnosticsLoading, fetchDiagnostics: fetchProjectDiagnostics, resetDiagnostics } = useProjectDiagnostics()
    const { startPolling, stopPolling } = useProjectPolling({ intervalMs: 8000, backoffIntervalMs: 30000 })
    const {
      cloudConfig,
      cloudConfigLoading,
      cloudSaving,
      cloudEnabled,
      cloudSwitchLabel,
      cloudSwitchTitle,
      fetchCloudConfig: fetchProjectCloudConfig,
      setCloudAcceleration,
    } = useProjectCloudConfig()

    const artifactRequest = ref(null)
    const activeTab = ref('overview')
    const killArm = ref(false)
    const nonce = ref(0)
    const showModels = ref(false)
    const showCloudDialog = ref(false)
    const cloudEstimate = ref({ local: 8, cloud: 2 })
    const lastStep = ref(null)

    const statusLabel = computed(() => mapStatusLabel(props.project.status))
    const modelRegistry = computed(() => models.value?.registry || [])
    const projectAssignments = computed(() => models.value?.config?.[props.project.base_name] || {})
    const dotClass = computed(() => ({
      running: 'live',
      awaiting_consultation: 'amber',
      completed: 'ok',
      paused: 'paused',
      failed: 'bad',
      killed: 'bad',
    }[props.project.status] || ''))
    const canResume = computed(() => ['paused', 'ready', 'awaiting_consultation'].includes(props.project.status))
    const tabs = computed(() => workspaceTabs({
      consultationPending: props.project.consultation_pending,
      diagnostics: diagnostics.value,
      cloudEnabled: cloudEnabled.value,
    }))
    const stepLabel = computed(() => {
      const current = props.project.current_step
      const gate = stepsData.value?.editorial_gate
      if (current >= 16) return 'STEP 16 / 16 · 已完成'
      if (current === 8 && gate && !gate.ready) return 'STEP 8.5 / 16 · 阅卷入口设计'
      const active = stepByIndex(Math.min(16, current + 1))
      return `STEP ${Math.max(0, current + 1)} / 16 · ${active ? active.name : ''}`
    })

    function fetchSteps() {
      return fetchProjectSteps(props.project.base_name)
    }

    function fetchDiagnostics() {
      return fetchProjectDiagnostics(props.project.base_name)
    }

    function fetchCloudConfig() {
      return fetchProjectCloudConfig(props.project.base_name)
    }

    function refresh() {
      Promise.allSettled([
        fetchSteps(),
        fetchDiagnostics(),
        fetchCloudConfig(),
      ])
      emit('refresh')
    }

    function act(action) {
      killArm.value = false
      emit('action', props.project, action)
    }

    function requestFile(file) {
      artifactRequest.value = { ...file, _n: ++nonce.value }
      activeTab.value = 'artifacts'
    }

    function requestPaper() {
      artifactRequest.value = { __paper: true, _n: ++nonce.value }
      activeTab.value = 'artifacts'
    }

    function onAnswered() {
      emit('refresh')
      fetchSteps()
      fetchDiagnostics()
    }

    function onModelingDirectionChanged() {
      emit('refresh')
      fetchSteps()
      fetchDiagnostics()
    }

    function onEsc(event) {
      if (event.key === 'Escape' && !killArm.value) emit('close')
    }

    async function toggleCloudAcceleration() {
      if (cloudSaving.value || cloudConfigLoading.value) return
      const nextEnabled = !cloudEnabled.value
      try {
        await setCloudAcceleration(props.project.base_name, nextEnabled)
        toasts.success(nextEnabled ? '云端加速已开启' : '云端加速已关闭', props.project.base_name)
      } catch (error) {
        toasts.error(error.response?.data?.detail || '云端加速设置失败')
      }
    }

    function onDiagnosticsAction(actionId) {
      if (actionId === 'refresh_status') {
        fetchDiagnostics()
        fetchSteps()
        emit('refresh')
        return
      }
      if (actionId === 'resume_project') {
        act('resume')
        return
      }
      if (actionId === 'open_runner_log') {
        activeTab.value = 'logs'
        return
      }
      const evidenceMap = {
        open_runner_log: { path: 'logs/runner.log', type: 'text', name: 'runner.log' },
        open_entry_gate: { path: 'entry_gate.md', type: 'markdown', name: 'entry_gate.md' },
        open_reviewer_entry_artifacts: { path: 'reviewer_entry_map.md', type: 'markdown', name: 'reviewer_entry_map.md' },
        open_consultation_request: { path: `consultation/${props.project.consultation_gate || 'dynamic'}_request.md`, type: 'markdown', name: 'consultation request' },
        open_human_review: { path: 'human_review.md', type: 'markdown', name: 'human_review.md' },
        open_failed_artifact: { path: 'logs/runner.log', type: 'text', name: 'runner.log' },
      }
      const request = evidenceMap[actionId]
      if (request) requestFile(request)
    }

    async function onAssign(step, assignment) {
      const steps = JSON.parse(JSON.stringify(projectAssignments.value))
      const key = stepConfigKey(step)
      const primary = (assignment.primary || '').trim()
      const fallback = (assignment.fallback || '').trim()
      if (!primary && !fallback) delete steps[key]
      else {
        const entry = { primary }
        if (fallback) entry.fallback = fallback
        steps[key] = entry
      }
      try {
        await saveConfig(props.project.base_name, steps)
        toasts.success(`步骤 ${step.key === '8_5' ? '8.5' : step.index} 模型已更新`)
      } catch (error) {
        toasts.error(error.response?.data?.detail || '保存模型选择失败')
      }
    }

    let cloudDialogTimer = null
    function checkCloudAccelerator(currentStep) {
      if (lastStep.value !== null && currentStep !== lastStep.value) {
        const computeSteps = [5, 6]
        if (computeSteps.includes(currentStep)) {
          cloudEstimate.value = currentStep === 5 ? { local: 6, cloud: 1.5 } : { local: 8, cloud: 2 }
          if (cloudDialogTimer) clearTimeout(cloudDialogTimer)
          cloudDialogTimer = setTimeout(() => {
            cloudDialogTimer = null
            if (props.project.is_running && !showCloudDialog.value) showCloudDialog.value = true
          }, 5000)
        }
      }
      lastStep.value = currentStep
    }

    function onCloudEnabled() {
      emit('refresh')
      fetchSteps()
      fetchCloudConfig()
    }

    function onCloudPanelChanged() {
      emit('refresh')
      fetchCloudConfig()
    }

    watch(() => props.project.base_name, () => {
      resetSteps()
      resetDiagnostics()
      fetchSteps()
      fetchDiagnostics()
      fetchCloudConfig()
    })
    watch(() => props.project.current_step, (newStep) => {
      fetchSteps()
      fetchDiagnostics()
      checkCloudAccelerator(newStep)
    })
    watch(() => props.project.consultation_pending, (pending) => {
      if (pending) activeTab.value = 'consultation'
      else if (activeTab.value === 'consultation') activeTab.value = 'overview'
    }, { immediate: true })

    // ---- tab deep-linking: keep activeTab and route.query.tab in sync ----
    const VALID_TABS = new Set(['overview', 'pipeline', 'logs', 'artifacts', 'diagnostics', 'consultation', 'cloud'])
    let syncingTab = false
    // URL -> tab. Only act when the URL explicitly carries a valid tab, so an
    // absent ?tab leaves the consultation auto-jump / default 'overview' intact.
    watch(() => route.query.tab, (tab) => {
      if (typeof tab !== 'string' || !VALID_TABS.has(tab)) return
      if (tab === activeTab.value) return
      syncingTab = true
      activeTab.value = tab
      syncingTab = false
    }, { immediate: true })
    // tab -> URL. Drop the key for 'overview' so the canonical URL stays clean.
    watch(activeTab, (tab) => {
      if (syncingTab) return
      const query = { ...route.query }
      if (tab && tab !== 'overview') query.tab = tab
      else delete query.tab
      syncingTab = true
      router.replace({ query }).catch(() => {}).finally(() => { syncingTab = false })
    })

    onMounted(() => {
      fetchSteps()
      fetchDiagnostics()
      fetchCloudConfig()
      loadModels().catch(() => {})
      startPolling(
        () => {
          fetchSteps()
          fetchDiagnostics()
        },
        {
          shouldRun: () => props.project.is_running,
          backoffWhen: () => wsConnected.value,
          onHidden: stopSteps,
          onVisible: () => {
            fetchSteps()
            fetchDiagnostics()
          },
        },
      )
      window.addEventListener('keydown', onEsc)
      lastStep.value = props.project.current_step
    })

    onBeforeUnmount(() => {
      stopPolling()
      if (cloudDialogTimer) clearTimeout(cloudDialogTimer)
      window.removeEventListener('keydown', onEsc)
    })

    return {
      stepsData,
      activeTab,
      tabs,
      loading,
      artifactRequest,
      killArm,
      showModels,
      diagnostics,
      diagnosticsLoading,
      showCloudDialog,
      cloudEstimate,
      cloudConfig,
      cloudConfigLoading,
      cloudSaving,
      cloudEnabled,
      cloudSwitchLabel,
      cloudSwitchTitle,
      statusLabel,
      modelRegistry,
      projectAssignments,
      dotClass,
      canResume,
      stepLabel,
      rel: relativeTime,
      fetchSteps,
      fetchDiagnostics,
      fetchCloudConfig,
      toggleCloudAcceleration,
      refresh,
      act,
      requestFile,
      requestPaper,
      onAnswered,
      onModelingDirectionChanged,
      onDiagnosticsAction,
      onAssign,
      checkCloudAccelerator,
      onCloudEnabled,
      onCloudPanelChanged,
    }
  },
}
</script>

<style scoped>
.ws {
  position: fixed; inset: 0; z-index: 200;
  display: flex; flex-direction: column;
  background: var(--bg);
  background-image:
    linear-gradient(var(--grid) 1px, transparent 1px),
    linear-gradient(90deg, var(--grid) 1px, transparent 1px);
  background-size: 34px 34px;
  animation: wsin 0.32s var(--ease-out);
}
@keyframes wsin { from { opacity: 0; transform: scale(0.99); } to { opacity: 1; transform: scale(1); } }

.ws-head {
  display: flex; align-items: center; justify-content: space-between; gap: 14px;
  padding: 12px 20px; min-height: var(--header-h);
  border-bottom: 1px solid var(--line);
  background: color-mix(in srgb, var(--panel) 80%, transparent);
  backdrop-filter: blur(8px);
  flex-shrink: 0;
}
.wh-left { display: flex; align-items: center; gap: 14px; min-width: 0; }
.wh-id { min-width: 0; }
.wh-name { font-size: 17px; font-weight: 700; letter-spacing: 0.01em; }
.wh-sub { display: flex; align-items: center; gap: 8px; margin-top: 3px; flex-wrap: wrap; }
.wh-step { font-size: 11px; color: var(--ink-2); }
.wh-time, .wh-pid { font-size: 11px; color: var(--ink-3); }
.wh-right { display: flex; align-items: center; gap: 7px; flex-shrink: 0; }
.spin { animation: spin 0.7s linear infinite; }
.cloud-switch {
  min-height: 30px;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 4px 10px 4px 5px;
  border: 1px solid var(--line);
  border-radius: var(--r-sm);
  background: var(--panel-2);
  color: var(--ink-2);
  font: 600 12px/1 var(--sans);
  cursor: pointer;
  white-space: nowrap;
}
.cloud-switch:hover:not(:disabled) { border-color: var(--live-line); color: var(--ink); }
.cloud-switch.on { color: var(--live); border-color: var(--live-line); background: var(--live-dim); }
.cloud-switch:disabled { opacity: 0.65; cursor: wait; }
.switch-track {
  width: 28px;
  height: 16px;
  padding: 2px;
  border-radius: 999px;
  background: var(--line);
  display: inline-flex;
  align-items: center;
  transition: background 0.16s var(--ease);
}
.switch-thumb {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--ink-3);
  transform: translateX(0);
  transition: transform 0.16s var(--ease), background 0.16s var(--ease);
}
.cloud-switch.on .switch-track { background: var(--live-line); }
.cloud-switch.on .switch-thumb { background: var(--live); transform: translateX(12px); }

.ws-tabs {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 8px 20px;
  border-bottom: 1px solid var(--line);
  background: var(--panel);
  overflow-x: auto;
  flex-shrink: 0;
}
.ws-tab {
  min-height: 32px;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 7px 11px;
  border: 1px solid transparent;
  border-radius: var(--r-sm);
  background: transparent;
  color: var(--ink-3);
  font: 700 12px/1 var(--sans);
  cursor: pointer;
  white-space: nowrap;
}
.ws-tab:hover { color: var(--ink); background: var(--panel-2); }
.ws-tab.on { color: var(--ink); background: var(--panel-3); border-color: var(--line-2); }
.ws-tab.attention { color: var(--amber); }
.ws-tab.attention.on { border-color: var(--amber-line); background: var(--amber-dim); }

.ws-scroll { flex: 1; overflow-y: auto; padding: 18px 20px 32px; display: flex; flex-direction: column; gap: 16px; }

.ws-split { display: grid; grid-template-columns: 0.92fr 1.25fr; gap: 16px; min-height: 520px; }
.ws-logs { height: 62vh; min-height: 420px; }
.ws-art { height: 62vh; min-height: 420px; overflow: hidden; }
.tab-panel { height: calc(100vh - 170px); min-height: 520px; }
.tab-stack, .overview { display: flex; flex-direction: column; gap: 16px; }
.overview-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.ov-card {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
  padding: 14px;
  border-color: var(--line);
  color: var(--ink);
  cursor: pointer;
  text-align: left;
}
.ov-card:hover { background: var(--panel-2); border-color: var(--line-2); }
.ov-l { color: var(--ink-3); }
.ov-v { color: var(--ink); font-size: 13px; line-height: 1.45; }
.empty-panel {
  min-height: 240px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: var(--ink-3);
}

.tag { display: inline-flex; align-items: center; gap: 6px; font: 600 11px/1 var(--mono); letter-spacing: 0.05em; text-transform: uppercase; padding: 5px 9px; border-radius: var(--r-sm); border: 1px solid var(--line); background: var(--panel-2); color: var(--ink-2); }
.st-running { color: var(--live); border-color: var(--live-dim); background: var(--live-dim); }
.st-awaiting_consultation { color: var(--amber); border-color: var(--amber-line); background: var(--amber-dim); }
.st-completed { color: var(--ok); border-color: var(--ok-dim); background: var(--ok-dim); }
.st-paused { color: var(--paused); }
.st-failed, .st-killed { color: var(--bad); border-color: var(--bad-dim); background: var(--bad-dim); }

@media (max-width: 1080px) {
  .ws-split { grid-template-columns: 1fr; }
  .ws-logs, .ws-art { height: auto; min-height: 380px; }
  .overview-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 640px) {
  .wh-right .btn span { display: none; }
  .cloud-switch { padding-right: 5px; }
  .overview-grid { grid-template-columns: 1fr; }
  .tab-panel { height: auto; min-height: 420px; }
}
</style>
