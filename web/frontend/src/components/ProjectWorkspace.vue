<template>
  <div class="ws">
    <!-- header -->
    <header class="ws-head">
      <div class="wh-left">
        <button class="btn btn-icon btn-ghost" @click="$emit('close')" title="返回 (Esc)">
          <Icon name="arrow-left" :size="16" />
        </button>
        <div class="wh-id">
          <div class="wh-name">{{ project.problem_title || project.base_name }}<small class="run-name mono">{{ project.base_name }}</small></div>
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
        <button v-if="primaryAction" class="btn btn-sm" :class="primaryAction.kind === 'navigate' ? 'btn-amber' : 'btn-ghost'" @click="performPrimary">
          <Icon :name="primaryAction.icon" :size="14" /> {{ primaryAction.label }}
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

    <ActionCenter
      v-if="!optionalWorkspaceExtensionEnabled || activeTab !== optionalWorkspaceExtensionKey"
      :actions="workspaceActions"
      @navigate="onWorkspaceAction"
    />

    <div class="ws-scroll">
      <div v-if="activeTab === 'overview'" class="overview rise">
        <StageProgressPanel :project="project" />
        <AuditStatusPanel :project="project" @navigate="activeTab = $event" />
        <div class="overview-grid">
          <button class="ov-card panel" @click="activeTab = 'pipeline'">
            <span class="ov-l label">当前阶段</span>
            <span class="ov-v mono">{{ contestPhaseLabel }}</span>
          </button>
          <button class="ov-card panel" @click="activeTab = 'pipeline'">
            <span class="ov-l label">比赛时钟</span>
            <span class="ov-v mono">{{ contestClockLabel }}</span>
          </button>
          <button class="ov-card panel" @click="activeTab = project.selection_pending ? 'selection' : project.consultation_pending ? 'consultation' : 'diagnostics'">
            <span class="ov-l label">人工/诊断</span>
            <span class="ov-v">{{ project.selection_pending ? '等待选方案' : project.consultation_pending ? '等待你处理' : project.workflow_error ? '需核对执行诊断' : project.evidence_validity === 'INVALID' ? '需重新核验证据' : diagnostics?.status?.reason_code ? '查看诊断事项' : '无待办事项' }}</span>
          </button>
          <button class="ov-card panel" @click="activeTab = 'logs'">
            <span class="ov-l label">日志</span>
            <span class="ov-v mono">实时跟随</span>
          </button>
          <button class="ov-card panel" @click="activeTab = 'cloud'">
            <span class="ov-l label">云端</span>
            <span class="ov-v mono">{{ cloudEnabled ? '已启用' : '未启用' }}</span>
          </button>
          <button v-if="project.active_stage" class="ov-card panel" @click="activeTab = 'diagnostics'">
            <span class="ov-l label">调度位置</span>
            <span class="ov-v mono">{{ schedulerLabel }}</span>
          </button>
        </div>
        <JointModelingPanel :base="project.base_name" :revision="project.revision" @changed="refresh" />
        <ContestTimingPanel :timing="contestDashboard?.timing || {}" />
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
          :revision="project.revision"
          @open-file="requestFile"
          @answered="onAnswered"
        />
        <SelectionPanel
          v-if="project.selection_pending"
          class="rise"
          :base="project.base_name"
          :revision="project.revision"
          @open-file="requestFile"
          @changed="onSelectionChanged"
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
        :project="project"
        :steps-data="stepsData"
        :awaiting="project.consultation_pending || project.selection_pending"
        :registry="modelRegistry"
        :assignments="projectAssignments"
        @open-file="requestFile"
        @open-paper="requestPaper"
        @assign="onAssign"
        @manage-models="isAdmin ? (showModels = true) : null"
      />

      <ProblemPlanPanel
        v-else-if="activeTab === 'plan'"
        class="tab-panel rise"
        :base="project.base_name"
        @open-file="requestFile"
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
        <AuditStatusPanel v-if="project.evidence_validity === 'INVALID' || project.workflow_error" :project="project" @navigate="activeTab = $event" />
        <DiagnosticsCard
          v-if="diagnostics && diagnostics.status && diagnostics.status.reason_code"
          class="rise"
          :diagnostics="diagnostics"
          @action="onDiagnosticsAction"
        />
        <div v-else-if="project.evidence_validity !== 'INVALID' && !project.workflow_error" class="empty-panel panel">
          <Icon name="check-circle" :size="28" />
          <span>当前没有阻塞诊断</span>
        </div>
      </div>

      <ConsultationPanel
        v-else-if="activeTab === 'consultation'"
        class="rise"
        :base="project.base_name"
        :gate="project.consultation_gate || ''"
        :revision="project.revision"
        @open-file="requestFile"
        @answered="onAnswered"
      />

      <SelectionPanel
        v-else-if="activeTab === 'selection'"
        class="rise"
        :base="project.base_name"
        :revision="project.revision"
        @open-file="requestFile"
        @changed="onSelectionChanged"
      />

      <EvidenceCockpit
        v-else-if="activeTab === 'evidence'"
        class="tab-panel rise"
        :evidence="contestDashboard?.evidence || {}"
        :audits="contestDashboard?.audits || []"
        @open-file="requestFile"
        @navigate="activeTab = $event"
      />

      <DeliveryReadinessPanel
        v-else-if="activeTab === 'delivery'"
        class="tab-panel rise"
        :base="project.base_name"
        :delivery="contestDashboard?.delivery || {}"
        :delivery-allowed="project.delivery_allowed"
        @open-file="requestFile"
      />

      <SolverJobPanel
        v-else-if="activeTab === 'solver'"
        class="tab-panel rise"
        :base="project.base_name"
        @changed="refresh"
      />

      <CloudTaskPanel
        v-else-if="activeTab === 'cloud'"
        class="rise"
        :base="project.base_name"
        :project-config="cloudConfig"
        @changed="onCloudPanelChanged"
      />

      <OptionalWorkspaceExtensionPanel
        v-else-if="optionalWorkspaceExtensionEnabled && activeTab === optionalWorkspaceExtensionKey"
        class="tab-panel rise"
        :base-name="project.base_name"
        @navigate="onWorkspaceAction"
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
import { computed, defineAsyncComponent, h, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Icon from './Icon.vue'
import ActionCenter from './ActionCenter.vue'
import ContestTimingPanel from './ContestTimingPanel.vue'
import ModelingDirectionPanel from './ModelingDirectionPanel.vue'
import SelectionPanel from './SelectionPanel.vue'
import { relativeTime } from '../lib/api.js'
import { stepByIndex, stepConfigKey } from '../lib/steps.js'
import { buildWorkspaceActions, workspaceTabs } from '../lib/workspaceUi.js'
import { useToasts } from '../composables/useToasts.js'
import { useModels } from '../composables/useModels.js'
import AuditStatusPanel from './AuditStatusPanel.vue'
import StageProgressPanel from './StageProgressPanel.vue'
import { primaryControl, executionLabel, statusTone, stepText } from '../lib/projectState.js'
import { statusLabel as mapStatusLabel } from '../lib/status.js'
import { useProjectCloudConfig } from '../composables/useProjectCloudConfig.js'
import { useProjectDiagnostics } from '../composables/useProjectDiagnostics.js'
import { useProjectPolling } from '../composables/useProjectPolling.js'
import { useProjectSteps } from '../composables/useProjectSteps.js'
import { useContestDashboard } from '../composables/useContestDashboard.js'
import { useRealtime } from '../composables/useRealtime.js'
import {
  optionalWorkspaceExtensionEnabled,
  optionalWorkspaceExtensionKey,
  optionalWorkspaceExtensionLoader,
  optionalWorkspaceExtensionTab,
} from 'virtual:optional-workspace-snapshot'

// Heavy sub-views are lazy so each tab's code (and KaTeX, via markdown.js used by
// PipelineTimeline/ArtifactBrowser/ConsultationPanel) loads on demand.
const TabFallback = {
  render: () => h('div', { class: 'tab-fallback' }, [h('div', { class: 'spinner' })]),
}
const asyncOpts = { loadingComponent: TabFallback, delay: 120 }
const PipelineTimeline = defineAsyncComponent({ loader: () => import('./PipelineTimeline.vue'), ...asyncOpts })
const ProblemPlanPanel = defineAsyncComponent({ loader: () => import('./ProblemPlanPanel.vue'), ...asyncOpts })
const LogConsole = defineAsyncComponent({ loader: () => import('./LogConsole.vue'), ...asyncOpts })
const ArtifactBrowser = defineAsyncComponent({ loader: () => import('./ArtifactBrowser.vue'), ...asyncOpts })
const SolverJobPanel = defineAsyncComponent({ loader: () => import('./SolverJobPanel.vue'), ...asyncOpts })
const ConsultationPanel = defineAsyncComponent({ loader: () => import('./ConsultationPanel.vue'), ...asyncOpts })
const JointModelingPanel = defineAsyncComponent({ loader: () => import('./JointModelingPanel.vue'), ...asyncOpts })
const DiagnosticsCard = defineAsyncComponent({ loader: () => import('./DiagnosticsCard.vue'), ...asyncOpts })
const ModelManager = defineAsyncComponent({ loader: () => import('./ModelManager.vue'), ...asyncOpts })
const CloudAcceleratorDialog = defineAsyncComponent({ loader: () => import('./CloudAcceleratorDialog.vue'), ...asyncOpts })
const CloudTaskPanel = defineAsyncComponent({ loader: () => import('./CloudTaskPanel.vue'), ...asyncOpts })
const EvidenceCockpit = defineAsyncComponent({ loader: () => import('./EvidenceCockpit.vue'), ...asyncOpts })
const DeliveryReadinessPanel = defineAsyncComponent({ loader: () => import('./DeliveryReadinessPanel.vue'), ...asyncOpts })
// Vite resolves the virtual module to an entirely inert default-off module or
// to the reviewed optional extension.  The base workspace contains no Phase 6
// path, route, component, or API string, so an accidental unconditional import
// is visible in the production manifest and browser resource tests.
const OptionalWorkspaceExtensionPanel = optionalWorkspaceExtensionLoader
  ? defineAsyncComponent({ loader: optionalWorkspaceExtensionLoader, ...asyncOpts })
  : { render: () => null }

export default {
  name: 'ProjectWorkspace',
  components: { StageProgressPanel, JointModelingPanel, AuditStatusPanel, Icon, ActionCenter, ContestTimingPanel, ModelingDirectionPanel, SelectionPanel, PipelineTimeline, ProblemPlanPanel, LogConsole, ArtifactBrowser, SolverJobPanel, ConsultationPanel, DiagnosticsCard, ModelManager, CloudAcceleratorDialog, CloudTaskPanel, EvidenceCockpit, DeliveryReadinessPanel, OptionalWorkspaceExtensionPanel },
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
    const { contestDashboard, contestDashboardLoading, fetchContestDashboard, resetContestDashboard } = useContestDashboard()
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
    const clockNow = ref(Math.floor(Date.now() / 1000))

    const statusLabel = computed(() => executionLabel(props.project))
    const primaryAction = computed(() => primaryControl(props.project))
    function performPrimary() {
      if (primaryAction.value?.kind === 'command') act(primaryAction.value.action)
      else if (primaryAction.value?.tab) activeTab.value = primaryAction.value.tab
    }
    const modelRegistry = computed(() => models.value?.registry || [])
    const projectAssignments = computed(() => models.value?.config?.[props.project.base_name] || {})
    const dotClass = computed(() => statusTone(props.project))
    const tabs = computed(() => {
      const currentTabs = workspaceTabs({
        consultationPending: props.project.consultation_pending,
        selectionPending: props.project.selection_pending,
        diagnostics: diagnostics.value,
        cloudEnabled: cloudEnabled.value,
      })
      if (optionalWorkspaceExtensionEnabled && optionalWorkspaceExtensionTab) {
        currentTabs.push(optionalWorkspaceExtensionTab)
      }
      return currentTabs
    })
    const workspaceActions = computed(() => buildWorkspaceActions(contestDashboard.value, stepsData.value, props.project))
    const stepLabel = computed(() => stepText(props.project))
    const phaseNames = {
      problem_understanding: '题意与数据',
      model_tournament: '模型竞赛',
      model_and_solve: '建模与求解',
      validation: '结果验证',
      paper_construction: '论文构建',
      deterministic_paper_audit: '确定性论文审计',
      review_and_revision: '审稿与修订',
      final_audit_and_delivery: '最终审计与交付',
    }
    const contestPhaseLabel = computed(() => {
      const phase = props.project.contest_phase
      if (!phase) return stepLabel.value
      return `阶段 ${phase.id} / 8 · ${phaseNames[phase.name] || phase.name}`
    })
    const schedulerLabel = computed(() => {
      if (!props.project.active_stage) return '未激活'
      const name = props.project.active_stage_name || `Stage ${props.project.active_stage}`
      const subtask = props.project.active_subtask || `Step ${props.project.source_step_id ?? props.project.current_step}`
      return `Stage ${props.project.active_stage} / 10 · ${name} · ${subtask}`
    })
    function formatRemaining(seconds) {
      const value = Math.max(0, Number(seconds) || 0)
      const hours = Math.floor(value / 3600)
      const minutes = Math.floor((value % 3600) / 60)
      return `${hours}h ${String(minutes).padStart(2, '0')}m`
    }
    const contestClockLabel = computed(() => {
      const deadline = Number(props.project.contest_deadline_at || 0)
      const contentFreeze = Number(props.project.content_freeze_at || 0)
      const deliveryFreeze = Number(props.project.delivery_freeze_at || 0)
      if (!deadline) return '未配置'
      if (clockNow.value < contentFreeze) return `距内容冻结 ${formatRemaining(contentFreeze - clockNow.value)}`
      if (clockNow.value < deliveryFreeze) return `最终审计期 · 距提交 ${formatRemaining(deadline - clockNow.value)}`
      if (clockNow.value < deadline) return `交付冻结 · 距提交 ${formatRemaining(deadline - clockNow.value)}`
      return '比赛截止时间已到'
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

    function fetchDashboard() {
      return fetchContestDashboard(props.project.base_name)
    }

    function refresh() {
      Promise.allSettled([
        fetchSteps(),
        fetchDiagnostics(),
        fetchCloudConfig(),
        fetchDashboard(),
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

    function onSelectionChanged() {
      emit('refresh')
      fetchSteps()
      fetchDiagnostics()
      fetchDashboard()
      activeTab.value = 'overview'
    }

    function onWorkspaceAction(action) {
      if (action?.file) {
        requestFile({ path: action.file, name: action.file.split('/').pop(), type: 'markdown' })
        return
      }
      if (action?.tab && tabs.value.some((tab) => tab.key === action.tab)) activeTab.value = action.tab
      else activeTab.value = 'overview'
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
      if (actionId === 'open_audit_timeline') {
        activeTab.value = 'diagnostics'
        return
      }
      if (actionId === 'retry_human_decision_commit') {
        activeTab.value = props.project.consultation_pending ? 'consultation' : 'selection'
        return
      }
      if (actionId === 'open_gate_evidence') {
        const evidence = diagnostics.value?.status?.evidence?.[0]
        if (evidence?.path) {
          requestFile({ path: evidence.path, type: 'text', name: evidence.path })
        }
        return
      }
      const evidenceMap = {
        open_runner_log: { path: 'logs/runner.log', type: 'text', name: 'runner.log' },
        open_entry_gate: { path: 'entry_gate.md', type: 'markdown', name: 'entry_gate.md' },
        open_reviewer_entry_artifacts: { path: 'reviewer_entry_map.md', type: 'markdown', name: 'reviewer_entry_map.md' },
        open_consultation_request: { path: `consultation/${props.project.consultation_gate || 'dynamic'}_request.md`, type: 'markdown', name: 'consultation request' },
        open_human_review: { path: 'human_review.md', type: 'markdown', name: 'human_review.md' },
        open_selection_request: { path: `selection/${props.project.selection_gate || 'step3'}_request.md`, type: 'markdown', name: 'selection request' },
        open_selection_evidence: { path: `selection/${props.project.selection_gate || 'step3'}_options.json`, type: 'json', name: 'selection options' },
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
      resetContestDashboard()
      fetchSteps()
      fetchDiagnostics()
      fetchCloudConfig()
      fetchDashboard()
    })
    watch(() => props.project.current_step, (newStep) => {
      fetchSteps()
      fetchDiagnostics()
      fetchDashboard()
      checkCloudAccelerator(newStep)
    })
    watch(() => props.project.consultation_pending, (pending) => {
      if (pending) activeTab.value = 'consultation'
      else if (activeTab.value === 'consultation') activeTab.value = 'overview'
    }, { immediate: true })
    watch(() => props.project.selection_pending, (pending) => {
      if (pending) activeTab.value = 'selection'
      else if (activeTab.value === 'selection') activeTab.value = 'overview'
    }, { immediate: true })

    // ---- tab deep-linking: keep activeTab and route.query.tab in sync ----
    const VALID_TABS = new Set(['overview', 'pipeline', 'plan', 'logs', 'artifacts', 'evidence', 'delivery', 'solver', 'diagnostics', 'consultation', 'selection', 'cloud'])
    if (optionalWorkspaceExtensionEnabled && optionalWorkspaceExtensionKey) {
      VALID_TABS.add(optionalWorkspaceExtensionKey)
    }
    let syncingTab = false
    // URL -> tab. Only act when the URL explicitly carries a valid tab, so an
    // absent ?tab leaves the consultation auto-jump / default 'overview' intact.
    watch(() => route.query.tab, (tab) => {
      if (typeof tab !== 'string' || !VALID_TABS.has(tab)) return
      if (!tabs.value.some((available) => available.key === tab)) return
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

    let contestClockTimer = null
    onMounted(() => {
      fetchSteps()
      fetchDiagnostics()
      fetchCloudConfig()
      fetchDashboard()
      loadModels().catch(() => {})
      startPolling(
        () => {
          fetchSteps()
          fetchDiagnostics()
          fetchDashboard()
        },
        {
          shouldRun: () => props.project.is_running,
          backoffWhen: () => wsConnected.value,
          onHidden: stopSteps,
          onVisible: () => {
            fetchSteps()
            fetchDiagnostics()
            fetchDashboard()
          },
        },
      )
      window.addEventListener('keydown', onEsc)
      lastStep.value = props.project.current_step
      contestClockTimer = window.setInterval(() => {
        clockNow.value = Math.floor(Date.now() / 1000)
      }, 30000)
    })

    onBeforeUnmount(() => {
      stopPolling()
      if (cloudDialogTimer) clearTimeout(cloudDialogTimer)
      if (contestClockTimer) clearInterval(contestClockTimer)
      window.removeEventListener('keydown', onEsc)
    })

    return {
      stepsData,
      contestDashboard,
      contestDashboardLoading,
      workspaceActions,
      optionalWorkspaceExtensionEnabled,
      optionalWorkspaceExtensionKey,
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
      primaryAction,
      performPrimary,
      stepLabel,
      contestPhaseLabel,
      schedulerLabel,
      contestClockLabel,
      rel: relativeTime,
      fetchSteps,
      fetchDiagnostics,
      fetchCloudConfig,
      fetchDashboard,
      toggleCloudAcceleration,
      refresh,
      act,
      requestFile,
      requestPaper,
      onAnswered,
      onModelingDirectionChanged,
      onSelectionChanged,
      onWorkspaceAction,
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
  min-width: 0; overflow-x: hidden;
  background: var(--bg);
  background-image:
    linear-gradient(var(--grid) 1px, transparent 1px),
    linear-gradient(90deg, var(--grid) 1px, transparent 1px);
  background-size: 34px 34px;
  animation: wsin 0.32s var(--ease-out);
}
.run-name { display: block; font-size: 11px; color: var(--ink-3); margin-top: 4px; font-weight: 400; }
.st-interrupted { color: var(--bad); background: var(--bad-dim); }
.st-retrying { color: var(--amber); background: var(--amber-dim); }
.st-archiving { color: var(--live); background: var(--live-dim); }
@keyframes wsin { from { opacity: 0; transform: scale(0.99); } to { opacity: 1; transform: scale(1); } }

.ws-head {
  display: flex; align-items: center; justify-content: space-between; gap: 14px;
  padding: 12px 20px; min-height: var(--header-h);
  border-bottom: 1px solid var(--line);
  background: color-mix(in srgb, var(--bg) 62%, transparent);
  -webkit-backdrop-filter: blur(22px) saturate(160%);
  backdrop-filter: blur(22px) saturate(160%);
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
  background: color-mix(in srgb, var(--bg) 55%, transparent);
  -webkit-backdrop-filter: blur(14px);
  backdrop-filter: blur(14px);
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
.ws-tab.on { color: var(--accent-ink); background: var(--grad); border-color: transparent; box-shadow: 0 2px 12px var(--accent-glow); }
.ws-tab.attention { color: var(--amber); }
.ws-tab.attention.on { color: var(--amber); border-color: var(--amber-line); background: var(--amber-dim); box-shadow: none; }

.ws-scroll { flex: 1; min-width: 0; overflow-y: auto; padding: 18px 20px 32px; display: flex; flex-direction: column; gap: 16px; }

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
.st-awaiting_selection { color: var(--amber); border-color: var(--amber-line); background: var(--amber-dim); }
.st-completed { color: var(--ok); border-color: var(--ok-dim); background: var(--ok-dim); }
.st-paused { color: var(--paused); }
.st-failed, .st-killed { color: var(--bad); border-color: var(--bad-dim); background: var(--bad-dim); }

@media (max-width: 1080px) {
  .ws-split { grid-template-columns: 1fr; }
  .ws-logs, .ws-art { height: auto; min-height: 380px; }
  .overview-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 640px) {
  .ws-head { padding: 12px; gap: 12px; flex-direction: column; align-items: stretch; }
  .wh-left { flex: 1; gap: 10px; }
  .wh-id { overflow: hidden; }
  .wh-name { font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .wh-sub { gap: 6px; }
  .wh-right { gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
  .wh-right .btn { min-width: 32px; min-height: 32px; width: auto; height: auto; padding: 6px 9px; font-size: 12px; }
  .cloud-switch { padding-right: 5px; }
  .cloud-switch .hide-xs { display: none; }
  .ws-tabs { padding-inline: 12px; }
  .ws-scroll { padding: 16px 12px 24px; }
  .overview-grid { grid-template-columns: 1fr; }
  .tab-panel { height: auto; min-height: 420px; }
}
</style>
