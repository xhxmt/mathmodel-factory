<template>
  <div class="ws">
    <header class="ws-top">
      <div class="ws-top-in">
        <button class="btn btn-icon btn-ghost back" @click="$emit('close')" title="返回项目列表 (Esc)">
          <Icon name="arrow-left" :size="17" />
        </button>
        <div class="ident">
          <h1 class="title">{{ project.problem_title || project.base_name }}</h1>
          <div class="meta">
            <span class="pill" :class="dotClass"><span class="dot" :class="dotClass"></span>{{ statusLabel }}</span>
            <span class="step">{{ stepLabel }}</span>
            <span v-if="project.last_updated" class="muted">更新于 {{ rel(project.last_updated) }}</span>
            <span class="muted mono run">{{ project.base_name }}</span>
          </div>
        </div>
        <div class="controls">
          <button class="btn btn-icon btn-ghost" @click="refresh" title="刷新">
            <Icon name="refresh" :size="16" :class="{ spin: loading }" />
          </button>
          <button v-if="isAdmin" class="btn btn-icon btn-ghost" @click="showModels = true" title="模型分配">
            <Icon name="cpu" :size="16" />
          </button>
          <template v-if="project.is_running || project.status === 'paused'">
            <button v-if="!killArm" class="btn btn-sm btn-danger" @click="killArm = true" title="终止运行">
              <Icon name="stop" :size="14" /><span class="hide-xs">终止</span>
            </button>
            <button v-else class="btn btn-sm btn-danger armed" @click="act('kill')" @blur="killArm = false">确认终止</button>
          </template>
          <button v-if="primaryAction" class="btn btn-sm" :class="primaryAction.kind === 'navigate' ? 'btn-primary' : ''" @click="performPrimary">
            <Icon :name="primaryAction.icon" :size="14" />{{ primaryAction.label }}
          </button>
        </div>
      </div>
      <nav class="tabs" role="tablist" aria-label="项目视图">
        <button
          v-for="tab in tabs"
          :key="tab.key"
          role="tab"
          :aria-selected="activeTab === tab.key ? 'true' : 'false'"
          class="tab"
          :class="{ on: activeTab === tab.key, attention: tab.attention }"
          @click="activeTab = tab.key"
        >
          {{ tab.label }}<span v-if="tab.attention" class="badge"></span>
        </button>
      </nav>
    </header>

    <main class="ws-body">
      <div class="view" :class="{ fill: activeTab === 'artifacts' || activeTab === 'logs' }">
      <!-- 概览 -->
      <div v-if="activeTab === 'overview'" class="overview rise">
        <section class="now panel">
          <div class="now-main">
            <div class="now-label">当前</div>
            <div class="now-step">{{ stepLabel }}</div>
            <p class="now-hint" :class="{ warn: awaiting, bad: failed }">{{ nowHint }}</p>
          </div>
          <div class="now-progress">
            <div class="bar"><span :class="dotClass" :style="{ width: progress + '%' }"></span></div>
            <span class="tnum">{{ progress }}%</span>
          </div>
          <dl class="facts">
            <div><dt>比赛阶段</dt><dd>{{ contestPhaseLabel }}</dd></div>
            <div><dt>比赛时钟</dt><dd>{{ contestClockLabel }}</dd></div>
            <div v-if="project.active_stage"><dt>调度位置</dt><dd>{{ schedulerLabel }}</dd></div>
            <div v-if="project.pid"><dt>进程</dt><dd class="mono">PID {{ project.pid }}</dd></div>
          </dl>
        </section>

        <ActionCenter :actions="workspaceActions" @navigate="onWorkspaceAction" />

        <section class="block">
          <h2 class="block-title">阶段进度</h2>
          <StageProgressPanel :project="project" />
        </section>

        <AuditStatusPanel :project="project" @navigate="goTo" />

        <ContestTimingPanel :timing="contestDashboard?.timing || {}" />

        <JointModelingPanel :base="project.base_name" :revision="project.revision" @changed="refresh" />

        <ModelingDirectionPanel
          v-if="project.current_step <= 1"
          :base="project.base_name"
          :current-step="project.current_step"
          @changed="onModelingDirectionChanged"
        />
      </div>

      <!-- 人工决策 / 咨询 -->
      <SelectionPanel
        v-else-if="activeTab === 'selection'"
        class="rise"
        :base="project.base_name"
        :revision="project.revision"
        @open-file="requestFile"
        @changed="onSelectionChanged"
      />
      <ConsultationPanel
        v-else-if="activeTab === 'consultation'"
        class="rise"
        :base="project.base_name"
        :gate="project.consultation_gate || ''"
        :revision="project.revision"
        @open-file="requestFile"
        @answered="onAnswered"
      />

      <!-- 流程 -->
      <div v-else-if="activeTab === 'pipeline'" class="rise">
        <div class="subnav">
          <div class="seg" role="tablist" aria-label="流程视图">
            <button role="tab" class="seg-b" :class="{ on: pipelineView === 'steps' }" :aria-selected="pipelineView === 'steps' ? 'true' : 'false'" @click="pipelineView = 'steps'">步骤</button>
            <button role="tab" class="seg-b" :class="{ on: pipelineView === 'plan' }" :aria-selected="pipelineView === 'plan' ? 'true' : 'false'" @click="pipelineView = 'plan'">任务图</button>
          </div>
        </div>
        <PipelineTimeline
          v-if="pipelineView === 'steps'"
          :project="project"
          :current-step="project.current_step"
          :steps-data="stepsData"
          :awaiting="project.consultation_pending || project.selection_pending"
          :registry="modelRegistry"
          :assignments="projectAssignments"
          @open-file="requestFile"
          @open-paper="requestPaper"
          @assign="onAssign"
          @manage-models="isAdmin ? (showModels = true) : null"
        />
        <ProblemPlanPanel v-else :base="project.base_name" @open-file="requestFile" />
      </div>

      <!-- 文件 -->
      <ArtifactBrowser
        v-else-if="activeTab === 'artifacts'"
        class="ws-fill panel"
        :base="project.base_name"
        :requested="artifactRequest"
        :current-step="project.current_step"
      />

      <!-- 日志 -->
      <LogConsole
        v-else-if="activeTab === 'logs'"
        class="ws-fill"
        :base="project.base_name"
        :active="activeTab === 'logs'"
      />

      <!-- 求解 -->
      <SolverJobPanel
        v-else-if="activeTab === 'solver'"
        class="rise"
        :base="project.base_name"
        @changed="refresh"
      />

      <!-- 质量与交付 -->
      <div v-else-if="activeTab === 'quality'" class="quality rise">
        <section ref="diagnosticsSection" class="block">
          <h2 class="block-title">诊断</h2>
          <DiagnosticsCard
            v-if="diagnostics && diagnostics.status && diagnostics.status.reason_code"
            :diagnostics="diagnostics"
            @action="onDiagnosticsAction"
          />
          <div v-else-if="project.evidence_validity === 'INVALID' || project.workflow_error" class="invalid panel">
            <div class="invalid-head">
              <Icon name="alert-triangle" :size="16" />
              <span>{{ project.workflow_error === 'RUNNER_EXIT_UNVERIFIED' ? '原运行进程已失联，子进程退出状态尚未确认，请先核对进程归属。' : project.evidence_validity === 'INVALID' ? '当前证据已失效，需要重新核验后才能确认审查结论与交付许可。' : project.reason_summary || '本次执行遇到错误，诊断接口暂未给出具体原因。' }}</span>
            </div>
            <dl v-if="project.workflow_error || project.evidence_errors?.length">
              <div v-if="project.workflow_error"><dt>错误代码</dt><dd class="mono">{{ project.workflow_error }}</dd></div>
              <div v-for="error in project.evidence_errors" :key="error"><dt>证据问题</dt><dd>{{ error }}</dd></div>
            </dl>
          </div>
          <div v-else class="calm panel">
            <Icon name="check-circle" :size="16" />当前没有阻塞运行的问题
          </div>
        </section>
        <section ref="evidenceSection" class="block">
          <h2 class="block-title">证据</h2>
          <EvidenceCockpit
            :evidence="contestDashboard?.evidence || {}"
            :audits="contestDashboard?.audits || []"
            @open-file="requestFile"
            @navigate="goTo"
          />
        </section>
        <section ref="deliverySection" class="block">
          <h2 class="block-title">交付</h2>
          <DeliveryReadinessPanel
            :base="project.base_name"
            :delivery="contestDashboard?.delivery || {}"
            :delivery-allowed="project.delivery_allowed"
            @open-file="requestFile"
          />
        </section>
      </div>
      </div>
    </main>

    <ModelManager v-if="showModels && isAdmin" @close="showModels = false" />
  </div>
</template>

<script>
import { computed, defineAsyncComponent, h, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Icon from './Icon.vue'
import AsyncLoadError from './AsyncLoadError.vue'
import ActionCenter from './ActionCenter.vue'
import ContestTimingPanel from './ContestTimingPanel.vue'
import ModelingDirectionPanel from './ModelingDirectionPanel.vue'
import SelectionPanel from './SelectionPanel.vue'
import AuditStatusPanel from './AuditStatusPanel.vue'
import StageProgressPanel from './StageProgressPanel.vue'
import { relativeTime } from '../lib/api.js'
import { stepConfigKey } from '../lib/steps.js'
import { buildWorkspaceActions, resolveWorkspaceTab, workspaceTabs } from '../lib/workspaceUi.js'
import { useToasts } from '../composables/useToasts.js'
import { useModels } from '../composables/useModels.js'
import {
  executionHint,
  executionLabel,
  executionStatus,
  needsHuman,
  primaryControl,
  projectProgress,
  statusTone,
  stepText,
} from '../lib/projectState.js'
import { useProjectDiagnostics } from '../composables/useProjectDiagnostics.js'
import { useProjectPolling } from '../composables/useProjectPolling.js'
import { useProjectSteps } from '../composables/useProjectSteps.js'
import { useContestDashboard } from '../composables/useContestDashboard.js'
import { useRealtime } from '../composables/useRealtime.js'

// Heavy sub-views are lazy so each tab's code (and KaTeX, via markdown.js used by
// PipelineTimeline/ArtifactBrowser/ConsultationPanel) loads on demand.
const TabFallback = {
  render: () => h('div', { class: 'tab-fallback' }, [h('div', { class: 'spinner' })]),
}
// Without an error component Vue keeps rendering the loading fallback forever
// when a lazy tab chunk cannot be loaded.
const TabError = {
  render: () => h(AsyncLoadError, { inline: true }),
}
const asyncOpts = { loadingComponent: TabFallback, errorComponent: TabError, delay: 120 }
const PipelineTimeline = defineAsyncComponent({ loader: () => import('./PipelineTimeline.vue'), ...asyncOpts })
const ProblemPlanPanel = defineAsyncComponent({ loader: () => import('./ProblemPlanPanel.vue'), ...asyncOpts })
const LogConsole = defineAsyncComponent({ loader: () => import('./LogConsole.vue'), ...asyncOpts })
const ArtifactBrowser = defineAsyncComponent({ loader: () => import('./ArtifactBrowser.vue'), ...asyncOpts })
const SolverJobPanel = defineAsyncComponent({ loader: () => import('./SolverJobPanel.vue'), ...asyncOpts })
const ConsultationPanel = defineAsyncComponent({ loader: () => import('./ConsultationPanel.vue'), ...asyncOpts })
const JointModelingPanel = defineAsyncComponent({ loader: () => import('./JointModelingPanel.vue'), ...asyncOpts })
const DiagnosticsCard = defineAsyncComponent({ loader: () => import('./DiagnosticsCard.vue'), ...asyncOpts })
const ModelManager = defineAsyncComponent({ loader: () => import('./ModelManager.vue'), ...asyncOpts })
const EvidenceCockpit = defineAsyncComponent({ loader: () => import('./EvidenceCockpit.vue'), ...asyncOpts })
const DeliveryReadinessPanel = defineAsyncComponent({ loader: () => import('./DeliveryReadinessPanel.vue'), ...asyncOpts })

const PHASE_NAMES = {
  problem_understanding: '题意与数据',
  model_tournament: '模型竞赛',
  model_and_solve: '建模与求解',
  validation: '结果验证',
  paper_construction: '论文构建',
  deterministic_paper_audit: '确定性论文审计',
  review_and_revision: '审稿与修订',
  final_audit_and_delivery: '最终审计与交付',
}

export default {
  name: 'ProjectWorkspace',
  components: { StageProgressPanel, JointModelingPanel, AuditStatusPanel, Icon, ActionCenter, ContestTimingPanel, ModelingDirectionPanel, SelectionPanel, PipelineTimeline, ProblemPlanPanel, LogConsole, ArtifactBrowser, SolverJobPanel, ConsultationPanel, DiagnosticsCard, ModelManager, EvidenceCockpit, DeliveryReadinessPanel },
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

    const artifactRequest = ref(null)
    const activeTab = ref('overview')
    const pipelineView = ref('steps')
    const killArm = ref(false)
    const nonce = ref(0)
    const showModels = ref(false)
    const clockNow = ref(Math.floor(Date.now() / 1000))
    const diagnosticsSection = ref(null)
    const evidenceSection = ref(null)
    const deliverySection = ref(null)

    const statusLabel = computed(() => executionLabel(props.project))
    const primaryAction = computed(() => primaryControl(props.project))
    const dotClass = computed(() => statusTone(props.project))
    const awaiting = computed(() => needsHuman(props.project))
    const failed = computed(() => ['failed', 'interrupted', 'killed'].includes(executionStatus(props.project)))
    const progress = computed(() => projectProgress(props.project))
    const stepLabel = computed(() => stepText(props.project))
    const nowHint = computed(() => {
      if (executionStatus(props.project) === 'completed') return '工作流已结束；交付是否获准见“质量与交付”。'
      if (failed.value && props.project.reason_summary) return props.project.reason_summary
      return executionHint(props.project)
    })
    const modelRegistry = computed(() => models.value?.registry || [])
    const projectAssignments = computed(() => models.value?.config?.[props.project.base_name] || {})
    const tabs = computed(() => workspaceTabs({
      consultationPending: props.project.consultation_pending,
      selectionPending: props.project.selection_pending,
      diagnostics: diagnostics.value,
    }))
    const workspaceActions = computed(() => buildWorkspaceActions(contestDashboard.value, stepsData.value, props.project))
    const contestPhaseLabel = computed(() => {
      const phase = props.project.contest_phase
      if (!phase) return '未配置'
      return `${phase.id} / 8 · ${PHASE_NAMES[phase.name] || phase.name}`
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
      return `${hours} 小时 ${String(minutes).padStart(2, '0')} 分`
    }
    const contestClockLabel = computed(() => {
      const deadline = Number(props.project.contest_deadline_at || 0)
      const contentFreeze = Number(props.project.content_freeze_at || 0)
      const deliveryFreeze = Number(props.project.delivery_freeze_at || 0)
      if (!deadline) return '未配置'
      if (clockNow.value < contentFreeze) return `距内容冻结 ${formatRemaining(contentFreeze - clockNow.value)}`
      if (clockNow.value < deliveryFreeze) return `最终审计期 · 距提交 ${formatRemaining(deadline - clockNow.value)}`
      if (clockNow.value < deadline) return `交付冻结 · 距提交 ${formatRemaining(deadline - clockNow.value)}`
      return '已截止'
    })

    function performPrimary() {
      if (primaryAction.value?.kind === 'command') act(primaryAction.value.action)
      else if (primaryAction.value?.tab) goTo(primaryAction.value.tab)
    }

    // Go to a view by its current or earlier name (deep links, action items and
    // child panels still use evidence / delivery / diagnostics / plan).
    function goTo(key) {
      const { tab, view } = resolveWorkspaceTab(key)
      if (!tabs.value.some((available) => available.key === tab)) {
        activeTab.value = 'overview'
        return
      }
      activeTab.value = tab
      if (tab === 'pipeline' && view) pipelineView.value = view
      if (tab === 'quality' && view) {
        const target = { diagnostics: diagnosticsSection, evidence: evidenceSection, delivery: deliverySection }[view]
        nextTick(() => target?.value?.scrollIntoView?.({ block: 'start', behavior: 'smooth' }))
      }
    }

    function fetchSteps() { return fetchProjectSteps(props.project.base_name) }
    function fetchDiagnostics() { return fetchProjectDiagnostics(props.project.base_name) }
    function fetchDashboard() { return fetchContestDashboard(props.project.base_name) }

    function refresh() {
      Promise.allSettled([fetchSteps(), fetchDiagnostics(), fetchDashboard()])
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
      goTo(action?.tab || 'overview')
    }

    function onEsc(event) {
      if (event.key === 'Escape' && !killArm.value) emit('close')
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
      if (actionId === 'revert_upstream_edits_and_resume') {
        // Resuming rechecks the same change set; it only gets past the pause
        // once the upstream-owned edits listed as evidence are reverted.
        if (typeof window === 'undefined' || window.confirm('确认已撤销证据中列出的上游文件改动？恢复后会重新检查这次变更。')) {
          act('resume')
        }
        return
      }
      if (actionId === 'retry_failed_step') {
        const step = diagnostics.value?.status?.current_step
        const reason = typeof window === 'undefined' ? '' : window.prompt(`重试 Step ${step}（尝试次数清零）的理由，会记录到工作流事件`)
        if (reason && reason.trim() && step !== null && step !== undefined) {
          emit('action', props.project, 'retry_step', { reason: reason.trim(), step })
        }
        return
      }
      if (actionId === 'grant_semantic_reopen') {
        const reason = typeof window === 'undefined' ? '' : window.prompt('批准这次回退的理由（会记录到工作流事件）')
        if (reason && reason.trim()) {
          killArm.value = false
          emit('action', props.project, 'grant_reopen', { reason: reason.trim() })
        }
        return
      }
      if (actionId === 'open_runner_log') {
        activeTab.value = 'logs'
        return
      }
      if (actionId === 'open_audit_timeline') {
        goTo('diagnostics')
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
        open_entry_gate: { path: 'entry_gate.md', type: 'markdown', name: 'entry_gate.md' },
        open_reviewer_entry_artifacts: { path: 'reviewer_entry_map.md', type: 'markdown', name: 'reviewer_entry_map.md' },
        open_consultation_request: { path: `consultation/${props.project.consultation_gate || 'dynamic'}_request.md`, type: 'markdown', name: 'consultation request' },
        open_human_review: { path: 'human_review.md', type: 'markdown', name: 'human_review.md' },
        open_selection_request: { path: `selection/${props.project.selection_gate || 'step3'}_request.md`, type: 'markdown', name: 'selection request' },
        open_selection_evidence: { path: `selection/${props.project.selection_gate || 'step3'}_options.json`, type: 'json', name: 'selection options' },
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

    watch(() => props.project.base_name, () => {
      resetSteps()
      resetDiagnostics()
      resetContestDashboard()
      fetchSteps()
      fetchDiagnostics()
      fetchDashboard()
    })
    watch(() => props.project.current_step, () => {
      fetchSteps()
      fetchDiagnostics()
      fetchDashboard()
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
    let syncingTab = false
    // URL -> tab. Only act when the URL explicitly carries a tab, so an absent
    // ?tab leaves the human-decision auto-jump / default overview intact.
    // Earlier names (plan, evidence, delivery, diagnostics) still resolve.
    watch(() => route.query.tab, (tab) => {
      if (typeof tab !== 'string' || !tab) return
      const { tab: resolved, view } = resolveWorkspaceTab(tab)
      if (!tabs.value.some((available) => available.key === resolved)) return
      // An alias with a nested view (?tab=plan, ?tab=evidence) still selects
      // its subsection when the grouped tab is already active.
      if (resolved === activeTab.value && !view) return
      syncingTab = true
      goTo(tab)
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
      contestClockTimer = window.setInterval(() => {
        clockNow.value = Math.floor(Date.now() / 1000)
      }, 30000)
    })

    onBeforeUnmount(() => {
      stopPolling()
      if (contestClockTimer) clearInterval(contestClockTimer)
      window.removeEventListener('keydown', onEsc)
    })

    return {
      stepsData,
      contestDashboard,
      contestDashboardLoading,
      workspaceActions,
      activeTab,
      pipelineView,
      tabs,
      loading,
      artifactRequest,
      killArm,
      showModels,
      diagnostics,
      diagnosticsLoading,
      diagnosticsSection,
      evidenceSection,
      deliverySection,
      statusLabel,
      modelRegistry,
      projectAssignments,
      dotClass,
      awaiting,
      failed,
      progress,
      nowHint,
      primaryAction,
      performPrimary,
      stepLabel,
      contestPhaseLabel,
      schedulerLabel,
      contestClockLabel,
      rel: relativeTime,
      fetchSteps,
      fetchDiagnostics,
      fetchDashboard,
      refresh,
      act,
      goTo,
      requestFile,
      requestPaper,
      onAnswered,
      onModelingDirectionChanged,
      onSelectionChanged,
      onWorkspaceAction,
      onDiagnosticsAction,
      onAssign,
    }
  },
}
</script>

<style scoped>
.ws {
  position: fixed; inset: 0; z-index: 200;
  display: flex; flex-direction: column;
  min-width: 0; overflow: hidden;
  background: var(--bg);
}

/* ---- header ---- */
.ws-top { flex-shrink: 0; background: var(--surface); border-bottom: 1px solid var(--line); }
.ws-top-in {
  max-width: var(--content-w); margin: 0 auto;
  display: flex; align-items: center; gap: 12px;
  padding: 14px 24px 10px;
}
.back { margin-left: -8px; }
.ident { flex: 1; min-width: 0; }
.title { font-size: 18px; line-height: 1.35; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.meta { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 12px; margin-top: 4px; font-size: 13px; }
.meta .pill .dot { width: 6px; height: 6px; }
.step { color: var(--ink); }
.muted { color: var(--ink-3); }
.run { font-size: 12px; }
.controls { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
.armed { background: var(--bad); color: #fff; border-color: var(--bad); }
.spin { animation: spin 0.8s linear infinite; }

.tabs {
  max-width: var(--content-w); margin: 0 auto;
  display: flex; gap: 2px; padding: 0 16px;
  overflow-x: auto; scrollbar-width: none;
}
.tabs::-webkit-scrollbar { display: none; }
.tab {
  position: relative;
  border: 0; background: none; cursor: pointer; white-space: nowrap;
  padding: 10px 10px 11px; color: var(--ink-2);
  font: 500 13.5px var(--sans);
}
.tab:hover { color: var(--ink); }
.tab.on { color: var(--ink); }
.tab.on::after { content: ''; position: absolute; left: 8px; right: 8px; bottom: -1px; height: 2px; border-radius: 2px; background: var(--accent); }
.tab.attention { color: var(--amber); }
.badge { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: var(--amber); margin-left: 6px; vertical-align: 2px; }

/* ---- body ---- */
.ws-body { flex: 1; min-height: 0; overflow-y: auto; }
.view { max-width: var(--content-w); margin: 0 auto; padding: 22px 24px 56px; }
.view.fill { padding-bottom: 24px; }
.ws-fill { min-height: calc(100vh - 190px); }

.overview, .quality { display: flex; flex-direction: column; gap: 18px; }
.block { display: flex; flex-direction: column; gap: 10px; }
.block-title { font-size: 14px; font-weight: 600; }

/* 当前状态 */
.now { padding: 18px 20px; display: grid; grid-template-columns: minmax(0, 1fr) 220px; gap: 14px 24px; align-items: start; }
.now-label { font-size: 12px; color: var(--ink-3); }
.now-step { font-size: 17px; font-weight: 600; margin-top: 2px; }
.now-hint { margin-top: 4px; color: var(--ink-2); }
.now-hint.warn { color: var(--amber); }
.now-hint.bad { color: var(--bad); }
.now-progress { display: flex; align-items: center; gap: 10px; padding-top: 22px; font-size: 13px; color: var(--ink-2); }
.bar { flex: 1; height: 6px; border-radius: 6px; background: var(--panel-3); overflow: hidden; }
.bar span { display: block; height: 100%; border-radius: 6px; background: var(--ink-3); }
.bar span.live { background: var(--accent); }
.bar span.ok { background: var(--ok); }
.bar span.amber { background: var(--amber); }
.bar span.bad { background: var(--bad); }
.facts {
  grid-column: 1 / -1;
  display: flex; flex-wrap: wrap; gap: 8px 32px;
  padding-top: 14px; border-top: 1px solid var(--line);
}
.facts dt { font-size: 12px; color: var(--ink-3); }
.facts dd { font-size: 13.5px; margin-top: 1px; }

.subnav { margin-bottom: 14px; }
.seg { display: inline-flex; padding: 2px; gap: 2px; background: var(--panel-2); border-radius: var(--r); }
.seg-b { border: 0; background: transparent; color: var(--ink-2); font: 500 13px var(--sans); padding: 5px 14px; border-radius: 6px; cursor: pointer; }
.seg-b:hover { color: var(--ink); }
.seg-b.on { background: var(--surface); color: var(--ink); box-shadow: 0 1px 2px rgba(0,0,0,0.08); }

.calm { display: flex; align-items: center; gap: 8px; padding: 14px 16px; color: var(--ok); }
.invalid { padding: 14px 16px; border-color: var(--bad); }
.invalid-head { display: flex; align-items: flex-start; gap: 8px; color: var(--bad); }
.invalid-head :deep(svg) { flex-shrink: 0; margin-top: 2px; }
.invalid dl { margin-top: 10px; display: grid; gap: 6px; font-size: 12.5px; }
.invalid dl div { display: grid; grid-template-columns: 72px minmax(0, 1fr); gap: 10px; }
.invalid dt { color: var(--ink-3); }
.invalid dd { color: var(--ink-2); overflow-wrap: anywhere; }
.quality .block { scroll-margin-top: 16px; }

@media (max-width: 720px) {
  .ws-top-in { padding: 10px 12px 6px; flex-wrap: wrap; }
  .controls { width: 100%; justify-content: flex-end; }
  .title { font-size: 16px; }
  .run { display: none; }
  .tabs { padding: 0 6px; }
  .view { padding: 16px 12px 40px; }
  .now { grid-template-columns: 1fr; }
  .now-progress { padding-top: 0; }
  .hide-xs { display: none; }
}
</style>
