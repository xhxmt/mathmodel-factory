<template>
  <section class="joint panel" aria-label="联合建模设置">
    <div class="joint-head">
      <div>
        <h3>GPT Pro + Claude Fable 联合建模</h3>
        <p class="dim">Claude 生成候选 → Pro 人工复核 → Claude 综合 → 人工选模</p>
      </div>
      <button class="btn btn-sm" :class="config?.enabled ? 'btn-amber' : 'btn-ghost'"
        role="switch" :aria-checked="config?.enabled === true"
        :disabled="loading || saving || !config?.can_configure"
        @click="toggle">
        {{ saving ? '保存中…' : loading ? '加载中…' : config?.enabled ? '已开启' : '未开启' }}
      </button>
    </div>
    <p v-if="error" class="joint-error" role="alert">{{ error }} <button class="btn btn-sm btn-ghost" @click="fetch">重试</button></p>
    <template v-if="config">
      <p v-if="!config.enabled" class="dim">默认关闭，仅在你手动开启后用于当前项目。请在候选生成开始前选择。</p>
      <p v-else class="joint-phase">{{ phaseLabel }} <span class="mono dim">· {{ config.model }}</span></p>
      <p v-if="config.configuration_blocker" class="dim">{{ config.configuration_blocker }}</p>
      <div v-if="config.synthesis" class="joint-synthesis">
        <h4>Claude 综合意见</h4>
        <p>{{ config.synthesis.summary }}</p>
        <article v-for="item in config.synthesis.items" :key="item.finding_id">
          <div><span class="tag">{{ actionLabel(item.action) }}</span> <span class="mono">{{ item.finding_id }}</span> <span v-if="item.human_needed" class="amber">需要人工判断</span></div>
          <blockquote>{{ item.pro_quote }}</blockquote>
          <p>{{ item.rationale }}</p>
          <p v-if="item.proposed_change">建议：{{ item.proposed_change }}</p>
        </article>
        <p class="dim">这些意见供选模参考；请在人工选模面板确认采用的候选。</p>
      </div>
    </template>
  </section>
</template>

<script>
import { Projects } from '../lib/api.js'

export default {
  name: 'JointModelingPanel',
  props: { base: { type: String, required: true }, revision: { type: Number, default: null } },
  emits: ['changed'],
  data() { return { config: null, loading: true, saving: false, error: '', requestGeneration: 0 } },
  computed: {
    phaseLabel() {
      return { candidates: '等待或正在生成 Claude 候选', awaiting_pro: '等待 Pro 候选复核回填', synthesis: '等待或正在综合 Pro 意见', human_selection: '等待人工选模', selected: '已完成人工选模', awaiting_risk_review: '等待求解前 Pro 风险复核' }[this.config?.phase] || '联合建模已开启'
    },
  },
  watch: { base() { this.config = null; this.fetch() }, revision() { if (!this.saving) this.fetch() } },
  mounted() { this.fetch() },
  beforeUnmount() { this.requestGeneration++ },
  methods: {
    actionLabel(action) { return { ACCEPT: '采纳', PARTIAL: '部分采纳', REJECT: '不采纳' }[action] || action },
    async fetch() {
      const generation = ++this.requestGeneration
      this.loading = true
      try {
        const config = await Projects.jointModeling(this.base)
        if (generation !== this.requestGeneration) return
        this.config = config
        this.error = ''
      } catch (error) {
        if (generation === this.requestGeneration) { this.config = null; this.error = error.response?.data?.detail || '联合建模设置加载失败' }
      } finally { if (generation === this.requestGeneration) this.loading = false }
    },
    async toggle() {
      if (this.saving || this.loading || !this.config?.can_configure) return
      const generation = ++this.requestGeneration
      this.saving = true
      this.error = ''
      try {
        const config = await Projects.setJointModeling(this.base, !this.config.enabled, this.config.workflow_revision)
        if (generation !== this.requestGeneration) return
        this.config = config
        this.$emit('changed')
      } catch (error) {
        if (generation === this.requestGeneration) this.error = error.response?.data?.detail || '联合建模设置保存失败，请刷新后重试'
      } finally { this.saving = false }
    },
  },
}
</script>

<style scoped>
.joint { padding: 18px; }
.joint-head { display: flex; align-items: center; justify-content: space-between; gap: 18px; }
h3 { font-size: 14px; margin: 0 0 8px; }
h4 { font-size: 13px; margin: 0 0 12px; }
p { font-size: 12px; line-height: 1.75; margin: 8px 0 0; overflow-wrap: anywhere; }
.joint-phase { color: var(--amber); }
.joint-error { color: var(--red, #e98181); }
.joint-synthesis { margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--line); }
article { margin-top: 14px; padding: 12px; border: 1px solid var(--line); border-radius: 6px; }
blockquote { margin: 10px 0; padding-left: 12px; border-left: 2px solid var(--amber); font-size: 12px; white-space: pre-wrap; }
@media (max-width: 600px) { .joint-head { align-items: flex-start; } }
</style>
