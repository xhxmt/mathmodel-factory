<template>
  <div class="modal-overlay" @click.self="$emit('close')">
    <div class="modal-content">
      <!-- Header -->
      <div class="modal-header">
        <h2>{{ project.base_name }}</h2>
        <button @click="$emit('close')" class="btn-close">&times;</button>
      </div>

      <!-- Tabs -->
      <div class="tabs">
        <button
          v-for="tab in tabs"
          :key="tab.id"
          :class="{ active: activeTab === tab.id }"
          @click="activeTab = tab.id"
          class="tab"
        >
          {{ tab.label }}
          <span v-if="tab.id === 'consultation' && project.consultation_pending" class="tab-badge">!</span>
        </button>
      </div>

      <!-- Tab Content -->
      <div class="modal-body">
        <!-- Overview Tab -->
        <div v-if="activeTab === 'overview'" class="tab-content">
          <div class="info-grid">
            <div class="info-item">
              <span class="info-label">状态</span>
              <span class="info-value" :class="'status-' + project.status">{{ statusText }}</span>
            </div>
            <div class="info-item">
              <span class="info-label">当前步骤</span>
              <span class="info-value">{{ project.current_step + 1 }} / {{ project.total_steps }}</span>
            </div>
            <div class="info-item">
              <span class="info-label">进度</span>
              <span class="info-value">{{ project.progress_percent }}%</span>
            </div>
            <div class="info-item">
              <span class="info-label">PID</span>
              <span class="info-value">{{ project.pid || 'N/A' }}</span>
            </div>
          </div>

          <div class="section">
            <h3>Checkpoint</h3>
            <div v-if="checkpointLoading" class="loading-text">加载中...</div>
            <pre v-else-if="checkpointContent" class="code-block">{{ checkpointContent }}</pre>
            <div v-else class="empty-text">暂无数据</div>
          </div>
        </div>

        <!-- Logs Tab -->
        <div v-if="activeTab === 'logs'" class="tab-content">
          <div class="logs-header">
            <h3>最近日志</h3>
            <button @click="fetchLogs" class="btn btn-sm">刷新</button>
          </div>
          <div v-if="logsLoading" class="loading-text">加载中...</div>
          <div v-else-if="logs.length > 0" class="logs-container">
            <div v-for="(line, idx) in logs" :key="idx" class="log-line">{{ line }}</div>
          </div>
          <div v-else class="empty-text">暂无日志</div>
        </div>

        <!-- Consultation Tab -->
        <div v-if="activeTab === 'consultation'" class="tab-content">
          <div v-if="!project.consultation_pending" class="empty-text">
            ✅ 当前无待处理的咨询请求
          </div>
          <div v-else>
            <div v-if="consultationLoading" class="loading-text">加载中...</div>
            <div v-else-if="consultationRequest">
              <div class="consultation-card">
                <div class="consultation-header">
                  <h3>{{ consultationRequest.title }}</h3>
                  <span class="badge">{{ consultationRequest.gate }}</span>
                </div>

                <div class="consultation-meta">
                  <div class="meta-item">
                    <span class="meta-label">步骤</span>
                    <span class="meta-value">{{ consultationRequest.step }}</span>
                  </div>
                  <div class="meta-item">
                    <span class="meta-label">创建时间</span>
                    <span class="meta-value">{{ consultationRequest.created }}</span>
                  </div>
                </div>

                <div class="consultation-content">
                  <h4>需要决定的事项</h4>
                  <div class="content-text">{{ consultationRequest.content }}</div>
                </div>

                <div class="consultation-form">
                  <h4>提交回答</h4>
                  <p class="form-hint">请将 GPT Pro / Gemini Deep Think 的结论粘贴到下方：</p>
                  <textarea
                    v-model="consultationAnswer"
                    placeholder="例如：
## 建模方案分析

经过 GPT Pro 深度思考，推荐采用以下方案：

1. 主模型：多目标优化 MILP
   - 目标函数：最小化总成本 + 最大化覆盖率
   - 约束：资源限制、时间窗口、容量约束

2. 辅助方法：
   - 灵敏度分析：参数扰动范围 ±20%
   - 鲁棒性验证：蒙特卡洛模拟 1000 次

理由：..."
                    class="form-textarea"
                    rows="12"
                  ></textarea>
                  <div class="form-actions">
                    <button
                      @click="submitConsultation"
                      :disabled="!consultationAnswer.trim() || submitting"
                      class="btn btn-primary"
                    >
                      {{ submitting ? '提交中...' : '提交并恢复运行' }}
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, computed, onMounted, watch } from 'vue'
import axios from 'axios'

export default {
  name: 'ProjectDetailModal',
  props: {
    project: {
      type: Object,
      required: true
    }
  },
  emits: ['close', 'consultation-submit'],
  setup(props, { emit }) {
    const activeTab = ref('overview')
    const checkpointContent = ref('')
    const checkpointLoading = ref(false)
    const logs = ref([])
    const logsLoading = ref(false)
    const consultationRequest = ref(null)
    const consultationLoading = ref(false)
    const consultationAnswer = ref('')
    const submitting = ref(false)

    const tabs = [
      { id: 'overview', label: '概览' },
      { id: 'logs', label: '日志' },
      { id: 'consultation', label: '人工咨询' }
    ]

    const statusText = computed(() => {
      const map = {
        running: '运行中',
        paused: '已暂停',
        completed: '已完成',
        awaiting_consultation: '等待咨询',
        failed: '失败',
        killed: '已终止'
      }
      return map[props.project.status] || '未知'
    })

    const fetchCheckpoint = async () => {
      checkpointLoading.value = true
      try {
        const response = await axios.get(`/api/projects/${props.project.base_name}/checkpoint`)
        checkpointContent.value = response.data.content
      } catch (error) {
        console.error('Failed to fetch checkpoint:', error)
      } finally {
        checkpointLoading.value = false
      }
    }

    const fetchLogs = async () => {
      logsLoading.value = true
      try {
        const response = await axios.get(`/api/projects/${props.project.base_name}/logs`)
        logs.value = response.data.logs
      } catch (error) {
        console.error('Failed to fetch logs:', error)
      } finally {
        logsLoading.value = false
      }
    }

    const fetchConsultation = async () => {
      if (!props.project.consultation_pending) return

      consultationLoading.value = true
      try {
        const response = await axios.get(`/api/projects/${props.project.base_name}/consultation`)
        consultationRequest.value = response.data
      } catch (error) {
        console.error('Failed to fetch consultation:', error)
      } finally {
        consultationLoading.value = false
      }
    }

    const submitConsultation = async () => {
      if (!consultationAnswer.value.trim()) return

      submitting.value = true
      try {
        await emit('consultation-submit', props.project.base_name, consultationAnswer.value)
        consultationAnswer.value = ''
        emit('close')
      } catch (error) {
        alert(`提交失败: ${error.message}`)
      } finally {
        submitting.value = false
      }
    }

    watch(activeTab, (newTab) => {
      if (newTab === 'overview' && !checkpointContent.value) {
        fetchCheckpoint()
      } else if (newTab === 'logs' && logs.value.length === 0) {
        fetchLogs()
      } else if (newTab === 'consultation' && !consultationRequest.value) {
        fetchConsultation()
      }
    })

    onMounted(() => {
      fetchCheckpoint()
    })

    return {
      activeTab,
      tabs,
      statusText,
      checkpointContent,
      checkpointLoading,
      logs,
      logsLoading,
      consultationRequest,
      consultationLoading,
      consultationAnswer,
      submitting,
      fetchLogs,
      submitConsultation
    }
  }
}
</script>

<style scoped>
.modal-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.75);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: 2rem;
}

.modal-content {
  background: #1a1d35;
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 0.75rem;
  width: 100%;
  max-width: 900px;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
}

.modal-header {
  padding: 1.5rem;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.modal-header h2 {
  margin: 0;
  font-size: 1.5rem;
  color: #f4f4f5;
}

.btn-close {
  background: none;
  border: none;
  font-size: 2rem;
  color: #a1a1aa;
  cursor: pointer;
  padding: 0;
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 0.375rem;
  transition: all 0.2s;
}

.btn-close:hover {
  background: rgba(255, 255, 255, 0.1);
  color: #f4f4f5;
}

.tabs {
  display: flex;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
  padding: 0 1.5rem;
}

.tab {
  padding: 1rem 1.5rem;
  background: none;
  border: none;
  color: #a1a1aa;
  font-size: 0.875rem;
  font-weight: 500;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  transition: all 0.2s;
  position: relative;
}

.tab:hover {
  color: #e4e4e7;
}

.tab.active {
  color: #60a5fa;
  border-bottom-color: #60a5fa;
}

.tab-badge {
  position: absolute;
  top: 0.5rem;
  right: 0.5rem;
  background: #fbbf24;
  color: #0a0e27;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.75rem;
  font-weight: 700;
}

.modal-body {
  flex: 1;
  overflow-y: auto;
  padding: 1.5rem;
}

.tab-content {
  animation: fadeIn 0.2s ease;
}

@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}

.info-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 1rem;
  margin-bottom: 1.5rem;
}

.info-item {
  background: rgba(255, 255, 255, 0.03);
  padding: 1rem;
  border-radius: 0.5rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.info-label {
  font-size: 0.75rem;
  color: #71717a;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.info-value {
  font-size: 1.125rem;
  font-weight: 600;
  color: #e4e4e7;
}

.status-running { color: #60a5fa; }
.status-completed { color: #34d399; }
.status-paused { color: #a1a1aa; }
.status-awaiting_consultation { color: #fbbf24; }

.section {
  margin-top: 1.5rem;
}

.section h3 {
  font-size: 1rem;
  color: #f4f4f5;
  margin-bottom: 1rem;
}

.code-block {
  background: rgba(0, 0, 0, 0.3);
  padding: 1rem;
  border-radius: 0.5rem;
  font-family: 'Monaco', 'Menlo', monospace;
  font-size: 0.875rem;
  color: #d4d4d8;
  overflow-x: auto;
  line-height: 1.6;
  white-space: pre-wrap;
  word-wrap: break-word;
}

.logs-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 1rem;
}

.logs-header h3 {
  margin: 0;
  font-size: 1rem;
  color: #f4f4f5;
}

.logs-container {
  background: rgba(0, 0, 0, 0.3);
  padding: 1rem;
  border-radius: 0.5rem;
  font-family: 'Monaco', 'Menlo', monospace;
  font-size: 0.75rem;
  color: #a1a1aa;
  max-height: 400px;
  overflow-y: auto;
}

.log-line {
  padding: 0.25rem 0;
  line-height: 1.4;
}

.consultation-card {
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 0.5rem;
  padding: 1.5rem;
}

.consultation-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 1rem;
}

.consultation-header h3 {
  margin: 0;
  font-size: 1.125rem;
  color: #f4f4f5;
}

.badge {
  background: rgba(251, 191, 36, 0.2);
  color: #fbbf24;
  padding: 0.25rem 0.75rem;
  border-radius: 0.375rem;
  font-size: 0.75rem;
  font-weight: 600;
}

.consultation-meta {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 1rem;
  margin-bottom: 1.5rem;
  padding-bottom: 1.5rem;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
}

.meta-item {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.meta-label {
  font-size: 0.75rem;
  color: #71717a;
}

.meta-value {
  font-size: 0.875rem;
  color: #e4e4e7;
}

.consultation-content {
  margin-bottom: 1.5rem;
}

.consultation-content h4 {
  font-size: 0.875rem;
  color: #a1a1aa;
  margin-bottom: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.content-text {
  background: rgba(0, 0, 0, 0.2);
  padding: 1rem;
  border-radius: 0.375rem;
  color: #d4d4d8;
  line-height: 1.6;
  white-space: pre-wrap;
}

.consultation-form h4 {
  font-size: 0.875rem;
  color: #a1a1aa;
  margin-bottom: 0.5rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.form-hint {
  font-size: 0.875rem;
  color: #71717a;
  margin-bottom: 0.75rem;
}

.form-textarea {
  width: 100%;
  background: rgba(0, 0, 0, 0.3);
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 0.5rem;
  padding: 0.75rem;
  color: #e4e4e7;
  font-family: 'Monaco', 'Menlo', monospace;
  font-size: 0.875rem;
  line-height: 1.6;
  resize: vertical;
  margin-bottom: 1rem;
}

.form-textarea:focus {
  outline: none;
  border-color: #60a5fa;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
}

.btn {
  padding: 0.5rem 1rem;
  border: none;
  border-radius: 0.375rem;
  font-size: 0.875rem;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s;
}

.btn-sm {
  padding: 0.375rem 0.75rem;
  font-size: 0.75rem;
}

.btn-primary {
  background: #3b82f6;
  color: white;
}

.btn-primary:hover:not(:disabled) {
  background: #2563eb;
}

.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.loading-text,
.empty-text {
  text-align: center;
  padding: 2rem;
  color: #71717a;
}

.empty-text {
  font-size: 1rem;
}
</style>
