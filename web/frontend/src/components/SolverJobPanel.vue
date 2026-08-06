<template>
  <div class="solver-panel panel">
    <div class="sp-head">
      <div class="sp-title">
        <Icon name="cpu" :size="15" />
        <span class="label">求解任务</span>
      </div>
      <button class="btn btn-sm btn-ghost" @click="refresh" :disabled="loading" title="刷新任务列表">
        <Icon name="refresh" :size="13" :class="{ spin: loading }" />
      </button>
    </div>

    <div v-if="loading && jobs.length === 0" class="sp-loading">
      <div class="spinner"></div>
    </div>

    <template v-else-if="jobs.length === 0">
      <div class="sp-empty empty panel">
        <Icon name="cpu" :size="32" />
        <p>暂无求解任务</p>
      </div>
    </template>

    <template v-else>
      <div class="sp-summary">
        <span class="sp-badge mono">总计 {{ summary.total }}</span>
        <span v-if="summary.running > 0" class="sp-badge mono sp-badge-running">运行中 {{ summary.running }}</span>
        <span v-if="summary.failed > 0" class="sp-badge mono sp-badge-failed">失败 {{ summary.failed }}</span>
      </div>

      <div class="sp-filters">
        <button
          v-for="filter in filters"
          :key="filter.key"
          class="sp-filter"
          :class="{ active: statusFilter === filter.key }"
          @click="statusFilter = filter.key"
        >
          {{ filter.label }}
        </button>
      </div>

      <div class="sp-list">
        <div v-for="job in filteredJobs" :key="job.job_id" class="sp-row">
          <div class="sp-main" @click="toggleExpand(job.job_id)">
            <span class="sp-jobid mono">{{ job.job_id.substring(0, 16) }}</span>
            <span class="sp-runtime-badge">{{ job.runtime }}</span>
            <span class="sp-backend-dot" :class="`backend-${job.backend}`" :title="job.backend"></span>
            <span class="sp-status" :class="`status-${job.status.toLowerCase()}`">{{ job.status }}</span>
            <span class="sp-duration mono">{{ formatDuration(job.duration_seconds) }}</span>
            <span class="sp-age">{{ relativeTime(job.requested_at) }}</span>
            <Icon
              :name="getReceiptIcon(job)"
              :size="14"
              class="sp-receipt-icon"
              :class="getReceiptClass(job)"
              :title="getReceiptTitle(job)"
            />
            <Icon name="chevron-right" :size="13" class="sp-expand-icon" :class="{ expanded: isExpanded(job.job_id) }" />
          </div>

          <div v-if="isExpanded(job.job_id)" class="sp-detail">
            <div v-if="loadingDetail[job.job_id]" class="sp-detail-loading">
              <div class="spinner"></div>
            </div>
            <div v-if="loadingDetail[job.job_id]" class="sp-detail-loading">
              <div class="spinner"></div>
            </div>
            <template v-else-if="evidence[job.job_id]">
              <div class="sp-detail-content">
                <div class="sp-detail-section">
                  <div class="sp-detail-label">Receipt 状态</div>
                  <div class="sp-detail-value">
                    <span v-if="evidence[job.job_id].receipt_ready" class="sp-receipt-badge sp-receipt-ready">
                      <Icon name="check-circle" :size="12" /> Ready
                    </span>
                    <span v-else class="sp-receipt-badge sp-receipt-not-ready">
                      <Icon name="alert-triangle" :size="12" /> {{ evidence[job.job_id].claim_limit || 'Not Ready' }}
                    </span>
                  </div>
                </div>

                <div v-if="evidence[job.job_id].errors && evidence[job.job_id].errors.length > 0" class="sp-detail-section">
                  <div class="sp-detail-label">Errors</div>
                  <div class="sp-detail-errors">
                    <div v-for="(err, idx) in evidence[job.job_id].errors" :key="idx" class="sp-error-item">{{ err }}</div>
                  </div>
                </div>

                <div v-if="evidence[job.job_id].submission" class="sp-detail-section">
                  <div class="sp-detail-label">Seeds</div>
                  <div class="sp-detail-value mono">{{ (evidence[job.job_id].submission.seeds || []).join(', ') || 'N/A' }}</div>
                </div>

                <div v-if="evidence[job.job_id].submission && evidence[job.job_id].submission.inputs" class="sp-detail-section">
                  <div class="sp-detail-label">Inputs</div>
                  <table class="sp-table">
                    <thead>
                      <tr>
                        <th>Path</th>
                        <th>SHA256</th>
                        <th>Size</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="(inp, idx) in evidence[job.job_id].submission.inputs" :key="idx">
                        <td class="mono">{{ inp.path }}</td>
                        <td class="mono sp-hash">{{ inp.sha256 ? inp.sha256.substring(0, 12) + '...' : 'N/A' }}</td>
                        <td class="mono">{{ formatBytes(inp.size) }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>

                <div v-if="evidence[job.job_id].completion && evidence[job.job_id].completion.outputs" class="sp-detail-section">
                  <div class="sp-detail-label">Outputs</div>
                  <table class="sp-table">
                    <thead>
                      <tr>
                        <th>Path</th>
                        <th>SHA256</th>
                        <th>Size</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="(out, idx) in evidence[job.job_id].completion.outputs" :key="idx">
                        <td class="mono">{{ out.path }}</td>
                        <td class="mono sp-hash">{{ out.sha256 ? out.sha256.substring(0, 12) + '...' : 'N/A' }}</td>
                        <td class="mono">{{ formatBytes(out.size) }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>

                <div v-if="job.result_refs" class="sp-detail-section">
                  <div class="sp-detail-label">日志</div>
                  <div class="sp-logs">
                    <a v-if="job.result_refs.stdout" :href="getLogUrl(job.result_refs.stdout)" target="_blank" class="sp-log-link">
                      <Icon name="file-text" :size="12" /> stdout
                    </a>
                    <a v-if="job.result_refs.stderr" :href="getLogUrl(job.result_refs.stderr)" target="_blank" class="sp-log-link">
                      <Icon name="file-text" :size="12" /> stderr
                    </a>
                  </div>
                </div>

                <div class="sp-detail-section">
                  <button class="btn btn-sm btn-ghost" @click="copyCommand(job.job_id)">
                    <Icon name="copy" :size="12" /> 复制状态命令
                  </button>
                </div>
              </div>
            </template>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>


<script>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import Icon from './Icon.vue'
import { SolverJobs, Projects, relativeTime, formatBytes } from '../lib/api.js'
import { useToasts } from '../composables/useToasts.js'
import { useProjectPolling } from '../composables/useProjectPolling.js'
import { useRealtime } from '../composables/useRealtime.js'

export default {
  name: 'SolverJobPanel',
  components: { Icon },
  props: {
    base: { type: String, required: true },
  },
  emits: ['changed'],
  setup(props) {
    const toasts = useToasts()
    const { wsConnected } = useRealtime()
    const jobs = ref([])
    const loading = ref(false)
    const statusFilter = ref('ALL')
    const expandedJobIds = ref(new Set())
    const evidence = ref({})
    const loadingDetail = ref({})

    const filters = [
      { key: 'ALL', label: '全部' },
      { key: 'RUNNING', label: 'RUNNING' },
      { key: 'COMPLETED', label: 'COMPLETED' },
      { key: 'FAILED', label: 'FAILED' },
      { key: 'TIMEOUT', label: 'TIMEOUT' },
    ]

    const summary = computed(() => {
      const total = jobs.value.length
      const running = jobs.value.filter(j => j.status === 'RUNNING').length
      const failed = jobs.value.filter(j => ['FAILED', 'TIMEOUT', 'EXITED'].includes(j.status)).length
      return { total, running, failed }
    })

    const filteredJobs = computed(() => {
      if (statusFilter.value === 'ALL') return jobs.value
      return jobs.value.filter(j => j.status === statusFilter.value)
    })

    async function refresh() {
      loading.value = true
      try {
        const data = await SolverJobs.list(props.base)
        jobs.value = data.jobs || []
      } catch (error) {
        toasts.error(error.response?.data?.detail || '加载求解任务失败')
      } finally {
        loading.value = false
      }
    }

    function isExpanded(jobId) {
      return expandedJobIds.value.has(jobId)
    }

    async function toggleExpand(jobId) {
      if (expandedJobIds.value.has(jobId)) {
        expandedJobIds.value.delete(jobId)
        expandedJobIds.value = new Set(expandedJobIds.value)
      } else {
        expandedJobIds.value.add(jobId)
        expandedJobIds.value = new Set(expandedJobIds.value)

        if (!evidence.value[jobId]) {
          loadingDetail.value[jobId] = true
          try {
            const data = await SolverJobs.detail(props.base, jobId)
            evidence.value[jobId] = data
          } catch (error) {
            toasts.error(`加载 ${jobId} 详情失败`)
          } finally {
            loadingDetail.value[jobId] = false
          }
        }
      }
    }

    function formatDuration(seconds) {
      if (!seconds) return 'N/A'
      if (seconds < 60) return `${seconds}s`
      if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
      return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`
    }

    function getReceiptIcon(job) {
      if (job.has_submission_receipt && job.has_completion_receipt) return 'check-circle'
      if (job.has_submission_receipt) return 'alert-triangle'
      return 'circle'
    }

    function getReceiptClass(job) {
      if (job.has_submission_receipt && job.has_completion_receipt) return 'receipt-ready'
      if (job.has_submission_receipt) return 'receipt-partial'
      return 'receipt-none'
    }

    function getReceiptTitle(job) {
      if (job.has_submission_receipt && job.has_completion_receipt) return 'Receipt complete'
      if (job.has_submission_receipt) return 'Submission receipt only'
      return 'No receipt'
    }

    function getLogUrl(path) {
      return Projects.rawUrl(props.base, path)
    }

    async function copyCommand(jobId) {
      const command = `../../solver_submit.sh --status ${jobId} --json`
      try {
        await navigator.clipboard.writeText(command)
        toasts.success('已复制命令')
      } catch {
        toasts.error('复制失败')
      }
    }

    const polling = useProjectPolling({ intervalMs: 8000, backoffIntervalMs: 30000 })

    onMounted(() => {
      refresh()
      polling.startPolling(refresh, {
        shouldRun: () => jobs.value.some(j => j.status === 'RUNNING'),
        backoffWhen: () => wsConnected.value,
      })
    })

    onUnmounted(() => {
      polling.stopPolling()
    })

    return {
      jobs,
      loading,
      statusFilter,
      filters,
      summary,
      filteredJobs,
      expandedJobIds,
      evidence,
      loadingDetail,
      refresh,
      isExpanded,
      toggleExpand,
      formatDuration,
      getReceiptIcon,
      getReceiptClass,
      getReceiptTitle,
      getLogUrl,
      copyCommand,
      relativeTime,
      formatBytes,
    }
  },
}
</script>


<style scoped>
.solver-panel {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.sp-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.sp-title {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.sp-loading {
  display: flex;
  justify-content: center;
  padding: 2rem;
}

.sp-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.75rem;
  padding: 3rem 1rem;
  color: var(--ink-2);
}

.sp-summary {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.sp-badge {
  padding: 0.25rem 0.75rem;
  border-radius: var(--r-sm);
  background: var(--panel-2);
  border: 1px solid var(--line);
  font-size: 0.8125rem;
}

.sp-badge-running {
  background: var(--live-dim);
  border-color: var(--live-line);
  color: var(--live);
}

.sp-badge-failed {
  background: var(--bad-dim);
  border-color: var(--bad-line);
  color: var(--bad);
}

.sp-filters {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.sp-filter {
  padding: 0.375rem 1rem;
  border-radius: 999px;
  background: var(--panel-2);
  border: 1px solid var(--line);
  font-size: 0.8125rem;
  cursor: pointer;
  transition: all 0.2s var(--ease);
}

.sp-filter:hover {
  background: var(--panel-3);
  border-color: var(--accent-line);
}

.sp-filter.active {
  background: var(--accent-dim);
  border-color: var(--accent-line);
  color: var(--accent);
}

.sp-list {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.sp-row {
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: var(--r);
  overflow: hidden;
}

.sp-main {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.75rem 1rem;
  cursor: pointer;
  transition: background 0.2s var(--ease);
}

.sp-main:hover {
  background: var(--panel-3);
}

.sp-jobid {
  flex-shrink: 0;
  font-size: 0.8125rem;
  color: var(--ink-2);
}

.sp-runtime-badge {
  padding: 0.125rem 0.5rem;
  border-radius: var(--r-xs);
  background: var(--accent-dim);
  border: 1px solid var(--accent-line);
  font-size: 0.75rem;
  color: var(--accent);
  flex-shrink: 0;
}

.sp-backend-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.backend-local {
  background: var(--ok);
}

.backend-cloud_run, .backend-cloud {
  background: var(--live);
}

.sp-status {
  padding: 0.125rem 0.625rem;
  border-radius: var(--r-xs);
  font-size: 0.75rem;
  font-weight: 500;
  flex-shrink: 0;
}

.status-running {
  background: var(--live-dim);
  color: var(--live);
}

.status-completed {
  background: var(--ok-dim);
  color: var(--ok);
}

.status-failed, .status-exited {
  background: var(--bad-dim);
  color: var(--bad);
}

.status-timeout {
  background: var(--amber-dim);
  color: var(--amber);
}

.status-cancelled {
  background: var(--panel-3);
  color: var(--ink-2);
}

.sp-duration {
  font-size: 0.8125rem;
  color: var(--ink-2);
  flex-shrink: 0;
}

.sp-age {
  font-size: 0.8125rem;
  color: var(--ink-3);
  margin-left: auto;
}

.sp-receipt-icon {
  flex-shrink: 0;
}

.receipt-ready {
  color: var(--ok);
}

.receipt-partial {
  color: var(--amber);
}

.receipt-none {
  color: var(--ink-3);
}

.sp-expand-icon {
  flex-shrink: 0;
  transition: transform 0.2s var(--ease);
  color: var(--ink-3);
}

.sp-expand-icon.expanded {
  transform: rotate(90deg);
}

.sp-detail {
  border-top: 1px solid var(--line);
  padding: 1rem;
  background: var(--bg-2);
}

.sp-detail-loading {
  display: flex;
  justify-content: center;
  padding: 1rem;
}

.sp-detail-content {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.sp-detail-section {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.sp-detail-label {
  font-size: 0.75rem;
  font-weight: 500;
  color: var(--ink-2);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.sp-detail-value {
  font-size: 0.875rem;
}

.sp-receipt-badge {
  display: inline-flex;
  align-items: center;
  gap: 0.375rem;
  padding: 0.25rem 0.75rem;
  border-radius: var(--r-sm);
  font-size: 0.8125rem;
}

.sp-receipt-ready {
  background: var(--ok-dim);
  color: var(--ok);
}

.sp-receipt-not-ready {
  background: var(--amber-dim);
  color: var(--amber);
}

.sp-detail-errors {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.sp-error-item {
  padding: 0.5rem;
  border-radius: var(--r-xs);
  background: var(--bad-dim);
  color: var(--bad);
  font-size: 0.8125rem;
  font-family: var(--mono);
}

.sp-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.8125rem;
  background: var(--panel);
  border-radius: var(--r-xs);
  overflow: hidden;
}

.sp-table th {
  text-align: left;
  padding: 0.5rem;
  background: var(--panel-2);
  color: var(--ink-2);
  font-weight: 500;
  border-bottom: 1px solid var(--line);
}

.sp-table td {
  padding: 0.5rem;
  border-bottom: 1px solid var(--line-2);
}

.sp-table tr:last-child td {
  border-bottom: none;
}

.sp-hash {
  color: var(--ink-3);
}

.sp-logs {
  display: flex;
  gap: 0.75rem;
}

.sp-log-link {
  display: inline-flex;
  align-items: center;
  gap: 0.375rem;
  padding: 0.375rem 0.75rem;
  border-radius: var(--r-sm);
  background: var(--panel);
  border: 1px solid var(--line);
  color: var(--accent);
  text-decoration: none;
  font-size: 0.8125rem;
  transition: all 0.2s var(--ease);
}

.sp-log-link:hover {
  background: var(--accent-dim);
  border-color: var(--accent-line);
}
</style>
