<template>
  <div class="cons panel">
    <div class="cons-head">
      <div class="ch-left">
        <span class="ch-icon"><Icon name="alert-triangle" :size="16" /></span>
        <div>
          <div class="ch-title">{{ req ? req.title : '人工咨询' }}</div>
          <div class="ch-sub mono">
            <span class="tag tag-amber">{{ gate }}</span>
            <span v-if="req">STEP {{ req.step }}</span>
            <span v-if="req" class="dim">· {{ req.created }}</span>
          </div>
        </div>
      </div>
      <span class="ch-flag label">需要你决定</span>
    </div>

    <div v-if="loading" class="cons-state"><div class="spinner"></div></div>
    <div v-else-if="!req" class="cons-state">无法加载咨询请求</div>

    <div v-else class="cons-body">
      <section class="workflow sec">
        <div class="flow-head">
          <span class="sec-h label amber">决策工作流</span>
          <span class="flow-state mono" :class="{ ready: workflow.ready }">
            {{ workflow.ready ? '可提交' : `缺 ${workflow.missing.length} 项` }}
          </span>
        </div>
        <div class="flow-grid">
          <div v-for="check in workflow.checks" :key="check.key" class="flow-check" :class="{ ok: check.ok }">
            <Icon :name="check.ok ? 'check-circle' : 'alert-triangle'" :size="13" />
            <span>{{ check.label }}</span>
          </div>
        </div>
        <div v-if="workflow.evidence.length" class="evidence-strip">
          <button
            v-for="f in workflow.evidence"
            :key="f.path"
            class="evidence-chip"
            @click="$emit('open-file', { path: f.path, name: f.name, type: guessType(f.path) })"
          >
            <Icon :name="guessIcon(f.path)" :size="12" />
            <span class="mono">{{ f.name }}</span>
          </button>
        </div>
      </section>

      <section v-if="req.background" class="sec">
        <div class="sec-h label">项目背景</div>
        <div class="md" v-html="md(req.background)"></div>
      </section>

      <section v-if="req.impact" class="sec">
        <div class="sec-h label">决策影响</div>
        <div class="md" v-html="md(req.impact)"></div>
      </section>

      <section v-if="req.key_files && req.key_files.length" class="sec">
        <div class="sec-h label">关键文件</div>
        <div class="kf">
          <button v-for="f in req.key_files" :key="f" class="kf-chip" @click="$emit('open-file', { path: f, name: f.split('/').pop(), type: guessType(f) })">
            <Icon :name="guessIcon(f)" :size="12" />
            <span class="mono">{{ f }}</span>
          </button>
        </div>
      </section>

      <section class="sec highlight">
        <div class="sec-h label amber">需要决定的事项</div>
        <div class="md" v-html="md(req.content)"></div>
      </section>

      <!-- AI工具快捷入口 -->
      <section class="sec ai-tools">
        <div class="sec-h label">AI 工具快捷入口</div>
        <div class="tools-grid">
          <a
            href="https://chatgpt.com/?model=gpt-4o"
            target="_blank"
            rel="noopener noreferrer"
            class="tool-card"
          >
            <div class="tool-icon gpt"><Icon name="cpu" :size="18" /></div>
            <div class="tool-info">
              <div class="tool-name">ChatGPT Pro</div>
              <div class="tool-desc">GPT-4 with extended thinking</div>
            </div>
            <Icon name="external-link" :size="14" class="tool-ext" />
          </a>
          <a
            href="https://gemini.google.com/app"
            target="_blank"
            rel="noopener noreferrer"
            class="tool-card"
          >
            <div class="tool-icon gemini"><Icon name="zap" :size="18" /></div>
            <div class="tool-info">
              <div class="tool-name">Gemini Deep Think</div>
              <div class="tool-desc">Google's advanced reasoning model</div>
            </div>
            <Icon name="external-link" :size="14" class="tool-ext" />
          </a>
          <a
            href="https://claude.ai/new"
            target="_blank"
            rel="noopener noreferrer"
            class="tool-card"
          >
            <div class="tool-icon claude"><Icon name="message-circle" :size="18" /></div>
            <div class="tool-info">
              <div class="tool-name">Claude Code</div>
              <div class="tool-desc">Coding and analysis assistance</div>
            </div>
            <Icon name="external-link" :size="14" class="tool-ext" />
          </a>
        </div>
        <div class="tools-hint">
          <Icon name="lightbulb" :size="14" />
          <span>点击打开对应 AI 工具，将咨询内容粘贴进行分析，然后将结论粘贴到下方表单提交。</span>
        </div>
      </section>

      <section v-if="req.suggestions" class="sec">
        <div class="sec-h label">回答建议</div>
        <div class="md" v-html="md(req.suggestions)"></div>
      </section>

      <!-- answer form -->
      <section class="form">
        <div class="form-top">
          <span class="sec-h label amber">提交结论</span>
          <div class="tmpl">
            <span class="tmpl-lbl mono">模板</span>
            <button v-for="t in templates" :key="t.name" class="tmpl-btn" @click="insert(t.body)">{{ t.name }}</button>
          </div>
        </div>
        <p class="form-hint">粘贴 GPT Pro / Gemini Deep Think 的结论。按 <kbd>⌘/Ctrl</kbd>+<kbd>↵</kbd> 提交。</p>
        <textarea
          ref="ta"
          v-model="answer"
          class="field answer mono"
          rows="11"
          placeholder="## 方案结论&#10;&#10;推荐采用 …，理由 …"
          @keydown="onKey"
        ></textarea>
        <div class="form-foot">
          <span class="meta mono">
            {{ answer.length }} 字
            <span v-if="draftSaved" class="saved">· 草稿已存</span>
          </span>
          <div class="foot-btns">
            <button v-if="answer" class="btn btn-sm btn-ghost" @click="clearDraft">清空</button>
            <button class="btn btn-amber" :disabled="!workflow.ready || submitting" @click="submit">
              <Icon name="check" :size="14" /> {{ submitting ? '提交中…' : '提交并恢复运行' }}
            </button>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<script>
import Icon from './Icon.vue'
import { Projects } from '../lib/api.js'
import { renderMarkdown } from '../lib/markdown.js'
import { buildConsultationWorkflow } from '../lib/workspaceUi.js'
import { useToasts } from '../composables/useToasts.js'

export default {
  name: 'ConsultationPanel',
  components: { Icon },
  props: {
    base: { type: String, required: true },
    gate: { type: String, default: '' },
  },
  emits: ['open-file', 'answered'],
  setup() { return { toasts: useToasts() } },
  data() {
    return {
      req: null, loading: true, answer: '', submitting: false, draftSaved: false,
      templates: [
        { name: '方案', body: '## 方案结论\n\n推荐主模型：\n\n关键参数：\n\n理由：\n' },
        { name: '灵敏度', body: '\n## 灵敏度 / 鲁棒性\n\n- 扰动范围：±\n- 验证方式：\n' },
        { name: '取舍', body: '\n## 取舍说明\n\n- 备选：\n- 放弃原因：\n' },
      ],
    }
  },
  computed: {
    draftKey() { return `pf_draft_${this.base}_${this.gate}` },
    workflow() { return buildConsultationWorkflow(this.req || {}, this.answer) },
  },
  watch: {
    answer(v) {
      localStorage.setItem(this.draftKey, v)
      this.draftSaved = !!v
    },
  },
  mounted() {
    this.answer = localStorage.getItem(this.draftKey) || ''
    this.fetch()
  },
  methods: {
    md(t) { return renderMarkdown(t) },
    async fetch() {
      this.loading = true
      try { this.req = await Projects.consultation(this.base) }
      catch (e) { this.req = null }
      finally { this.loading = false }
    },
    guessType(f) {
      const e = f.split('.').pop().toLowerCase()
      return { md: 'markdown', csv: 'csv', json: 'json', py: 'code', png: 'image', jpg: 'image', pdf: 'pdf' }[e] || 'text'
    },
    guessIcon(f) {
      return { markdown: 'file-text', csv: 'table', json: 'code', code: 'code', image: 'image', pdf: 'file-text' }[this.guessType(f)] || 'file'
    },
    insert(body) {
      const ta = this.$refs.ta
      const pos = ta ? ta.selectionStart : this.answer.length
      this.answer = this.answer.slice(0, pos) + body + this.answer.slice(pos)
      this.$nextTick(() => ta && ta.focus())
    },
    onKey(e) {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); this.submit() }
    },
    clearDraft() {
      this.answer = ''
      localStorage.removeItem(this.draftKey)
      this.draftSaved = false
    },
    async submit() {
      if (!this.workflow.ready || this.submitting) return
      this.submitting = true
      try {
        await Projects.answer(this.base, this.answer)
        localStorage.removeItem(this.draftKey)
        this.toasts.success('结论已提交，项目将恢复运行', this.base)
        this.$emit('answered')
      } catch (e) {
        this.toasts.error(e.response?.data?.detail || '提交失败')
      } finally {
        this.submitting = false
      }
    },
  },
}
</script>

<style scoped>
.cons { border-color: var(--amber-line); box-shadow: 0 0 0 1px var(--amber-line), var(--shadow); overflow: hidden; }
.cons-head {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 14px 18px; background: var(--amber-dim); border-bottom: 1px solid var(--amber-line);
}
.ch-left { display: flex; align-items: center; gap: 12px; }
.ch-icon { width: 34px; height: 34px; border-radius: var(--r); display: flex; align-items: center; justify-content: center; background: var(--amber); color: var(--amber-ink); flex-shrink: 0; }
.ch-title { font-size: 15px; font-weight: 700; }
.ch-sub { display: flex; align-items: center; gap: 8px; font-size: 11px; color: var(--ink-2); margin-top: 3px; }
.ch-sub .dim { color: var(--ink-3); }
.ch-flag { color: var(--amber); }

.cons-state { padding: 40px; text-align: center; color: var(--ink-3); display: flex; justify-content: center; }
.cons-body { padding: 18px; }

.sec { margin-bottom: 18px; padding-bottom: 18px; border-bottom: 1px solid var(--line); }
.sec:last-child { border-bottom: none; margin-bottom: 0; padding-bottom: 0; }
.sec-h { display: block; margin-bottom: 9px; }
.label.amber { color: var(--amber); }
.sec.highlight { background: var(--amber-dim); border: 1px solid var(--amber-line); border-radius: var(--r); padding: 16px; }
.workflow { padding: 14px; border: 1px solid var(--amber-line); border-radius: var(--r); background: var(--amber-dim); }
.flow-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 10px; }
.flow-state { font-size: 11px; color: var(--amber); }
.flow-state.ready { color: var(--ok); }
.flow-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.flow-check { display: flex; align-items: center; gap: 7px; padding: 8px 10px; border: 1px solid var(--line); border-radius: var(--r-sm); background: var(--panel); color: var(--ink-3); font-size: 12px; }
.flow-check.ok { color: var(--ok); border-color: var(--ok-dim); background: var(--ok-dim); }
.evidence-strip { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 10px; }
.evidence-chip { display: inline-flex; align-items: center; gap: 6px; padding: 6px 9px; border: 1px solid var(--line); border-radius: var(--r-sm); background: var(--panel-2); color: var(--ink-2); cursor: pointer; font-size: 12px; }
.evidence-chip:hover { color: var(--live); border-color: var(--live); }

.kf { display: flex; flex-direction: column; gap: 6px; }
.kf-chip { display: inline-flex; align-items: center; gap: 8px; padding: 8px 11px; background: var(--panel-2); border: 1px solid var(--line); border-left: 2px solid var(--live); border-radius: var(--r-sm); color: var(--ink-2); cursor: pointer; font-size: 12px; text-align: left; transition: all 0.14s var(--ease); }
.kf-chip:hover { background: var(--panel-3); color: var(--live); }

.form-top { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 6px; flex-wrap: wrap; }
.tmpl { display: flex; align-items: center; gap: 6px; }
.tmpl-lbl { font-size: 10px; color: var(--ink-3); letter-spacing: 0.12em; text-transform: uppercase; }
.tmpl-btn { padding: 4px 9px; background: var(--panel-2); border: 1px solid var(--line); border-radius: var(--r-xs); color: var(--ink-2); font-size: 11px; cursor: pointer; }
.tmpl-btn:hover { background: var(--panel-3); color: var(--ink); border-color: var(--live); }
.form-hint { font-size: 12px; color: var(--ink-3); margin: 4px 0 10px; }
.form-hint kbd { font-family: var(--mono); font-size: 10px; padding: 1px 5px; background: var(--panel-2); border: 1px solid var(--line-2); border-radius: var(--r-xs); }
.answer { font-size: 13px; }
.form-foot { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 12px; flex-wrap: wrap; }
.meta { font-size: 11px; color: var(--ink-3); }
.saved { color: var(--ok); }
.foot-btns { display: flex; gap: 8px; }

.tag { display: inline-flex; align-items: center; gap: 5px; font: 600 10.5px/1 var(--mono); letter-spacing: 0.06em; text-transform: uppercase; padding: 4px 8px; border-radius: var(--r-xs); }
.tag-amber { color: var(--amber); border: 1px solid var(--amber-line); background: var(--amber-dim); }

/* markdown */
.md { font-size: 13px; line-height: 1.7; color: var(--ink); }
.md :deep(.md-h) { font-weight: 700; margin: 1em 0 0.5em; }
.md :deep(.md-h2) { font-size: 1.15em; }
.md :deep(.md-h3) { font-size: 1.05em; }
.md :deep(p) { margin: 0.55em 0; }
.md :deep(ul), .md :deep(ol) { margin: 0.55em 0; padding-left: 1.4em; }
.md :deep(li) { margin: 0.25em 0; }
.md :deep(.md-code) { font-family: var(--mono); font-size: 0.88em; background: var(--panel); border: 1px solid var(--line); padding: 0.1em 0.4em; border-radius: var(--r-xs); color: var(--live); }
.md :deep(.md-pre) { background: var(--panel); border: 1px solid var(--line); border-radius: var(--r); padding: 12px 14px; overflow-x: auto; margin: 0.8em 0; }
.md :deep(.md-pre code) { font-family: var(--mono); font-size: 12px; color: var(--ink-2); }
.md :deep(.md-table) { border-collapse: collapse; margin: 0.8em 0; font-size: 0.9em; }
.md :deep(.md-table th), .md :deep(.md-table td) { border: 1px solid var(--line); padding: 6px 10px; }
.md :deep(.md-table th) { background: var(--panel); }

/* AI tools section */
.ai-tools { background: var(--panel-2); border: 1px solid var(--line-2); border-radius: var(--r); padding: 16px; }
.tools-grid { display: grid; gap: 10px; margin-bottom: 12px; }
.tool-card { display: flex; align-items: center; gap: 12px; padding: 12px 14px; background: var(--panel); border: 1px solid var(--line); border-radius: var(--r); text-decoration: none; color: var(--ink); transition: all 0.2s var(--ease); cursor: pointer; }
.tool-card:hover { background: var(--panel-3); border-color: var(--live); transform: translateY(-1px); box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
.tool-icon { width: 36px; height: 36px; border-radius: var(--r); display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
.tool-icon.gpt { background: linear-gradient(135deg, #10a37f, #1a7f64); color: white; }
.tool-icon.gemini { background: linear-gradient(135deg, #4285f4, #34a853); color: white; }
.tool-icon.claude { background: linear-gradient(135deg, #cc785c, #d4a574); color: white; }
.tool-info { flex: 1; min-width: 0; }
.tool-name { font-size: 13px; font-weight: 600; color: var(--ink); }
.tool-desc { font-size: 11px; color: var(--ink-3); margin-top: 2px; }
.tool-ext { color: var(--ink-3); flex-shrink: 0; }
.tools-hint { display: flex; align-items: flex-start; gap: 8px; padding: 10px; background: var(--amber-dim); border: 1px solid var(--amber-line); border-radius: var(--r-sm); font-size: 12px; line-height: 1.5; color: var(--ink-2); }
.tools-hint svg { flex-shrink: 0; margin-top: 2px; color: var(--amber); }
</style>
