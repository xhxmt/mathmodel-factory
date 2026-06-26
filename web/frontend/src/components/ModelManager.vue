<template>
  <div class="ov" @click.self="$emit('close')">
    <div class="modal panel rise">
      <!-- header -->
      <div class="m-head">
        <div class="mh-l"><Icon name="cpu" :size="17" /><span>模型管理 · MODELS</span></div>
        <div class="tabs">
          <button class="tab" :class="{ on: tab === 'registry' }" @click="tab = 'registry'">模型库</button>
          <button class="tab" :class="{ on: tab === 'default' }" @click="tab = 'default'">默认预设</button>
        </div>
        <button class="btn btn-icon btn-ghost btn-sm" @click="$emit('close')"><Icon name="x" :size="15" /></button>
      </div>

      <div v-if="loading" class="m-load"><span class="spinner"></span> 加载中…</div>

      <!-- ===================== model library ===================== -->
      <div v-else-if="tab === 'registry'" class="m-body">
        <p class="intro">
          注册可在各步骤选用的模型。<b>claude / codex / agy</b> 是 agentic 后端（可读写文件、跑求解器）；
          <b>openai / gemini / deepseek</b> 是 HTTP API（非 agentic），适合评委/评审/评价类步骤（7·11·13）。
          API 后端的密钥放在仓库根目录 <code>.env</code>（如 <code>DEEPSEEK_API_KEY</code>），此处只填写变量名。
        </p>

        <div class="cards">
          <div v-for="(m, i) in models" :key="i" class="mc" :class="{ off: !m.enabled }">
            <div class="mc-top">
              <span class="badge" :class="agentic(m.backend) ? 'ag' : 'api'">
                {{ agentic(m.backend) ? 'AGENTIC' : 'API' }}
              </span>
              <input v-model="m.label" class="field f-label" placeholder="显示名称" />
              <label class="sw" :title="m.enabled ? '已启用' : '已停用'">
                <input type="checkbox" v-model="m.enabled" /><span class="sw-t">{{ m.enabled ? '启用' : '停用' }}</span>
              </label>
              <button class="btn btn-icon btn-sm btn-danger" :disabled="m.builtin" :title="m.builtin ? '内置模型不可删除' : '删除'" @click="removeModel(i)">
                <Icon name="x" :size="13" />
              </button>
            </div>

            <div class="mc-grid">
              <label class="fld">
                <span class="fl">id</span>
                <input v-model="m.id" class="field mono" :readonly="m.builtin" placeholder="deepseek-chat" />
              </label>
              <label class="fld">
                <span class="fl">后端</span>
                <select v-model="m.backend" class="field">
                  <option v-for="b in validBackends" :key="b" :value="b">{{ b }}</option>
                </select>
              </label>
              <label class="fld">
                <span class="fl">model 名称</span>
                <input v-model="m.model" class="field mono" :placeholder="m.backend === 'claude' ? '(留空=CLI默认)' : 'deepseek-chat'" />
              </label>
              <label v-if="m.backend === 'codex' || m.backend === 'claude'" class="fld">
                <span class="fl">推理强度</span>
                <input v-model="m.effort" class="field mono" placeholder="xhigh / max" />
              </label>
              <label v-if="!agentic(m.backend)" class="fld wide">
                <span class="fl">base_url <span class="fl-hint">(openai 兼容端点)</span></span>
                <input v-model="m.base_url" class="field mono" placeholder="https://api.deepseek.com" />
              </label>
              <label v-if="!agentic(m.backend)" class="fld">
                <span class="fl">key_env <span class="fl-hint">(.env 变量名)</span></span>
                <input v-model="m.key_env" class="field mono" placeholder="DEEPSEEK_API_KEY" />
              </label>
            </div>
          </div>
        </div>

        <button class="btn btn-ghost add" @click="addModel"><Icon name="plus" :size="14" /> 添加模型</button>

        <div v-if="error" class="m-err"><Icon name="alert-triangle" :size="14" /> {{ error }}</div>
        <div class="m-foot">
          <button class="btn btn-ghost" @click="$emit('close')">关闭</button>
          <button class="btn btn-amber" :disabled="saving" @click="saveRegistry">
            <span v-if="saving" class="spinner sm"></span><template v-else><Icon name="check" :size="14" /> 保存模型库</template>
          </button>
        </div>
      </div>

      <!-- ===================== default preset ===================== -->
      <div v-else class="m-body">
        <p class="intro">
          为<b>所有新建项目</b>设置每一步的默认模型（单个项目可在其工作台内覆盖）。
          留空＝沿用内置默认链。仅 agentic 后端能完成建模/求解类步骤；API 后端仅在评委/评审/评价步（7·11·13）可用。
        </p>

        <div class="rows">
          <div v-for="s in overridableSteps" :key="s.key || s.index" class="row">
            <div class="row-id">
              <span class="ri-n mono">{{ s.key === '8_5' ? '8.5' : String(s.index).padStart(2, '0') }}</span>
              <div class="ri-tx">
                <div class="ri-name">{{ s.name }}<span v-if="meta(s.key || s.index).apiOk" class="api-ok" title="此步骤可用 API 模型">API✓</span></div>
                <div class="ri-def mono">内置: {{ meta(s.key || s.index).default }}</div>
              </div>
            </div>
            <div class="row-sel">
              <select class="field sel" :value="defGet(s, 'primary')" @change="defSet(s, 'primary', $event.target.value)">
                <option value="">— 主：内置默认 —</option>
                <option v-for="m in pickable(s)" :key="m.id" :value="m.id">{{ m.label }}</option>
              </select>
              <select class="field sel" :value="defGet(s, 'fallback')" @change="defSet(s, 'fallback', $event.target.value)">
                <option value="">— 备用：无 —</option>
                <option v-for="m in pickable(s)" :key="m.id" :value="m.id">{{ m.label }}</option>
              </select>
            </div>
          </div>
        </div>

        <div v-if="error" class="m-err"><Icon name="alert-triangle" :size="14" /> {{ error }}</div>
        <div class="m-foot">
          <button class="btn btn-ghost" @click="$emit('close')">关闭</button>
          <button class="btn btn-amber" :disabled="saving" @click="saveDefault">
            <span v-if="saving" class="spinner sm"></span><template v-else><Icon name="check" :size="14" /> 保存默认预设</template>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import Icon from './Icon.vue'
import { STEPS, EDITORIAL_GATE_STEP, stepModelMeta, stepConfigKey } from '../lib/steps.js'
import { useToasts } from '../composables/useToasts.js'
import { useModels } from '../composables/useModels.js'

export default {
  name: 'ModelManager',
  components: { Icon },
  emits: ['close', 'saved'],
  setup() {
    const m = useModels()
    return { toasts: useToasts(), _models: m.models, _loadModels: m.load, _saveRegistry: m.saveRegistry, _saveConfig: m.saveConfig }
  },
  data() {
    return {
      tab: 'registry', loading: true, saving: false, error: '',
      models: [], config: {}, defaultSteps: {},
      agenticBackends: ['claude', 'codex', 'agy'],
      validBackends: ['claude', 'codex', 'agy', 'openai', 'gemini', 'deepseek'],
    }
  },
  computed: {
    overridableSteps() {
      return [
        ...STEPS.filter((s) => stepModelMeta(s.index).overridable).slice(0, 9),
        EDITORIAL_GATE_STEP,
        ...STEPS.filter((s) => stepModelMeta(s.index).overridable).slice(9),
      ]
    },
    enabledModels() { return this.models.filter((m) => m.enabled && m.id) },
  },
  async mounted() { await this.load() },
  methods: {
    meta: stepModelMeta,
    agentic(b) { return this.agenticBackends.includes(b) },
    // Populate local form state from the shared models cache. Deep-clones the
    // registry/default preset because the template binds v-model directly to
    // these — binding to the singleton would mutate shared state across components.
    hydrate(d) {
      this.models = (d.registry || []).map((m) => ({ effort: '', model: '', base_url: '', key_env: '', builtin: false, ...m }))
      this.config = d.config || {}
      if (Array.isArray(d.agentic_backends) && d.agentic_backends.length) this.agenticBackends = d.agentic_backends
      if (Array.isArray(d.valid_backends) && d.valid_backends.length) this.validBackends = d.valid_backends
      this.defaultSteps = JSON.parse(JSON.stringify(this.config._default || {}))
    },
    async load() {
      this.loading = true
      try {
        await this._loadModels()
        this.hydrate(this._models)
      } catch (e) {
        this.error = e.response?.data?.detail || '加载模型配置失败'
      } finally {
        this.loading = false
      }
    },
    // models usable for a given step: API backends only where the step supports them
    pickable(step) {
      const apiOk = stepModelMeta(step.key || step.index).apiOk
      return this.enabledModels.filter((m) => apiOk || this.agentic(m.backend))
    },
    addModel() {
      this.models.push({ id: '', label: '新模型', backend: 'openai', model: '', effort: '', base_url: '', key_env: '', enabled: true, builtin: false })
    },
    removeModel(i) { if (!this.models[i].builtin) this.models.splice(i, 1) },
    async saveRegistry() {
      this.error = ''
      for (const m of this.models) {
        if (!m.id || !/^[a-zA-Z0-9_.-]+$/.test(m.id)) { this.error = `模型 id 非法或为空：${m.id || '(空)'}`; return }
        if (m.backend !== 'claude' && !m.model) { this.error = `模型 ${m.id} 需要填写 model 名称`; return }
      }
      this.saving = true
      try {
        await this._saveRegistry(this.models)
        this.toasts.success('模型库已保存')
        this.$emit('saved')
        this.hydrate(this._models)
      } catch (e) {
        this.error = e.response?.data?.detail || '保存失败'
      } finally {
        this.saving = false
      }
    },
    defGet(step, field) { return this.defaultSteps[stepConfigKey(step)]?.[field] || '' },
    defSet(step, field, val) {
      const key = stepConfigKey(step)
      const cur = { ...(this.defaultSteps[key] || {}) }
      cur[field] = val
      if (!cur.primary && !cur.fallback) delete this.defaultSteps[key]
      else this.defaultSteps[key] = cur
    },
    async saveDefault() {
      this.error = ''
      this.saving = true
      try {
        await this._saveConfig('_default', this.defaultSteps)
        this.toasts.success('默认预设已保存（应用于新建项目）')
        this.$emit('saved')
        this.hydrate(this._models)
      } catch (e) {
        this.error = e.response?.data?.detail || '保存失败'
      } finally {
        this.saving = false
      }
    },
  },
}
</script>

<style scoped>
.ov { position: fixed; inset: 0; z-index: 300; background: rgba(0,0,0,0.5); backdrop-filter: blur(4px); display: flex; align-items: center; justify-content: center; padding: 20px; }
.modal { width: 100%; max-width: 760px; max-height: 92vh; display: flex; flex-direction: column; box-shadow: var(--shadow-lg); }

.m-head { display: flex; align-items: center; gap: 14px; padding: 14px 18px; border-bottom: 1px solid var(--line); flex-shrink: 0; }
.mh-l { display: flex; align-items: center; gap: 9px; font-size: 15px; font-weight: 700; }
.tabs { display: flex; gap: 4px; margin-left: auto; padding: 3px; background: var(--bg-2); border: 1px solid var(--line); border-radius: var(--r); }
.tab { padding: 6px 13px; background: none; border: none; border-radius: var(--r-sm); color: var(--ink-3); font: 600 12.5px/1 var(--sans); cursor: pointer; }
.tab:hover { color: var(--ink); }
.tab.on { background: var(--panel-3); color: var(--ink); }

.m-load { padding: 50px; display: flex; align-items: center; justify-content: center; gap: 12px; color: var(--ink-2); }
.m-body { padding: 16px 18px; overflow-y: auto; display: flex; flex-direction: column; gap: 14px; }

.intro { font-size: 12px; line-height: 1.7; color: var(--ink-2); background: var(--panel-2); border: 1px solid var(--line); border-radius: var(--r); padding: 10px 12px; }
.intro code { font-family: var(--mono); font-size: 11px; color: var(--amber); }
.intro b { color: var(--ink); }

/* ---- registry cards ---- */
.cards { display: flex; flex-direction: column; gap: 10px; }
.mc { border: 1px solid var(--line-2); border-radius: var(--r); padding: 11px 12px; background: var(--panel-2); transition: opacity 0.15s var(--ease); }
.mc.off { opacity: 0.5; }
.mc-top { display: flex; align-items: center; gap: 9px; margin-bottom: 10px; }
.badge { font: 700 8.5px/1 var(--mono); letter-spacing: 0.08em; padding: 4px 6px; border-radius: var(--r-xs); flex-shrink: 0; }
.badge.ag { background: var(--live-dim); color: var(--live); }
.badge.api { background: var(--amber-dim); color: var(--amber); }
.f-label { flex: 1; padding: 7px 10px; font-size: 13px; font-weight: 600; }
.sw { display: flex; align-items: center; gap: 6px; cursor: pointer; font: 600 11px/1 var(--mono); color: var(--ink-2); white-space: nowrap; }
.sw input { accent-color: var(--ok); width: 15px; height: 15px; }

.mc-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 9px; }
.fld { display: flex; flex-direction: column; gap: 4px; }
.fld.wide { grid-column: span 2; }
.fl { font: 500 9.5px/1 var(--mono); letter-spacing: 0.1em; text-transform: uppercase; color: var(--ink-3); }
.fl-hint { text-transform: none; letter-spacing: 0; color: var(--ink-3); opacity: 0.7; }
.fld .field { padding: 8px 10px; font-size: 12.5px; }

.add { align-self: flex-start; }

/* ---- default preset rows ---- */
.rows { display: flex; flex-direction: column; gap: 7px; }
.row { display: flex; align-items: center; gap: 12px; padding: 9px 11px; border: 1px solid var(--line); border-radius: var(--r); background: var(--panel-2); }
.row-id { display: flex; align-items: center; gap: 11px; flex: 1; min-width: 0; }
.ri-n { font-size: 13px; font-weight: 700; color: var(--ink-3); width: 22px; text-align: center; }
.ri-tx { min-width: 0; }
.ri-name { font-size: 13px; font-weight: 600; display: flex; align-items: center; gap: 7px; }
.api-ok { font: 700 8px/1 var(--mono); color: var(--amber); background: var(--amber-dim); padding: 3px 5px; border-radius: var(--r-xs); }
.ri-def { font-size: 10.5px; color: var(--ink-3); margin-top: 2px; }
.row-sel { display: flex; gap: 7px; flex-shrink: 0; }
.sel { width: 178px; padding: 8px 10px; font-size: 12px; cursor: pointer; }

select.field { appearance: none; background-image: linear-gradient(45deg, transparent 50%, var(--ink-3) 50%), linear-gradient(135deg, var(--ink-3) 50%, transparent 50%); background-position: calc(100% - 14px) center, calc(100% - 9px) center; background-size: 5px 5px, 5px 5px; background-repeat: no-repeat; padding-right: 26px; }

.m-err { display: flex; align-items: center; gap: 8px; padding: 10px 12px; background: var(--bad-dim); border: 1px solid var(--bad); border-radius: var(--r-sm); color: var(--bad); font-size: 12.5px; }
.m-foot { display: flex; justify-content: flex-end; gap: 10px; padding-top: 4px; position: sticky; bottom: 0; background: var(--panel); padding-bottom: 2px; }
.spinner.sm { width: 15px; height: 15px; border-top-color: var(--amber-ink); }

@media (max-width: 680px) {
  .row { flex-direction: column; align-items: stretch; }
  .row-sel { flex-direction: column; }
  .sel { width: 100%; }
  .mc-grid { grid-template-columns: 1fr; }
  .fld.wide { grid-column: span 1; }
}
</style>
