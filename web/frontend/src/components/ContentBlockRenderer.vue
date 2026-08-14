<template>
  <div class="cb-root">
    <section v-for="block in normalized" :key="block.id" class="cb-block" :data-render-type="block.render_type">
      <div class="cb-label">{{ block.label }}</div>

      <div v-if="blockWidget(block) === 'notice'" class="cb-notice" :class="`level-${block.content?.level || 'info'}`">
        <Icon :name="block.content?.level === 'error' ? 'alert-triangle' : 'clock'" :size="15" />
        <span>{{ block.content?.message || '暂无内容' }}</span>
      </div>

      <dl v-else-if="blockWidget(block) === 'key-value'" class="cb-kv">
        <template v-for="(value, key) in objectContent(block)" :key="key">
          <dt>{{ key }}</dt><dd>{{ value }}</dd>
        </template>
      </dl>

      <div v-else-if="blockWidget(block) === 'method-cards'" class="cb-method-grid">
        <article v-for="item in listContent(block)" :key="item.id" class="cb-method" :class="{ selected: selectedId === item.id }">
          <div class="cb-method-top">
            <span class="cb-rank mono">#{{ item.rank }}</span>
            <span class="cb-source">{{ item.source || '方法库' }}</span>
          </div>
          <h3>{{ item.title }}</h3>
          <div class="cb-path mono">{{ hierarchyLabel(item) }}</div>
          <div class="cb-badges">
            <span>证据 {{ evidenceLabel(item.evidence_level) }}</span>
            <span>数据 {{ percent(item.data_coverage) }}</span>
            <span>样本 {{ item.historical_samples || 0 }}</span>
          </div>
          <p>{{ item.rationale }}</p>
          <div class="cb-risk">{{ riskText(item) }}</div>
          <button
            v-for="action in item.actions || []"
            :key="action.id"
            class="btn btn-sm"
            :class="action.style === 'primary' ? 'btn-amber' : 'btn-ghost'"
            :disabled="busyId === item.id || (selectedId === item.id && action.id === 'select_modeling_direction')"
            @click="$emit('action', action, item, block)"
          >
            <span v-if="busyId === item.id" class="spinner sm"></span>
            <template v-else><Icon :name="selectedId === item.id ? 'check' : 'check-circle'" :size="13" /> {{ action.label }}</template>
          </button>
        </article>
      </div>

      <div v-else-if="blockWidget(block) === 'dag'" class="cb-dag">
        <ol class="cb-node-list">
          <li v-for="node in dagNodes(block)" :key="node.id" class="cb-node">
            <div class="cb-node-order mono">{{ node.order }}</div>
            <div class="cb-node-body">
              <div class="cb-node-head"><strong>{{ node.title }}</strong><span>{{ node.phase_label }}</span></div>
              <p>{{ node.objective }}</p>
              <div v-if="node.method_candidates?.length" class="cb-node-method mono">{{ node.method_candidates.join(' · ') }}</div>
            </div>
          </li>
        </ol>
        <div v-if="dagEdges(block).length" class="cb-edge-list">
          <div class="cb-edge-title">依赖边</div>
          <div v-for="(edge, index) in dagEdges(block)" :key="`${edge.from}-${edge.to}-${index}`" class="cb-edge">
            <span>{{ edge.from_title || edge.from }}</span><Icon name="arrow-right" :size="12" /><span>{{ edge.to_title || edge.to }}</span><em>{{ edge.type }}</em>
          </div>
        </div>
      </div>

      <button v-else-if="blockWidget(block) === 'artifact-link'" class="cb-artifact" @click="openArtifact(block)">
        <Icon name="file-text" :size="15" />
        <span><strong>{{ block.content?.name || '打开产物' }}</strong><small>{{ block.content?.description || block.content?.path }}</small></span>
        <Icon name="arrow-right" :size="13" />
      </button>

      <div v-else-if="blockWidget(block) === 'markdown'" class="cb-markdown">{{ String(block.content || '') }}</div>
      <div v-else-if="blockWidget(block) === 'table'" class="cb-table-wrap">
        <table v-if="listContent(block).length" class="cb-table">
          <thead><tr><th v-for="column in tableColumns(block)" :key="column">{{ column }}</th></tr></thead>
          <tbody><tr v-for="(row, index) in listContent(block)" :key="index"><td v-for="column in tableColumns(block)" :key="column">{{ row?.[column] ?? '—' }}</td></tr></tbody>
        </table>
        <span v-else>暂无表格数据</span>
      </div>
      <pre v-else class="cb-unsupported">{{ pretty(block.content) }}</pre>

      <ContentBlockRenderer
        v-if="block.children.length"
        :blocks="block.children"
        :selected-id="selectedId"
        :busy-id="busyId"
        @action="(...args) => $emit('action', ...args)"
        @open-file="$emit('open-file', $event)"
      />
    </section>
  </div>
</template>

<script>
import { computed } from 'vue'
import Icon from './Icon.vue'
import { artifactRequestFromBlock, blockWidget, normalizeContentBlocks } from '../lib/contentBlocks.js'

export default {
  name: 'ContentBlockRenderer',
  components: { Icon },
  props: {
    blocks: { type: Array, default: () => [] },
    selectedId: { type: String, default: '' },
    busyId: { type: String, default: '' },
  },
  emits: ['action', 'open-file'],
  setup(props, { emit }) {
    const normalized = computed(() => normalizeContentBlocks(props.blocks))
    const objectContent = (block) => block.content && typeof block.content === 'object' && !Array.isArray(block.content) ? block.content : {}
    const listContent = (block) => Array.isArray(block.content) ? block.content : []
    const dagNodes = (block) => Array.isArray(block.content?.nodes) ? [...block.content.nodes].sort((a, b) => Number(a.order || 0) - Number(b.order || 0)) : []
    const dagEdges = (block) => Array.isArray(block.content?.edges) ? block.content.edges : []
    const tableColumns = (block) => {
      const rows = listContent(block)
      return rows.length && rows[0] && typeof rows[0] === 'object' ? Object.keys(rows[0]) : []
    }
    const hierarchyLabel = (item) => Array.isArray(item.hierarchy) && item.hierarchy.length ? item.hierarchy.join(' → ') : `${item.domain || '未分类'} → ${item.subdomain || item.method}`
    const evidenceLabel = (level) => ({ strong: '强', moderate: '中', weak: '弱', none: '无' })[level] || '无'
    const percent = (value) => Number.isFinite(Number(value)) ? `${Math.round(Number(value) * 100)}%` : '0%'
    const riskText = (item) => Array.isArray(item.risks) && item.risks.length ? `风险：${item.risks.join(' / ')}` : '风险：无硬阻塞'
    const pretty = (value) => JSON.stringify(value, null, 2)
    function openArtifact(block) {
      const request = artifactRequestFromBlock(block)
      if (request) emit('open-file', request)
    }
    return { normalized, blockWidget, objectContent, listContent, dagNodes, dagEdges, tableColumns, hierarchyLabel, evidenceLabel, percent, riskText, pretty, openArtifact }
  },
}
</script>

<style scoped>
.cb-root { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.cb-block { display: flex; flex-direction: column; gap: 9px; min-width: 0; }
.cb-label { color: var(--ink-3); font-size: 10px; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }
.cb-notice { display: flex; align-items: center; gap: 8px; min-height: 44px; padding: 10px 12px; border: 1px dashed var(--line-2); border-radius: var(--r); color: var(--ink-3); }
.cb-notice.level-error { border-style: solid; border-color: var(--bad); color: var(--bad); background: var(--bad-dim); }
.cb-kv { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin: 0; border: 1px solid var(--line); border-radius: var(--r); overflow: hidden; }
.cb-kv dt, .cb-kv dd { margin: 0; padding: 8px 10px; border-right: 1px solid var(--line); }
.cb-kv dt { color: var(--ink-3); font-size: 10px; background: var(--panel-3); }
.cb-kv dd { color: var(--ink-1); font-size: 11.5px; overflow-wrap: anywhere; }
.cb-method-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.cb-method { display: flex; flex-direction: column; gap: 8px; min-height: 265px; padding: 12px; border: 1px solid var(--line); border-radius: var(--r); background: var(--panel-2); }
.cb-method.selected { border-color: var(--ok); background: var(--ok-dim); }
.cb-method-top, .cb-node-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.cb-rank { color: var(--amber); font-weight: 900; }
.cb-source { color: var(--ink-3); font-size: 9.5px; text-align: right; }
.cb-method h3 { margin: 0; font-size: 14px; }
.cb-path, .cb-risk, .cb-node-method { color: var(--ink-3); font-size: 10px; overflow-wrap: anywhere; }
.cb-badges { display: flex; flex-wrap: wrap; gap: 5px; }
.cb-badges span { padding: 3px 6px; border: 1px solid var(--line); border-radius: var(--r-sm); color: var(--ink-2); font-size: 10px; }
.cb-method p { flex: 1; margin: 0; color: var(--ink-2); font-size: 11.5px; line-height: 1.45; }
.cb-dag { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(260px, .75fr); gap: 12px; }
.cb-node-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin: 0; padding: 0; list-style: none; }
.cb-node { display: flex; gap: 9px; padding: 10px; border: 1px solid var(--line); border-radius: var(--r); background: var(--panel-2); }
.cb-node-order { display: grid; place-items: center; flex: 0 0 26px; height: 26px; border-radius: 50%; background: var(--amber-dim); color: var(--amber); font-weight: 900; }
.cb-node-body { min-width: 0; }
.cb-node-head strong { font-size: 12px; }
.cb-node-head span { flex: none; color: var(--amber); font-size: 9px; }
.cb-node p { margin: 5px 0; color: var(--ink-2); font-size: 10.5px; line-height: 1.4; }
.cb-edge-list { padding: 10px; border: 1px solid var(--line); border-radius: var(--r); background: var(--panel-2); }
.cb-edge-title { margin-bottom: 7px; font-size: 11px; font-weight: 800; }
.cb-edge { display: grid; grid-template-columns: minmax(0, 1fr) 14px minmax(0, 1fr); align-items: center; gap: 4px; padding: 6px 0; border-bottom: 1px solid var(--line); color: var(--ink-2); font-size: 10px; }
.cb-edge:last-child { border-bottom: 0; }
.cb-edge em { grid-column: 1 / -1; color: var(--ink-3); font-size: 9px; font-style: normal; }
.cb-artifact { display: flex; align-items: center; gap: 9px; width: 100%; padding: 10px 12px; border: 1px solid var(--line); border-radius: var(--r); background: var(--panel-2); color: var(--ink-1); text-align: left; cursor: pointer; }
.cb-artifact span { display: flex; flex: 1; flex-direction: column; gap: 2px; }
.cb-artifact small { color: var(--ink-3); }
.cb-table-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: var(--r); }
.cb-table { width: 100%; border-collapse: collapse; font-size: 11px; }
.cb-table th, .cb-table td { padding: 8px 10px; border-bottom: 1px solid var(--line); text-align: left; }
.cb-table th { color: var(--ink-3); background: var(--panel-3); }
.cb-unsupported { max-height: 260px; overflow: auto; padding: 10px; border: 1px solid var(--line); border-radius: var(--r); color: var(--ink-2); background: var(--panel-3); }
.spinner.sm { width: 13px; height: 13px; border-top-color: currentColor; }
@media (max-width: 980px) {
  .cb-kv, .cb-method-grid, .cb-node-list, .cb-dag { grid-template-columns: 1fr; }
  .cb-method { min-height: auto; }
}
</style>
