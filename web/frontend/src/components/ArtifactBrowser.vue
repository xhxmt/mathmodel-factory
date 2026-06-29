<template>
  <div class="ab">
    <!-- file tree -->
    <aside class="ab-tree">
      <div class="ab-tree-head">
        <span class="label">产物 · ARTIFACTS</span>
        <span class="count mono">{{ files.length }}</span>
      </div>
      <div class="ab-tree-body">
        <button v-if="hasPaper" class="paper-btn" :class="{ active: current && current.__paper }" @click="openPaper">
          <Icon name="book-open" :size="14" />
          <span>查看论文 PDF</span>
        </button>

        <div v-if="loading && !files.length" class="tree-empty">加载产物…</div>
        <div v-else-if="!files.length" class="tree-empty">暂无产物文件</div>

        <div v-if="keyArtifacts.length" class="pinned">
          <div class="pinned-h">
            <Icon name="pin" :size="12" />
            <span>关键产物</span>
          </div>
          <button
            v-for="f in keyArtifacts"
            :key="'pinned-' + f.path"
            class="fitem pinitem"
            :class="{ active: current && current.path === f.path }"
            :title="f.path"
            @click="openFile(f)"
          >
            <Icon :name="iconFor(f.type)" :size="13" class="fic" />
            <span class="fname">{{ f.name }}</span>
          </button>
        </div>

        <div v-for="g in grouped" :key="g.key" class="grp">
          <button class="grp-head" @click="toggle(g.key)">
            <Icon :name="expanded.has(g.key) ? 'chevron-down' : 'chevron-right'" :size="13" />
            <Icon :name="g.icon" :size="13" class="grp-ic" />
            <span class="grp-name">{{ g.label }}</span>
            <span class="grp-n mono">{{ g.files.length }}</span>
          </button>
          <div v-show="expanded.has(g.key)" class="grp-files">
            <button
              v-for="f in g.files"
              :key="f.path"
              class="fitem"
              :class="{ active: current && current.path === f.path }"
              :title="f.path"
              @click="openFile(f)"
            >
              <Icon :name="iconFor(f.type)" :size="13" class="fic" />
              <span class="fname">{{ f.name }}</span>
            </button>
          </div>
        </div>
      </div>
    </aside>

    <!-- viewer -->
    <section class="ab-view">
      <div v-if="current" class="view-head">
        <div class="vh-left">
          <Icon :name="current.__paper ? 'book-open' : iconFor(current.type)" :size="14" />
          <span class="vh-name mono">{{ current.path || current.name }}</span>
          <span v-if="current.size != null" class="vh-size mono">{{ fmtBytes(current.size) }}</span>
        </div>
        <div class="vh-right">
          <button v-if="isText" class="btn btn-sm btn-ghost" @click="copyContent"><Icon name="copy" :size="12" /> 复制</button>
          <button class="btn btn-sm btn-ghost" @click="download"><Icon name="download" :size="12" /> 下载</button>
        </div>
      </div>

      <div class="view-body" :class="{ pad: isText }">
        <div v-if="vLoading" class="view-state"><div class="spinner"></div></div>
        <div v-else-if="vError" class="view-state err">{{ vError }}</div>

        <template v-else-if="current">
          <iframe v-if="current.type === 'pdf'" :src="blobUrl" class="pdf" title="PDF"></iframe>
          <div v-else-if="current.type === 'image'" class="imgwrap"><img :src="blobUrl" :alt="current.name" /></div>
          <div v-else-if="current.type === 'markdown'" class="md" v-html="rendered"></div>
          <div v-else-if="current.type === 'csv'" class="csvwrap">
            <table class="csvt mono">
              <thead><tr><th v-for="(h, i) in csv.head" :key="i">{{ h }}</th></tr></thead>
              <tbody>
                <tr v-for="(r, ri) in csv.rows" :key="ri"><td v-for="(c, ci) in r" :key="ci">{{ c }}</td></tr>
              </tbody>
            </table>
            <div v-if="csv.truncated" class="csv-trunc">仅显示前 {{ csv.rows.length }} 行</div>
          </div>
          <pre v-else class="codeview mono"><code>{{ content }}</code></pre>
        </template>

        <div v-else class="view-empty">
          <Icon name="folder" :size="34" />
          <p>从左侧选择产物查看</p>
          <span class="hint">模型 · 结果 · 图表 · 评审 · 论文</span>
        </div>
      </div>
    </section>
  </div>
</template>

<script>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import Icon from './Icon.vue'
import { Projects, fetchBlobUrl, formatBytes } from '../lib/api.js'
import { renderMarkdown } from '../lib/markdown.js'
import { priorityArtifacts } from '../lib/workspaceUi.js'
import { useToasts } from '../composables/useToasts.js'

const GROUP_META = {
  problem: { label: '赛题解析', icon: 'file-text' },
  method: { label: '方法 · 提案', icon: 'layers' },
  model: { label: '模型', icon: 'beaker' },
  solve: { label: '求解 · 灵敏度', icon: 'cpu' },
  results: { label: '结果数据', icon: 'table' },
  figures: { label: '图表', icon: 'image' },
  evaluation: { label: '评估 · 评审', icon: 'check-circle' },
  paper: { label: '论文', icon: 'book-open' },
  diagnostics: { label: '诊断 · DIAGNOSTICS', icon: 'alert-triangle' },
  code: { label: '模型代码', icon: 'code' },
}
const GROUP_ORDER = ['problem', 'method', 'model', 'solve', 'results', 'figures', 'evaluation', 'paper', 'diagnostics', 'code']

export default {
  name: 'ArtifactBrowser',
  components: { Icon },
  props: {
    base: { type: String, required: true },
    requested: { type: Object, default: null },
    currentStep: { type: Number, default: -1 },
  },
  setup(props) {
    const toasts = useToasts()
    const files = ref([])
    const loading = ref(false)
    const current = ref(null)
    const content = ref('')
    const csv = ref({ head: [], rows: [], truncated: false })
    const blobUrl = ref('')
    const vLoading = ref(false)
    const vError = ref('')
    const expanded = ref(new Set())

    const grouped = computed(() => {
      const by = {}
      for (const f of files.value) (by[f.group] = by[f.group] || []).push(f)
      return GROUP_ORDER.filter((k) => by[k]).map((k) => ({
        key: k, label: GROUP_META[k]?.label || k, icon: GROUP_META[k]?.icon || 'folder', files: by[k],
      }))
    })
    const hasPaper = computed(() => files.value.some((f) => f.type === 'pdf' && f.group === 'paper'))
    const isText = computed(() => current.value && ['markdown', 'text', 'csv', 'json', 'code'].includes(current.value.type))
    const rendered = computed(() => current.value?.type === 'markdown' ? renderMarkdown(content.value) : '')
    const keyArtifacts = computed(() => priorityArtifacts(files.value, props.currentStep).slice(0, 6))

    function iconFor(t) {
      return { image: 'image', pdf: 'file-text', csv: 'table', json: 'code', code: 'code', markdown: 'file-text', text: 'file-text' }[t] || 'file'
    }

    function toggle(k) {
      const next = new Set(expanded.value)
      next.has(k) ? next.delete(k) : next.add(k)
      expanded.value = next
    }

    function revoke() {
      if (blobUrl.value) {
        URL.revokeObjectURL(blobUrl.value)
        blobUrl.value = ''
      }
    }

    function reset() {
      files.value = []
      current.value = null
      content.value = ''
      csv.value = { head: [], rows: [], truncated: false }
      vError.value = ''
      revoke()
    }

    async function loadFiles() {
      loading.value = true
      try {
        const d = await Projects.files(props.base)
        files.value = d.files || []
        // expand the first two non-empty groups by default
        expanded.value = new Set(grouped.value.slice(0, 2).map((g) => g.key))
        if (props.requested) handleRequest(props.requested)
      } catch (e) {
        toasts.error('加载产物列表失败')
      } finally {
        loading.value = false
      }
    }

    function handleRequest(req) {
      if (req.__paper) { openPaper(); return }
      // ensure its group is expanded if known
      const known = files.value.find((f) => f.path === req.path)
      if (known) {
        const next = new Set(expanded.value)
        next.add(known.group)
        expanded.value = next
        openFile(known)
      } else openFile(req)
    }

    async function openFile(f) {
      revoke()
      current.value = f
      vError.value = ''
      content.value = ''
      vLoading.value = true
      try {
        if (f.type === 'image' || f.type === 'pdf') {
          blobUrl.value = await fetchBlobUrl(Projects.rawUrl(props.base, f.path))
        } else {
          const d = await Projects.file(props.base, f.path)
          content.value = d.content || ''
          if (f.type === 'csv') parseCsv(content.value)
          if (f.type === 'json') content.value = prettyJson(content.value)
        }
      } catch (e) {
        vError.value = e.response?.data?.detail || '无法加载该文件'
      } finally {
        vLoading.value = false
      }
    }

    async function openPaper() {
      revoke()
      current.value = { __paper: true, name: '论文 PDF', path: '', type: 'pdf' }
      vError.value = ''
      vLoading.value = true
      try {
        blobUrl.value = await fetchBlobUrl(Projects.paperUrl(props.base))
      } catch (e) {
        vError.value = '论文 PDF 暂不可用'
      } finally {
        vLoading.value = false
      }
    }

    function parseCsv(text) {
      const lines = text.split('\n').filter((l) => l.length)
      const head = lines.length ? splitCsv(lines[0]) : []
      const max = 300
      const rows = lines.slice(1, max + 1).map((l) => splitCsv(l))
      csv.value = { head, rows, truncated: lines.length - 1 > max }
    }

    function splitCsv(line) {
      // naive split (sufficient for the simple numeric result CSVs)
      return line.split(',').map((s) => s.trim())
    }

    function prettyJson(text) {
      try { return JSON.stringify(JSON.parse(text), null, 2) } catch (e) { return text }
    }

    async function copyContent() {
      try { await navigator.clipboard.writeText(content.value); toasts.success('已复制') }
      catch (e) { toasts.error('复制失败') }
    }

    async function download() {
      const f = current.value
      if (!f) return
      try {
        let url = blobUrl.value
        let revokeAfter = false
        if (!url) { url = URL.createObjectURL(new Blob([content.value], { type: 'text/plain' })); revokeAfter = true }
        const a = document.createElement('a')
        a.href = url; a.download = f.name || 'file'; document.body.appendChild(a); a.click(); a.remove()
        if (revokeAfter) setTimeout(() => URL.revokeObjectURL(url), 1500)
      } catch (e) { toasts.error('下载失败') }
    }

    watch(() => props.base, () => { reset(); loadFiles() })
    watch(() => props.requested, (v) => { if (v) handleRequest(v) })
    onMounted(loadFiles)
    onBeforeUnmount(revoke)

    return {
      files,
      loading,
      current,
      content,
      csv,
      blobUrl,
      vLoading,
      vError,
      expanded,
      grouped,
      hasPaper,
      isText,
      rendered,
      keyArtifacts,
      fmtBytes: formatBytes,
      iconFor,
      toggle,
      reset,
      revoke,
      loadFiles,
      handleRequest,
      openFile,
      openPaper,
      parseCsv,
      splitCsv,
      prettyJson,
      copyContent,
      download,
    }
  },
}
</script>

<style scoped>
.ab { display: flex; min-height: 0; height: 100%; }

/* tree */
.ab-tree { width: 248px; flex-shrink: 0; border-right: 1px solid var(--line); display: flex; flex-direction: column; min-height: 0; background: var(--panel); }
.ab-tree-head { display: flex; align-items: center; justify-content: space-between; padding: 12px 14px; border-bottom: 1px solid var(--line); }
.count { font-size: 11px; color: var(--ink-3); }
.ab-tree-body { flex: 1; overflow-y: auto; padding: 8px; }

.paper-btn {
  width: 100%; display: flex; align-items: center; gap: 9px;
  padding: 10px 12px; margin-bottom: 8px;
  background: var(--amber-dim); border: 1px solid var(--amber-line); border-radius: var(--r);
  color: var(--amber); font: 600 12.5px/1 var(--sans); cursor: pointer;
  transition: all 0.15s var(--ease);
}
.paper-btn:hover { background: var(--amber); color: var(--amber-ink); }
.paper-btn.active { background: var(--amber); color: var(--amber-ink); }

.tree-empty { padding: 24px 12px; text-align: center; color: var(--ink-3); font-size: 12px; }

.pinned {
  margin: 0 0 10px;
  padding: 7px;
  border: 1px solid var(--amber-line);
  border-radius: var(--r);
  background: var(--amber-dim);
}
.pinned-h {
  display: flex; align-items: center; gap: 6px;
  margin: 0 0 6px;
  color: var(--amber);
  font: 700 10.5px/1 var(--mono);
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.pinitem {
  border-left-color: var(--amber-line);
  background: color-mix(in srgb, var(--panel) 74%, transparent);
}

.grp { margin-bottom: 2px; }
.grp-head { width: 100%; display: flex; align-items: center; gap: 7px; padding: 7px 8px; background: none; border: none; color: var(--ink-2); cursor: pointer; border-radius: var(--r-sm); font: 600 12px/1 var(--sans); }
.grp-head:hover { background: var(--panel-2); }
.grp-ic { color: var(--ink-3); }
.grp-name { flex: 1; text-align: left; }
.grp-n { font-size: 10px; color: var(--ink-3); }
.grp-files { padding-left: 8px; }

.fitem { width: 100%; display: flex; align-items: center; gap: 8px; padding: 6px 8px 6px 10px; background: none; border: none; border-left: 1.5px solid var(--line); color: var(--ink-2); cursor: pointer; font: 500 12px/1.3 var(--mono); transition: all 0.12s var(--ease); }
.fitem:hover { background: var(--panel-2); color: var(--ink); border-left-color: var(--ink-3); }
.fitem.active { background: var(--live-dim); color: var(--live); border-left-color: var(--live); }
.fic { flex-shrink: 0; opacity: 0.8; }
.fname { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* viewer */
.ab-view { flex: 1; display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.view-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 10px 14px; border-bottom: 1px solid var(--line); background: var(--panel-2); }
.vh-left { display: flex; align-items: center; gap: 9px; min-width: 0; color: var(--ink-2); }
.vh-name { font-size: 12px; color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.vh-size { font-size: 10.5px; color: var(--ink-3); flex-shrink: 0; }
.vh-right { display: flex; gap: 6px; flex-shrink: 0; }

.view-body { flex: 1; min-height: 0; overflow: auto; background: var(--bg); }
.view-body.pad { padding: 20px 24px; }
.view-state { display: flex; align-items: center; justify-content: center; height: 100%; min-height: 200px; color: var(--ink-3); }
.view-state.err { color: var(--bad); font-size: 13px; }

.pdf { width: 100%; height: 100%; min-height: 480px; border: none; background: #fff; }
.imgwrap { display: flex; align-items: center; justify-content: center; padding: 24px; min-height: 100%; }
.imgwrap img { max-width: 100%; max-height: 80vh; border-radius: var(--r); border: 1px solid var(--line); background: #fff; }

.codeview { font-size: 12.5px; line-height: 1.65; color: var(--ink-2); white-space: pre; }

.csvwrap { padding: 4px; }
.csvt { border-collapse: collapse; font-size: 12px; width: 100%; }
.csvt th, .csvt td { border: 1px solid var(--line); padding: 6px 10px; text-align: right; white-space: nowrap; }
.csvt th { background: var(--panel-2); color: var(--ink); position: sticky; top: 0; text-align: right; font-weight: 700; }
.csvt td { color: var(--ink-2); }
.csvt tbody tr:hover td { background: var(--panel); }
.csv-trunc { padding: 10px; color: var(--ink-3); font-size: 11px; text-align: center; }

.view-empty { display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100%; min-height: 260px; color: var(--ink-3); gap: 10px; }
.view-empty p { font-size: 14px; color: var(--ink-2); }
.view-empty .hint { font: 500 11px/1 var(--mono); letter-spacing: 0.08em; }

/* markdown (rendered via v-html) */
.md { font-size: 13.5px; line-height: 1.75; color: var(--ink); max-width: 880px; }
.md :deep(.md-h) { font-weight: 700; margin: 1.4em 0 0.6em; line-height: 1.3; }
.md :deep(.md-h1) { font-size: 1.5em; }
.md :deep(.md-h2) { font-size: 1.28em; padding-bottom: 0.3em; border-bottom: 1px solid var(--line); }
.md :deep(.md-h3) { font-size: 1.12em; color: var(--ink); }
.md :deep(.md-h4) { font-size: 1em; color: var(--ink-2); }
.md :deep(p) { margin: 0.7em 0; }
.md :deep(ul), .md :deep(ol) { margin: 0.7em 0; padding-left: 1.5em; }
.md :deep(li) { margin: 0.3em 0; }
.md :deep(.md-code) { font-family: var(--mono); font-size: 0.88em; background: var(--panel-2); border: 1px solid var(--line); padding: 0.1em 0.4em; border-radius: var(--r-xs); color: var(--live); }
.md :deep(.md-pre) { background: var(--panel); border: 1px solid var(--line); border-radius: var(--r); padding: 14px 16px; overflow-x: auto; margin: 1em 0; }
.md :deep(.md-pre code) { font-family: var(--mono); font-size: 12.5px; color: var(--ink-2); }
.md :deep(.md-quote) { border-left: 3px solid var(--amber); padding: 0.3em 1em; margin: 1em 0; color: var(--ink-2); background: var(--amber-dim); border-radius: 0 var(--r-sm) var(--r-sm) 0; }
.md :deep(.md-hr) { border: none; border-top: 1px solid var(--line); margin: 1.5em 0; }
.md :deep(.md-table) { border-collapse: collapse; margin: 1em 0; font-size: 0.92em; width: 100%; }
.md :deep(.md-table th), .md :deep(.md-table td) { border: 1px solid var(--line); padding: 7px 11px; text-align: left; }
.md :deep(.md-table th) { background: var(--panel-2); font-weight: 700; }
.md :deep(a) { color: var(--live); }

@media (max-width: 820px) {
  .ab { flex-direction: column; }
  .ab-tree { width: 100%; max-height: 200px; border-right: none; border-bottom: 1px solid var(--line); }
}
</style>
