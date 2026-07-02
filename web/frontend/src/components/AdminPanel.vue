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
        </template>
      </div>
    </div>
  </div>
</template>

<script>
import Icon from './Icon.vue'
import { AdminUsers, ProjectRequests } from '../lib/api.js'

export default {
  name: 'AdminPanel',
  components: { Icon },
  emits: ['close', 'changed'],
  data() {
    return { users: [], requests: [], loading: false, busyUser: null, busyRequest: null, error: '' }
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
  },
  mounted() { this.load() },
  methods: {
    async load() {
      this.loading = true
      this.error = ''
      try {
        const [users, requests] = await Promise.all([AdminUsers.list(), ProjectRequests.list()])
        this.users = users
        this.requests = requests
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
  },
}
</script>

<style scoped>
.ov { position: fixed; inset: 0; z-index: 300; background: rgba(0, 0, 0, 0.5); backdrop-filter: blur(4px); display: flex; align-items: center; justify-content: center; padding: 20px; }
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
.main { min-width: 0; display: flex; flex-direction: column; gap: 5px; }
.line { display: flex; align-items: center; gap: 8px; min-width: 0; flex-wrap: wrap; }
.name { font-size: 13px; font-weight: 700; overflow-wrap: anywhere; }
.meta, .note { font-size: 12px; color: var(--ink-3); overflow-wrap: anywhere; }
.note { color: var(--ink-2); }
.actions { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
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
}
</style>
