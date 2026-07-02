<template>
  <div class="ov" @click.self="$emit('close')">
    <div class="modal panel rise">
      <div class="m-head">
        <div class="mh-l"><Icon name="inbox" :size="16" /><span>{{ admin ? '项目审批' : '项目申请' }}</span></div>
        <div class="mh-actions">
          <button class="btn btn-icon btn-ghost btn-sm" @click="load" :disabled="loading" title="刷新"><Icon name="refresh" :size="14" :class="{ spin: loading }" /></button>
          <button class="btn btn-icon btn-ghost btn-sm" @click="$emit('close')" title="关闭"><Icon name="x" :size="15" /></button>
        </div>
      </div>

      <div class="m-body">
        <div v-if="error" class="err"><Icon name="alert-triangle" :size="14" /> {{ error }}</div>
        <div v-if="loading" class="loading"><div class="spinner"></div></div>
        <div v-else-if="orderedRequests.length" class="rows">
          <div v-for="item in orderedRequests" :key="item.id" class="req-row">
            <div class="req-main">
              <div class="req-id">
                <span class="req-name mono">{{ item.base_name }}</span>
                <span class="badge mono" :class="'st-' + item.status">{{ statusLabel(item.status) }}</span>
              </div>
              <div class="req-meta mono">#{{ item.id }} · {{ item.requester || 'unknown' }}</div>
              <div v-if="item.failure_reason || item.decision_note" class="req-note">{{ item.failure_reason || item.decision_note }}</div>
            </div>
            <div v-if="admin && item.status === 'pending'" class="req-actions">
              <button class="btn btn-icon btn-sm btn-ghost ok" @click="approve(item)" :disabled="busy === item.id" title="批准"><Icon name="check" :size="13" /></button>
              <button class="btn btn-icon btn-sm btn-ghost danger" @click="reject(item)" :disabled="busy === item.id" title="拒绝"><Icon name="x" :size="13" /></button>
            </div>
          </div>
        </div>
        <div v-else class="empty">
          <Icon name="inbox" :size="28" />
          <span>{{ admin ? '暂无项目申请' : '暂无申请记录' }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import Icon from './Icon.vue'
import { ProjectRequests } from '../lib/api.js'

export default {
  name: 'ProjectRequestsPanel',
  components: { Icon },
  props: {
    admin: { type: Boolean, default: false },
  },
  emits: ['close', 'changed'],
  data() {
    return { requests: [], loading: false, busy: null, error: '' }
  },
  computed: {
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
    statusLabel(status) {
      return { pending: 'pending', approved: 'approved', rejected: 'rejected', failed: 'failed' }[status] || status
    },
    async load() {
      this.loading = true
      this.error = ''
      try {
        this.requests = await ProjectRequests.list()
      } catch (err) {
        this.error = err.response?.data?.detail || '加载失败'
      } finally {
        this.loading = false
      }
    },
    async approve(item) {
      this.busy = item.id
      this.error = ''
      try {
        await ProjectRequests.approve(item.id, '')
        await this.load()
        this.$emit('changed')
      } catch (err) {
        this.error = err.response?.data?.detail || '审批失败'
      } finally {
        this.busy = null
      }
    },
    async reject(item) {
      this.busy = item.id
      this.error = ''
      try {
        await ProjectRequests.reject(item.id, 'rejected by admin')
        await this.load()
        this.$emit('changed')
      } catch (err) {
        this.error = err.response?.data?.detail || '拒绝失败'
      } finally {
        this.busy = null
      }
    },
  },
}
</script>

<style scoped>
.ov { position: fixed; inset: 0; z-index: 300; background: rgba(0, 0, 0, 0.5); backdrop-filter: blur(4px); display: flex; align-items: center; justify-content: center; padding: 20px; }
.modal { width: min(760px, 100%); max-height: 88vh; display: flex; flex-direction: column; box-shadow: var(--shadow-lg); overflow: hidden; }
.m-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 15px 18px; border-bottom: 1px solid var(--line); flex-shrink: 0; }
.mh-l, .mh-actions { display: flex; align-items: center; gap: 9px; }
.mh-l { font-size: 15px; font-weight: 700; }
.m-body { padding: 14px; overflow-y: auto; }
.rows { display: flex; flex-direction: column; border: 1px solid var(--line); border-radius: var(--r); overflow: hidden; }
.req-row { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 12px 14px; background: var(--panel-2); border-bottom: 1px solid var(--line); }
.req-row:last-child { border-bottom: none; }
.req-main { min-width: 0; display: flex; flex-direction: column; gap: 5px; }
.req-id { display: flex; align-items: center; gap: 8px; min-width: 0; flex-wrap: wrap; }
.req-name { font-size: 13px; font-weight: 700; overflow-wrap: anywhere; }
.req-meta { font-size: 11px; color: var(--ink-3); }
.req-note { font-size: 12px; color: var(--ink-2); overflow-wrap: anywhere; }
.req-actions { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
.badge { font-size: 10px; line-height: 1; padding: 4px 7px; border: 1px solid var(--line); border-radius: var(--r-xs); color: var(--ink-2); background: var(--panel); }
.st-pending { color: var(--amber); border-color: var(--amber-line); background: var(--amber-dim); }
.st-approved { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 45%, transparent); background: var(--ok-dim); }
.st-rejected, .st-failed { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 45%, transparent); background: var(--bad-dim); }
.ok { color: var(--ok); }
.danger { color: var(--bad); }
.err { display: flex; align-items: center; gap: 8px; padding: 10px 12px; margin-bottom: 12px; background: var(--bad-dim); border: 1px solid var(--bad); border-radius: var(--r-sm); color: var(--bad); font-size: 12.5px; }
.loading, .empty { min-height: 180px; display: flex; align-items: center; justify-content: center; gap: 10px; color: var(--ink-3); }
.spin { animation: spin 0.7s linear infinite; }
@media (max-width: 640px) {
  .req-row { align-items: flex-start; }
}
</style>
