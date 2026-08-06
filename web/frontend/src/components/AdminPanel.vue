<template>
  <div class="ov" @click.self="$emit('close')">
    <div class="modal panel rise">
      <div class="m-head">
        <div class="mh-l"><Icon name="shield" :size="16" /><span>管理员</span></div>
        <div class="mh-actions">
          <button class="btn btn-icon btn-ghost btn-sm" @click="load" :disabled="loading" title="刷新"><Icon name="refresh" :size="14" :class="{ spin: loading }" /></button>
          <button class="btn btn-icon btn-ghost btn-sm" @click="$emit('close')" title="关闭"><Icon name="x" :size="15" /></button>
        </div>
      </div>

      <div class="m-body">
        <div v-if="error" class="err"><Icon name="alert-triangle" :size="14" /> {{ error }}</div>
        <div v-if="loading" class="loading"><div class="spinner"></div></div>
        <template v-else>
          <section class="section">
            <div class="sec-h"><span class="label">用户审批</span><b class="mono">{{ orderedUsers.length }}</b></div>
            <div v-if="orderedUsers.length" class="rows">
              <div v-for="user in orderedUsers" :key="user.username" class="row">
                <div class="main">
                  <div class="line">
                    <span class="name mono">{{ user.username }}</span>
                    <span class="badge mono" :class="'st-' + user.status">{{ user.status }}</span>
                  </div>
                  <div class="meta">{{ user.display_name || '未设置显示名' }}</div>
                </div>
                <div class="actions">
                  <button v-if="user.status === 'pending'" class="btn btn-icon btn-sm btn-ghost ok" @click="approveUser(user)" :disabled="busyUser === user.username" title="批准"><Icon name="check" :size="13" /></button>
                  <button v-if="user.status === 'pending'" class="btn btn-icon btn-sm btn-ghost danger" @click="rejectUser(user)" :disabled="busyUser === user.username" title="拒绝"><Icon name="x" :size="13" /></button>
                  <button v-if="user.status === 'active' && user.username !== 'admin'" class="btn btn-icon btn-sm btn-ghost danger" @click="disableUser(user)" :disabled="busyUser === user.username" title="停用"><Icon name="stop" :size="13" /></button>
                  <button v-if="user.username !== 'admin'" class="btn btn-icon btn-sm btn-ghost danger" @click="deleteUser(user)" :disabled="busyUser === user.username" title="删除"><Icon name="trash-2" :size="13" /></button>
                </div>
              </div>
            </div>
            <div v-else class="empty-row">暂无用户</div>
          </section>

          <section class="section">
            <div class="sec-h"><span class="label">完成论文展示权限</span><b class="mono">{{ showcaseCandidates.length }}</b></div>
            <div class="showcase-config">
              <div class="audience-picker">
                <label for="showcase-audience">展示对象</label>
                <select id="showcase-audience" v-model="selectedAudience" class="audience-select mono">
                  <option v-for="audience in showcaseAudiences" :key="audience.id" :value="audience.id">
                    {{ audienceLabel(audience) }}
                  </option>
                </select>
                <span class="meta">注册用户会同时继承未登录访客可见的公共论文；这里为其增加专属论文。</span>
              </div>
              <div v-if="showcaseCandidates.length" class="rows showcase-rows">
                <label v-for="paper in showcaseCandidates" :key="paper.base_name" class="row showcase-row">
                  <div class="main">
                    <div class="line"><span class="name">{{ paper.title }}</span></div>
                    <div class="meta mono">{{ paper.base_name }}</div>
                  </div>
                  <input v-model="selectedProjectNames" type="checkbox" :value="paper.base_name" />
                </label>
              </div>
              <div v-else class="empty-row">complete/ 中暂无带最终 PDF 的项目</div>
              <div class="showcase-actions">
                <span class="meta mono">已选择 {{ selectedProjectNames.length }} 篇</span>
                <button class="btn btn-sm btn-amber" :disabled="busyShowcase || !selectedAudience || !showcaseDirty" @click="saveShowcase">
                  <Icon name="check" :size="13" /> 保存展示权限
                </button>
              </div>
            </div>
          </section>

          <section class="section">
            <div class="sec-h"><span class="label">项目审批</span><b class="mono">{{ orderedRequests.length }}</b></div>
            <div v-if="orderedRequests.length" class="rows">
              <div v-for="item in orderedRequests" :key="item.id" class="row">
                <div class="main">
                  <div class="line">
                    <span class="name mono">{{ item.base_name }}</span>
                    <span class="badge mono" :class="'st-' + item.status">{{ item.status }}</span>
                  </div>
                  <div class="meta mono">#{{ item.id }} · {{ item.requester || 'unknown' }}</div>
                  <div v-if="item.failure_reason || item.decision_note" class="note">{{ item.failure_reason || item.decision_note }}</div>
                </div>
                <div class="actions">
                  <button v-if="item.status === 'pending'" class="btn btn-icon btn-sm btn-ghost ok" @click="approveRequest(item)" :disabled="busyRequest === item.id" title="批准"><Icon name="check" :size="13" /></button>
                  <button v-if="item.status === 'pending'" class="btn btn-icon btn-sm btn-ghost danger" @click="rejectRequest(item)" :disabled="busyRequest === item.id" title="拒绝"><Icon name="x" :size="13" /></button>
                </div>
              </div>
            </div>
            <div v-else class="empty-row">暂无项目申请</div>
          </section>

          <section class="section">
            <div class="sec-h">
              <span class="label">Secret Manager</span>
              <b class="mono">{{ secretHealthLabel }}</b>
            </div>
            <div v-if="ops" class="ops-box">
              <div class="ops-top">
                <div class="kv">
                  <span>Project</span>
                  <b class="mono">{{ ops.project_id || '未配置' }}</b>
                </div>
                <div class="kv">
                  <span>gcloud</span>
                  <b class="mono">{{ ops.gcloud_path || '未找到' }}</b>
                </div>
              </div>
              <div v-if="secretRows.length" class="rows">
                <div v-for="secret in secretRows" :key="secret.env" class="row row-tight">
                  <div class="main">
                    <div class="line">
                      <span class="name mono">{{ secret.env }}</span>
                      <span class="meta mono">{{ secret.secret }}</span>
                    </div>
                    <div v-if="secret.error" class="note">{{ secret.error }}</div>
                  </div>
                  <div class="actions">
                    <span class="badge mono" :class="secret.loaded ? 'st-active' : 'st-failed'">runtime</span>
                    <span class="badge mono" :class="secret.accessible ? 'st-active' : 'st-failed'">access</span>
                  </div>
                </div>
              </div>
              <div v-if="localConfigRows.length" class="env-files">
                <div v-for="file in localConfigRows" :key="file.path" class="env-row">
                  <div class="main">
                    <div class="line">
                      <span class="name mono">{{ file.path }}</span>
                      <span class="badge mono" :class="envFileClass(file)">{{ envFileStatus(file) }}</span>
                    </div>
                    <div class="meta">{{ envFileMeta(file) }}</div>
                  </div>
                </div>
              </div>
            </div>
            <div v-else class="empty-row">暂无运维状态</div>
          </section>

          <section class="section">
            <div class="sec-h"><span class="label">审计日志</span><b class="mono">{{ auditLog.length }}</b></div>
            <div v-if="auditLog.length" class="rows audit-rows">
              <div v-for="item in auditLog" :key="item.id" class="row">
                <div class="main">
                  <div class="line">
                    <span class="name mono">{{ item.action }}</span>
                    <span class="badge mono">{{ item.target_type }}</span>
                  </div>
                  <div class="meta mono">#{{ item.id }} · {{ item.actor }} · {{ item.target_id }} · {{ formatUnix(item.created_at) }}</div>
                  <div v-if="metadataSummary(item.metadata)" class="note">{{ metadataSummary(item.metadata) }}</div>
                </div>
              </div>
            </div>
            <div v-else class="empty-row">暂无审计记录</div>
          </section>
        </template>
      </div>
    </div>
  </div>
</template>

<script>
import Icon from './Icon.vue'
import { AdminOps, AdminShowcase, AdminUsers, ProjectRequests } from '../lib/api.js'

export default {
  name: 'AdminPanel',
  components: { Icon },
  emits: ['close', 'changed'],
  data() {
    return {
      users: [],
      requests: [],
      ops: null,
      auditLog: [],
      showcase: { candidates: [], audiences: [] },
      selectedAudience: 'guest',
      selectedProjectNames: [],
      loading: false,
      busyUser: null,
      busyRequest: null,
      busyShowcase: false,
      error: '',
    }
  },
  computed: {
    orderedUsers() {
      const rank = { pending: 0, active: 1, rejected: 2, disabled: 3 }
      return [...this.users].sort((a, b) => {
        const ar = rank[a.status] ?? 9
        const br = rank[b.status] ?? 9
        if (ar !== br) return ar - br
        return a.username.localeCompare(b.username)
      })
    },
    orderedRequests() {
      const rank = { pending: 0, failed: 1, approved: 2, rejected: 3 }
      return [...this.requests].sort((a, b) => {
        const ar = rank[a.status] ?? 9
        const br = rank[b.status] ?? 9
        if (ar !== br) return ar - br
        return (b.created_at || 0) - (a.created_at || 0)
      })
    },
    showcaseCandidates() {
      return Array.isArray(this.showcase?.candidates) ? this.showcase.candidates : []
    },
    showcaseAudiences() {
      return Array.isArray(this.showcase?.audiences) ? this.showcase.audiences : []
    },
    selectedAudienceRecord() {
      return this.showcaseAudiences.find((audience) => audience.id === this.selectedAudience) || null
    },
    showcaseDirty() {
      const saved = [...(this.selectedAudienceRecord?.base_names || [])].sort()
      const selected = [...this.selectedProjectNames].sort()
      return JSON.stringify(saved) !== JSON.stringify(selected)
    },
    secretRows() {
      return Array.isArray(this.ops?.secrets) ? this.ops.secrets : []
    },
    localConfigRows() {
      return Array.isArray(this.ops?.local_config) ? this.ops.local_config : []
    },
    secretHealthLabel() {
      const rows = this.secretRows
      if (!rows.length) return '0/0'
      const ok = rows.filter((row) => row.loaded && row.accessible).length
      return `${ok}/${rows.length}`
    },
  },
  watch: {
    selectedAudience() { this.syncShowcaseSelection() },
  },
  mounted() { this.load() },
  methods: {
    async load() {
      this.loading = true
      this.error = ''
      try {
        const [users, requests, ops, auditLog, showcase] = await Promise.all([
          AdminUsers.list(),
          ProjectRequests.list(),
          AdminOps.secrets(),
          AdminOps.auditLog(),
          AdminShowcase.get(),
        ])
        this.users = users
        this.requests = requests
        this.ops = ops
        this.auditLog = auditLog
        this.showcase = showcase || { candidates: [], audiences: [] }
        if (!this.showcaseAudiences.some((audience) => audience.id === this.selectedAudience)) {
          this.selectedAudience = this.showcaseAudiences[0]?.id || ''
        }
        this.syncShowcaseSelection()
      } catch (err) {
        this.error = err.response?.data?.detail || '加载失败'
      } finally {
        this.loading = false
      }
    },
    async approveUser(user) {
      this.busyUser = user.username
      await this.userAction(() => AdminUsers.approve(user.username))
    },
    async rejectUser(user) {
      this.busyUser = user.username
      await this.userAction(() => AdminUsers.reject(user.username, 'rejected by admin'))
    },
    async disableUser(user) {
      this.busyUser = user.username
      await this.userAction(() => AdminUsers.disable(user.username))
    },
    async deleteUser(user) {
      if (typeof window !== 'undefined' && !window.confirm(`确认删除用户 ${user.username}？`)) return
      this.busyUser = user.username
      await this.userAction(() => AdminUsers.delete(user.username))
    },
    async userAction(fn) {
      this.error = ''
      try {
        await fn()
        await this.load()
        this.$emit('changed')
      } catch (err) {
        this.error = err.response?.data?.detail || '用户操作失败'
      } finally {
        this.busyUser = null
      }
    },
    async approveRequest(item) {
      this.busyRequest = item.id
      await this.requestAction(() => ProjectRequests.approve(item.id, ''))
    },
    async rejectRequest(item) {
      this.busyRequest = item.id
      await this.requestAction(() => ProjectRequests.reject(item.id, 'rejected by admin'))
    },
    async requestAction(fn) {
      this.error = ''
      try {
        await fn()
        await this.load()
        this.$emit('changed')
      } catch (err) {
        this.error = err.response?.data?.detail || '项目操作失败'
      } finally {
        this.busyRequest = null
      }
    },
    audienceLabel(audience) {
      if (audience.kind === 'guest') return '默认未登录用户（公开）'
      const displayName = audience.display_name ? ` · ${audience.display_name}` : ''
      return `${audience.username}${displayName} [${audience.status}]`
    },
    syncShowcaseSelection() {
      this.selectedProjectNames = [...(this.selectedAudienceRecord?.base_names || [])]
    },
    async saveShowcase() {
      if (!this.selectedAudience) return
      this.busyShowcase = true
      this.error = ''
      try {
        await AdminShowcase.replace(this.selectedAudience, this.selectedProjectNames)
        await this.load()
        this.$emit('changed')
      } catch (err) {
        const detail = err.response?.data?.detail
        this.error = typeof detail === 'string' ? detail : detail?.code || '展示权限保存失败'
      } finally {
        this.busyShowcase = false
      }
    },
    envFileStatus(file) {
      if (!file?.exists) return 'missing'
      if ((file.sensitive_keys || []).length) return 'dirty'
      if (!file.secure_mode) return 'mode'
      return 'clean'
    },
    envFileClass(file) {
      return this.envFileStatus(file) === 'clean' || this.envFileStatus(file) === 'missing'
        ? 'st-active'
        : 'st-failed'
    },
    envFileMeta(file) {
      if (!file?.exists) return '文件不存在'
      const mode = file.mode ? `mode ${file.mode}` : 'mode unknown'
      const keys = Array.isArray(file.sensitive_keys) ? file.sensitive_keys : []
      if (keys.length) return `${mode} · 敏感键 ${keys.join(', ')}`
      return `${mode} · 无敏感键`
    },
    formatUnix(value) {
      const n = Number(value)
      if (!Number.isFinite(n) || n <= 0) return '—'
      return new Date(n * 1000).toLocaleString()
    },
    metadataSummary(metadata) {
      if (!metadata || typeof metadata !== 'object') return ''
      const parts = Object.entries(metadata).map(([key, value]) => {
        const shown = /secret|token|password|key/i.test(key) ? '<redacted>' : String(value)
        return `${key}=${shown}`
      })
      return parts.join(' · ').slice(0, 180)
    },
  },
}
</script>

<style scoped>
.ov { position: fixed; inset: 0; z-index: 300; background: var(--overlay); -webkit-backdrop-filter: blur(8px) saturate(140%); backdrop-filter: blur(8px) saturate(140%); display: flex; align-items: center; justify-content: center; padding: 20px; }
.modal { width: min(880px, 100%); max-height: 90vh; display: flex; flex-direction: column; box-shadow: var(--shadow-lg); overflow: hidden; }
.m-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 15px 18px; border-bottom: 1px solid var(--line); flex-shrink: 0; }
.mh-l, .mh-actions { display: flex; align-items: center; gap: 9px; }
.mh-l { font-size: 15px; font-weight: 700; }
.m-body { padding: 15px; overflow-y: auto; display: flex; flex-direction: column; gap: 16px; }
.section { display: flex; flex-direction: column; gap: 9px; }
.sec-h { display: flex; align-items: center; gap: 8px; }
.sec-h b { color: var(--ink); font-size: 11px; }
.rows { display: flex; flex-direction: column; border: 1px solid var(--line); border-radius: var(--r); overflow: hidden; }
.row { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 11px 13px; background: var(--panel-2); border-bottom: 1px solid var(--line); }
.row:last-child { border-bottom: none; }
.row-tight { padding-block: 9px; }
.main { min-width: 0; display: flex; flex-direction: column; gap: 5px; }
.line { display: flex; align-items: center; gap: 8px; min-width: 0; flex-wrap: wrap; }
.name { font-size: 13px; font-weight: 700; overflow-wrap: anywhere; }
.meta, .note { font-size: 12px; color: var(--ink-3); overflow-wrap: anywhere; }
.note { color: var(--ink-2); }
.actions { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
.ops-box { display: flex; flex-direction: column; gap: 9px; }
.ops-top { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.4fr); gap: 9px; }
.kv { min-width: 0; display: flex; flex-direction: column; gap: 5px; padding: 10px 12px; border: 1px solid var(--line); border-radius: var(--r-sm); background: var(--panel-2); }
.kv span { color: var(--ink-3); font-size: 11px; }
.kv b { font-size: 12px; overflow-wrap: anywhere; }
.env-files { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; }
.env-row { min-width: 0; padding: 10px 12px; border: 1px solid var(--line); border-radius: var(--r-sm); background: var(--panel-2); }
.audit-rows { max-height: 300px; overflow-y: auto; }
.showcase-config { display: flex; flex-direction: column; gap: 9px; }
.audience-picker { display: grid; grid-template-columns: auto minmax(220px, 1fr); align-items: center; gap: 8px 12px; padding: 11px 13px; border: 1px solid var(--line); border-radius: var(--r); background: var(--panel-2); }
.audience-picker label { font-size: 12px; color: var(--ink-2); }
.audience-picker .meta { grid-column: 1 / -1; }
.audience-select { width: 100%; min-width: 0; padding: 8px 10px; color: var(--ink); background: var(--panel); border: 1px solid var(--line-2); border-radius: var(--r-sm); }
.showcase-rows { max-height: 310px; overflow-y: auto; }
.showcase-row { cursor: pointer; }
.showcase-row input { width: 17px; height: 17px; accent-color: var(--amber); flex-shrink: 0; }
.showcase-actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.badge { font-size: 10px; line-height: 1; padding: 4px 7px; border: 1px solid var(--line); border-radius: var(--r-xs); color: var(--ink-2); background: var(--panel); }
.st-pending { color: var(--amber); border-color: var(--amber-line); background: var(--amber-dim); }
.st-active, .st-approved { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 45%, transparent); background: var(--ok-dim); }
.st-rejected, .st-disabled, .st-failed { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 45%, transparent); background: var(--bad-dim); }
.ok { color: var(--ok); }
.danger { color: var(--bad); }
.err { display: flex; align-items: center; gap: 8px; padding: 10px 12px; background: var(--bad-dim); border: 1px solid var(--bad); border-radius: var(--r-sm); color: var(--bad); font-size: 12.5px; }
.loading, .empty-row { min-height: 120px; display: flex; align-items: center; justify-content: center; color: var(--ink-3); }
.empty-row { border: 1px solid var(--line); border-radius: var(--r); background: var(--panel-2); font-size: 12.5px; }
.spin { animation: spin 0.7s linear infinite; }
@media (max-width: 640px) {
  .row { align-items: flex-start; }
  .ops-top, .env-files { grid-template-columns: 1fr; }
  .audience-picker { grid-template-columns: 1fr; }
}
</style>
