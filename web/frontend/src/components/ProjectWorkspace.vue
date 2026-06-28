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
        <button class="btn btn-sm btn-ghost" @click="showModels = true" title="模型管理">
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

    <div class="ws-scroll">
      <!-- priority: consultation -->
      <ConsultationPanel
        v-if="project.consultation_pending"
        class="rise"
        :base="project.base_name"
        :gate="project.consultation_gate || ''"
        @open-file="requestFile"
        @answered="onAnswered"
      />

      <!-- pipeline -->
      <PipelineTimeline
        class="rise"
        :current-step="project.current_step"
        :steps-data="stepsData"
        :awaiting="project.consultation_pending"
        :registry="modelRegistry"
        :assignments="projectAssignments"
        @open-file="requestFile"
        @open-paper="requestPaper"
        @assign="onAssign"
        @manage-models="showModels = true"
      />

      <DiagnosticsCard
        v-if="diagnostics && diagnostics.status && diagnostics.status.reason_code"
        class="rise"
        :diagnostics="diagnostics"
        @action="onDiagnosticsAction"
      />

      <!-- logs ∥ artifacts -->
      <div class="ws-split rise">
        <LogConsole class="ws-logs" :base="project.base_name" :active="true" />
        <ArtifactBrowser class="ws-art panel" :base="project.base_name" :requested="artifactRequest" />
      </div>
    </div>

    <ModelManager v-if="showModels" @close="showModels = false" />
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
import Icon from './Icon.vue'
import PipelineTimeline from './PipelineTimeline.vue'
import LogConsole from './LogConsole.vue'
import ArtifactBrowser from './ArtifactBrowser.vue'
import ConsultationPanel from './ConsultationPanel.vue'
import DiagnosticsCard from './DiagnosticsCard.vue'
import ModelManager from './ModelManager.vue'
import CloudAcceleratorDialog from './CloudAcceleratorDialog.vue'
import { Cloud, Projects, relativeTime } from '../lib/api.js'
import { stepByIndex, stepConfigKey } from '../lib/steps.js'
import { useToasts } from '../composables/useToasts.js'
import { useModels } from '../composables/useModels.js'
import { statusLabel as mapStatusLabel } from '../lib/status.js'

export default {
  name: 'ProjectWorkspace',
  components: { Icon, PipelineTimeline, LogConsole, ArtifactBrowser, ConsultationPanel, DiagnosticsCard, ModelManager, CloudAcceleratorDialog },
  props: { project: { type: Object, required: true } },
  emits: ['close', 'action', 'refresh'],
  setup() {
    const m = useModels()
    return { _models: m.models, _loadModels: m.load, _saveConfig: m.saveConfig }
  },
  created() {
    // Non-reactive step-poll caches (not rendered → kept out of data()).
    this._stepsAbort = null
    this._stepsFp = ''
    this._stepsPromise = null
    this._visH = null
  },
  data() {
    return {
      stepsData: null, loading: false, artifactRequest: null, killArm: false,
      nonce: 0, timer: null, showModels: false,
      diagnostics: null, diagnosticsLoading: false,
      showCloudDialog: false, cloudEstimate: { local: 8, cloud: 2 }, lastStep: null,
      cloudConfig: null, cloudConfigLoading: false, cloudSaving: false,
    }
  },
  computed: {
    statusLabel() { return mapStatusLabel(this.project.status) },
    modelRegistry() { return this._models?.registry || [] },
    // This project's own per-step overrides ({ step_N: {primary, fallback} }).
    projectAssignments() { return this._models?.config?.[this.project.base_name] || {} },
    dotClass() {
      return {
        running: 'live', awaiting_consultation: 'amber', completed: 'ok',
        paused: 'paused', failed: 'bad', killed: 'bad',
      }[this.project.status] || ''
    },
    canResume() { return ['paused', 'ready', 'awaiting_consultation'].includes(this.project.status) },
    cloudEnabled() { return this.cloudConfig?.enabled || false },
    cloudSwitchLabel() {
      if (this.cloudSaving) return '保存中'
      if (this.cloudConfigLoading && !this.cloudConfig) return '云端...'
      return this.cloudEnabled ? '云端开启' : '云端关闭'
    },
    cloudSwitchTitle() {
      return this.cloudEnabled ? '关闭本项目云端加速' : '开启本项目云端加速'
    },
    stepLabel() {
      const c = this.project.current_step
      const gate = this.stepsData?.editorial_gate
      if (c >= 16) return 'STEP 16 / 16 · 已完成'
      if (c === 8 && gate && !gate.ready) return 'STEP 8.5 / 16 · 阅卷入口设计'
      const active = stepByIndex(Math.min(16, c + 1))
      return `STEP ${Math.max(0, c + 1)} / 16 · ${active ? active.name : ''}`
    },
  },
  watch: {
    'project.base_name'() {
      this.stepsData = null
      this._stepsFp = ''
      this.diagnostics = null
      this.fetchSteps()
      this.fetchDiagnostics()
      this.fetchCloudConfig()
    },
    'project.current_step'(newStep) {
      this.fetchSteps()
      this.fetchDiagnostics()
      this.checkCloudAccelerator(newStep)
    },
  },
  mounted() {
    this.fetchSteps()
    this.fetchDiagnostics()
    this.fetchCloudConfig()
    this._loadModels().catch(() => {})
    this.timer = setInterval(() => {
      if (this.project.is_running && !document.hidden) {
        this.fetchSteps()
        this.fetchDiagnostics()
      }
    }, 8000)
    this._visH = () => {
      if (document.hidden) {
        if (this._stepsAbort) { try { this._stepsAbort.abort() } catch (e) { /* */ } }
      } else if (this.project.is_running) {
        this.fetchSteps()
        this.fetchDiagnostics()
      }
    }
    document.addEventListener('visibilitychange', this._visH)
    window.addEventListener('keydown', this.onEsc)
    this.lastStep = this.project.current_step
  },
  beforeUnmount() {
    if (this.timer) clearInterval(this.timer)
    if (this._stepsAbort) { try { this._stepsAbort.abort() } catch (e) { /* */ } }
    if (this._visH) { document.removeEventListener('visibilitychange', this._visH); this._visH = null }
    window.removeEventListener('keydown', this.onEsc)
  },
  methods: {
    rel: relativeTime,
    onEsc(e) { if (e.key === 'Escape' && !this.killArm) this.$emit('close') },
    // Fingerprint covering everything PipelineTimeline renders, so an unchanged
    // steps payload skips the re-assignment (and the timeline re-render).
    stepsFp(s) {
      if (!s) return ''
      const a = s.steps.map((st) =>
        st.artifacts.length + '/' +
        st.artifacts.reduce((mx, o) => Math.max(mx, o.mtime ? Date.parse(o.mtime.replace(' ', 'T')) : 0), 0) + '/' +
        st.artifacts.reduce((sum, o) => sum + (o.size || 0), 0)
      ).join(',')
      const g = s.editorial_gate
      return `${s.current_step}|${s.verdict}|${s.open_issues}|${s.paper_available}|${g?.ready}|${g?.verdict}|${a}`
    },
    async fetchSteps() {
      // Coalesce timer + watcher firing near-simultaneously into one request.
      if (this._stepsPromise) return this._stepsPromise
      if (this._stepsAbort) { try { this._stepsAbort.abort() } catch (e) { /* */ } }
      const ac = new AbortController()
      this._stepsAbort = ac
      this.loading = true
      this._stepsPromise = (async () => {
        try {
          const s = await Projects.steps(this.project.base_name, ac.signal)
          this._stepsAbort = null
          const fp = this.stepsFp(s)
          if (fp !== this._stepsFp) { this._stepsFp = fp; this.stepsData = s }
        } catch (e) {
          if (e?.code !== 'ERR_CANCELED') { /* keep prior stepsData */ }
        } finally {
          this.loading = false
          this._stepsPromise = null
        }
      })()
      return this._stepsPromise
    },
    async fetchDiagnostics() {
      this.diagnosticsLoading = true
      try {
        this.diagnostics = await Projects.diagnostics(this.project.base_name)
      } catch (e) {
        this.diagnostics = null
      } finally {
        this.diagnosticsLoading = false
      }
    },
    async fetchCloudConfig() {
      this.cloudConfigLoading = true
      try {
        this.cloudConfig = await Cloud.projectConfig(this.project.base_name)
      } catch (e) {
        this.cloudConfig = { enabled: false }
      } finally {
        this.cloudConfigLoading = false
      }
    },
    async toggleCloudAcceleration() {
      if (this.cloudSaving || this.cloudConfigLoading) return
      this.cloudSaving = true
      try {
        const response = this.cloudEnabled
          ? await Cloud.disable(this.project.base_name)
          : await Cloud.enable(this.project.base_name)
        this.cloudConfig = response.config || await Cloud.projectConfig(this.project.base_name)
        useToasts().success(this.cloudEnabled ? '云端加速已开启' : '云端加速已关闭', this.project.base_name)
      } catch (e) {
        useToasts().error(e.response?.data?.detail || '云端加速设置失败')
      } finally {
        this.cloudSaving = false
      }
    },
    refresh() { this.fetchSteps(); this.fetchDiagnostics(); this.fetchCloudConfig(); this.$emit('refresh') },
    act(a) { this.killArm = false; this.$emit('action', this.project, a) },
    requestFile(f) { this.artifactRequest = { ...f, _n: ++this.nonce } },
    requestPaper() { this.artifactRequest = { __paper: true, _n: ++this.nonce } },
    onAnswered() { this.$emit('refresh'); this.fetchSteps(); this.fetchDiagnostics() },
    onDiagnosticsAction(actionId) {
      if (actionId === 'refresh_status') {
        this.fetchDiagnostics()
        this.fetchSteps()
        this.$emit('refresh')
        return
      }
      if (actionId === 'resume_project') {
        this.act('resume')
        return
      }
      const evidenceMap = {
        open_runner_log: { path: 'logs/runner.log', type: 'text', name: 'runner.log' },
        open_entry_gate: { path: 'entry_gate.md', type: 'markdown', name: 'entry_gate.md' },
        open_reviewer_entry_artifacts: { path: 'reviewer_entry_map.md', type: 'markdown', name: 'reviewer_entry_map.md' },
        open_consultation_request: { path: `consultation/${this.project.consultation_gate || 'dynamic'}_request.md`, type: 'markdown', name: 'consultation request' },
        open_human_review: { path: 'human_review.md', type: 'markdown', name: 'human_review.md' },
        open_failed_artifact: { path: 'logs/runner.log', type: 'text', name: 'runner.log' },
      }
      const req = evidenceMap[actionId]
      if (req) this.requestFile(req)
    },
    // PipelineTimeline asks to set step primary/fallback for THIS project.
    async onAssign(step, assignment) {
      const steps = JSON.parse(JSON.stringify(this.projectAssignments))
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
        await this._saveConfig(this.project.base_name, steps)
        useToasts().success(`步骤 ${step.key === '8_5' ? '8.5' : step.index} 模型已更新`)
      } catch (e) {
        useToasts().error(e.response?.data?.detail || '保存模型选择失败')
      }
    },
    checkCloudAccelerator(currentStep) {
      // Trigger cloud dialog for compute-intensive steps
      if (this.lastStep !== null && currentStep !== this.lastStep) {
        const COMPUTE_STEPS = [5, 6] // Step 5: full solve, Step 6: sensitivity
        if (COMPUTE_STEPS.includes(currentStep)) {
          // Estimate based on step type
          if (currentStep === 5) {
            this.cloudEstimate = { local: 6, cloud: 1.5 }
          } else if (currentStep === 6) {
            this.cloudEstimate = { local: 8, cloud: 2 }
          }
          // Show dialog after a short delay (let step start first)
          setTimeout(() => {
            if (this.project.is_running && !this.showCloudDialog) {
              this.showCloudDialog = true
            }
          }, 5000)
        }
      }
      this.lastStep = currentStep
    },
    onCloudEnabled() {
      this.$emit('refresh')
      this.fetchSteps()
      this.fetchCloudConfig()
    },
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

.ws-scroll { flex: 1; overflow-y: auto; padding: 18px 20px 32px; display: flex; flex-direction: column; gap: 16px; }

.ws-split { display: grid; grid-template-columns: 0.92fr 1.25fr; gap: 16px; min-height: 520px; }
.ws-logs { height: 62vh; min-height: 420px; }
.ws-art { height: 62vh; min-height: 420px; overflow: hidden; }

.tag { display: inline-flex; align-items: center; gap: 6px; font: 600 11px/1 var(--mono); letter-spacing: 0.05em; text-transform: uppercase; padding: 5px 9px; border-radius: var(--r-sm); border: 1px solid var(--line); background: var(--panel-2); color: var(--ink-2); }
.st-running { color: var(--live); border-color: var(--live-dim); background: var(--live-dim); }
.st-awaiting_consultation { color: var(--amber); border-color: var(--amber-line); background: var(--amber-dim); }
.st-completed { color: var(--ok); border-color: var(--ok-dim); background: var(--ok-dim); }
.st-paused { color: var(--paused); }
.st-failed, .st-killed { color: var(--bad); border-color: var(--bad-dim); background: var(--bad-dim); }

@media (max-width: 1080px) {
  .ws-split { grid-template-columns: 1fr; }
  .ws-logs, .ws-art { height: auto; min-height: 380px; }
}
@media (max-width: 640px) {
  .wh-right .btn span { display: none; }
  .cloud-switch { padding-right: 5px; }
}
</style>
