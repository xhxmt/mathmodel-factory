<template>
  <section class="evidence panel">
    <header>
      <div class="eyebrow">MACHINE EVIDENCE</div>
      <h2>证据驾驶舱</h2>
      <p>集中查看规范结果、求解回执、分阶段审计和三角色结论。</p>
    </header>

    <div class="method-strip">
      <div><span>PRIMARY</span><strong class="mono">{{ canonical.primary_method || '未声明' }}</strong></div>
      <div><span>AUXILIARY</span><strong class="mono">{{ canonical.auxiliary_method || 'NONE' }}</strong></div>
      <button v-if="canonical.path" class="btn btn-sm btn-ghost" @click="open(canonical.path)">查看 canonical results</button>
    </div>

    <div class="evidence-grid">
      <article class="e-card">
        <h3>核心结果</h3>
        <div v-if="canonical.items?.length" class="headline-list">
          <div v-for="item in canonical.items" :key="item.id">
            <span class="mono">{{ item.id }}</span>
            <strong>{{ item.headline == null ? item.status || '已记录' : `${item.headline_label}: ${item.headline}` }}</strong>
          </div>
        </div>
        <div v-else class="empty">尚未生成规范结果</div>
      </article>

      <article class="e-card">
        <h3>求解与回执</h3>
        <div class="big-number mono">{{ solver.receipt_ready || 0 }}<small>/ {{ solver.total || 0 }}</small></div>
        <p>提交与完成回执成对齐全</p>
        <div class="chips"><span v-for="(count, status) in solver.status_counts" :key="status">{{ status }} {{ count }}</span></div>
        <button class="btn btn-sm btn-ghost" @click="$emit('navigate', 'solver')">打开求解任务</button>
      </article>

      <article class="e-card">
        <h3>分阶段审计</h3>
        <button v-for="audit in audits" :key="audit.profile" class="audit-row" :disabled="!audit.path" @click="open(audit.path)">
          <span>{{ auditName(audit.profile) }}</span>
          <b :class="audit.delivery_allowed || audit.status === 'PASS' ? 'ok' : audit.available ? 'bad' : 'pending'">{{ audit.available ? audit.status : 'PENDING' }}</b>
        </button>
      </article>

      <article class="e-card">
        <h3>三角色 Judge</h3>
        <div v-for="role in ['math', 'execution', 'paper']" :key="role" class="role-row">
          <span>{{ roleName(role) }}</span>
          <b :class="roles[role] === 'PASS' ? 'ok' : roles[role] ? 'bad' : 'pending'">{{ roles[role] || 'PENDING' }}</b>
        </div>
      </article>
    </div>
  </section>
</template>

<script>
import { computed } from 'vue'
import Icon from './Icon.vue'

export default {
  name: 'EvidenceCockpit',
  components: { Icon },
  props: {
    evidence: { type: Object, default: () => ({}) },
    audits: { type: Array, default: () => [] },
  },
  emits: ['open-file', 'navigate'],
  setup(props, { emit }) {
    const canonical = computed(() => props.evidence?.canonical || {})
    const solver = computed(() => props.evidence?.solver || {})
    const roles = computed(() => props.evidence?.role_statuses || {})
    const open = (path) => path && emit('open-file', { path, name: path.split('/').pop(), type: 'json' })
    const auditName = (name) => ({ model: '模型审计', results: '结果审计', paper: '论文审计', final: 'Final Audit' }[name] || name)
    const roleName = (name) => ({ math: '数学正确性', execution: '执行与证据', paper: '论文表达' }[name] || name)
    return { canonical, solver, roles, open, auditName, roleName }
  },
}
</script>

<style scoped>
.evidence { padding: 18px; }
.eyebrow { color: var(--live); font: 800 9px var(--mono); letter-spacing: .16em; }
h2 { margin: 4px 0 6px; font-size: 20px; }
header p { margin: 0; color: var(--ink-2); font-size: 12px; }
.method-strip { display: grid; grid-template-columns: 1fr 1fr auto; gap: 10px; align-items: center; margin: 16px 0 10px; padding: 11px 12px; border: 1px solid var(--live-line); border-radius: var(--r); background: var(--live-dim); }
.method-strip div { display: flex; flex-direction: column; gap: 3px; }
.method-strip span { color: var(--live); font-size: 9px; letter-spacing: .1em; }
.method-strip strong { font-size: 12px; }
.evidence-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.e-card { padding: 13px; border: 1px solid var(--line); border-radius: var(--r); background: var(--panel-2); }
h3 { margin: 0 0 10px; font-size: 12px; }
.headline-list { display: grid; gap: 7px; }
.headline-list div, .role-row, .audit-row { display: flex; justify-content: space-between; gap: 10px; align-items: center; }
.headline-list div { padding-bottom: 6px; border-bottom: 1px solid var(--line); }
.headline-list span { color: var(--ink-3); font-size: 10px; }
.headline-list strong { font-size: 11px; text-align: right; }
.big-number { color: var(--live); font-size: 28px; font-weight: 900; }
.big-number small { color: var(--ink-3); font-size: 12px; }
.e-card p, .empty { color: var(--ink-3); font-size: 10px; }
.chips { display: flex; flex-wrap: wrap; gap: 5px; margin: 9px 0; }
.chips span { padding: 3px 6px; border-radius: 999px; background: var(--bg-2); color: var(--ink-2); font-size: 9px; }
.audit-row { width: 100%; padding: 7px 2px; border: 0; border-bottom: 1px solid var(--line); background: transparent; color: var(--ink); cursor: pointer; font: 11px var(--sans); }
.audit-row:disabled { cursor: default; }
.role-row { padding: 8px 2px; border-bottom: 1px solid var(--line); font-size: 11px; }
.ok { color: var(--ok); }
.bad { color: var(--bad); }
.pending { color: var(--amber); }
@media (max-width: 720px) {
  .method-strip, .evidence-grid { grid-template-columns: 1fr; }
}
</style>
