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

      <!-- logs ∥ artifacts -->
      <div class="ws-split rise">
        <LogConsole class="ws-logs" :base="project.base_name" :active="true" />
        <ArtifactBrowser class="ws-art panel" :base="project.base_name" :requested="artifactRequest" />
      </div>
    </div>

    <ModelManager v-if="showModels" @close="showModels = false" @saved="fetchModels" />
  </div>
</template>

<script>
import Icon from './Icon.vue'
import PipelineTimeline from './PipelineTimeline.vue'
import LogConsole from './LogConsole.vue'
import ArtifactBrowser from './ArtifactBrowser.vue'
import ConsultationPanel from './ConsultationPanel.vue'
import ModelManager from './ModelManager.vue'
import { Projects, Models, relativeTime } from '../lib/api.js'
import { STEPS, stepByIndex } from '../lib/steps.js'
import { useToasts } from '../composables/useToasts.js'

const STATUS_LABEL = {
  running: '运行中', paused: '已暂停', completed: '已完成',
  awaiting_consultation: '等待咨询', ready: '就绪', setup: '初始化',
  failed: '失败', killed: '已终止',
}

export default {
  name: 'ProjectWorkspace',
  components: { Icon, PipelineTimeline, LogConsole, ArtifactBrowser, ConsultationPanel, ModelManager },
  props: { project: { type: Object, required: true } },
  emits: ['close', 'action', 'refresh'],
  data() {
    return {
      stepsData: null, loading: false, artifactRequest: null, killArm: false,
      nonce: 0, timer: null, showModels: false, modelsData: null,
    }
  },
  computed: {
    statusLabel() { return STATUS_LABEL[this.project.status] || this.project.status },
    modelRegistry() { return this.modelsData?.registry || [] },
    // This project's own per-step overrides ({ step_N: {primary, fallback} }).
    projectAssignments() { return this.modelsData?.config?.[this.project.base_name] || {} },
    dotClass() {
      return {
        running: 'live', awaiting_consultation: 'amber', completed: 'ok',
        paused: 'paused', failed: 'bad', killed: 'bad',
      }[this.project.status] || ''
    },
    canResume() { return ['paused', 'ready', 'awaiting_consultation'].includes(this.project.status) },
    stepLabel() {
      const c = this.project.current_step
      if (c >= 16) return 'STEP 16 / 16 · 已完成'
      const active = stepByIndex(Math.min(16, c + 1))
      return `STEP ${Math.max(0, c + 1)} / 16 · ${active ? active.name : ''}`
    },
  },
  watch: {
    'project.base_name'() { this.stepsData = null; this.fetchSteps() },
    'project.current_step'() { this.fetchSteps() },
  },
  mounted() {
    this.fetchSteps()
    this.fetchModels()
    this.timer = setInterval(() => { if (this.project.is_running) this.fetchSteps() }, 8000)
    window.addEventListener('keydown', this.onEsc)
  },
  beforeUnmount() {
    if (this.timer) clearInterval(this.timer)
    window.removeEventListener('keydown', this.onEsc)
  },
  methods: {
    rel: relativeTime,
    onEsc(e) { if (e.key === 'Escape' && !this.killArm) this.$emit('close') },
    async fetchSteps() {
      this.loading = true
      try { this.stepsData = await Projects.steps(this.project.base_name) }
      catch (e) { /* keep prior */ }
      finally { this.loading = false }
    },
    refresh() { this.fetchSteps(); this.$emit('refresh') },
    act(a) { this.killArm = false; this.$emit('action', this.project, a) },
    requestFile(f) { this.artifactRequest = { ...f, _n: ++this.nonce } },
    requestPaper() { this.artifactRequest = { __paper: true, _n: ++this.nonce } },
    onAnswered() { this.$emit('refresh'); this.fetchSteps() },
    async fetchModels() {
      try { this.modelsData = await Models.get() }
      catch (e) { /* models optional; keep prior */ }
    },
    // PipelineTimeline asks to set step <index> primary/fallback for THIS project.
    async onAssign(index, assignment) {
      const steps = JSON.parse(JSON.stringify(this.projectAssignments))
      const key = 'step_' + index
      const primary = (assignment.primary || '').trim()
      const fallback = (assignment.fallback || '').trim()
      if (!primary && !fallback) delete steps[key]
      else {
        const entry = { primary }
        if (fallback) entry.fallback = fallback
        steps[key] = entry
      }
      try {
        await Models.saveConfig(this.project.base_name, steps)
        await this.fetchModels()
        useToasts().success(`步骤 ${index} 模型已更新`)
      } catch (e) {
        useToasts().error(e.response?.data?.detail || '保存模型选择失败')
        await this.fetchModels()
      }
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
}
</style>
