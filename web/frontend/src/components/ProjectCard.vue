<template>
  <div class="project-card" :class="statusClass">
    <div class="card-header">
      <div class="card-title">
        <span class="status-dot" :class="statusClass"></span>
        <h3>{{ project.base_name }}</h3>
      </div>
      <div class="card-actions">
        <button v-if="project.is_running" @click="$emit('action', project, 'pause')" class="btn btn-sm" title="暂停">
          ⏸
        </button>
        <button v-else-if="project.status === 'paused'" @click="$emit('action', project, 'resume')" class="btn btn-sm" title="恢复">
          ▶️
        </button>
        <button v-if="project.is_running" @click="confirmKill" class="btn btn-sm btn-danger" title="终止">
          ⏹
        </button>
      </div>
    </div>

    <div class="card-body">
      <!-- Status Badge -->
      <div class="status-badge" :class="statusClass">
        {{ statusText }}
      </div>

      <!-- Consultation Alert -->
      <div v-if="project.consultation_pending" class="consultation-alert">
        <span class="alert-icon">⚠️</span>
        <span>等待人工咨询 ({{ project.consultation_gate }})</span>
      </div>

      <!-- Progress -->
      <div class="progress-section">
        <div class="progress-label">
          <span>步骤 {{ project.current_step + 1 }} / {{ project.total_steps }}</span>
          <span class="progress-percent">{{ project.progress_percent }}%</span>
        </div>
        <div class="progress-bar">
          <div class="progress-fill" :style="{ width: project.progress_percent + '%' }"></div>
        </div>
      </div>

      <!-- Metadata -->
      <div class="metadata">
        <div class="metadata-item">
          <span class="metadata-label">最后更新</span>
          <span class="metadata-value">{{ formatTime(project.last_updated) }}</span>
        </div>
        <div v-if="project.pid" class="metadata-item">
          <span class="metadata-label">PID</span>
          <span class="metadata-value">{{ project.pid }}</span>
        </div>
      </div>
    </div>

    <div class="card-footer">
      <button @click="$emit('view-details', project)" class="btn btn-primary btn-block">
        查看详情
      </button>
    </div>
  </div>
</template>

<script>
export default {
  name: 'ProjectCard',
  props: {
    project: {
      type: Object,
      required: true
    }
  },
  computed: {
    statusClass() {
      const map = {
        running: 'status-running',
        paused: 'status-paused',
        completed: 'status-completed',
        awaiting_consultation: 'status-consultation',
        failed: 'status-failed',
        killed: 'status-killed'
      }
      return map[this.project.status] || 'status-unknown'
    },
    statusText() {
      const map = {
        running: '运行中',
        paused: '已暂停',
        completed: '已完成',
        awaiting_consultation: '等待咨询',
        failed: '失败',
        killed: '已终止',
        ready: '就绪',
        setup: '初始化'
      }
      return map[this.project.status] || '未知'
    }
  },
  methods: {
    formatTime(timestamp) {
      const now = new Date()
      const time = new Date(timestamp)
      const diff = Math.floor((now - time) / 1000)

      if (diff < 60) return `${diff}秒前`
      if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`
      if (diff < 86400) return `${Math.floor(diff / 3600)}小时前`
      return timestamp.split(' ')[0]
    },
    confirmKill() {
      if (confirm(`确定要终止项目 ${this.project.base_name} 吗？`)) {
        this.$emit('action', this.project, 'kill')
      }
    }
  }
}
</script>

<style scoped>
.project-card {
  background: rgba(26, 29, 53, 0.6);
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 0.75rem;
  overflow: hidden;
  transition: all 0.3s ease;
}

.project-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3);
  border-color: rgba(255, 255, 255, 0.2);
}

.project-card.status-running {
  border-left: 3px solid #60a5fa;
}

.project-card.status-consultation {
  border-left: 3px solid #fbbf24;
}

.project-card.status-completed {
  border-left: 3px solid #34d399;
}

.project-card.status-paused {
  border-left: 3px solid #a1a1aa;
}

.project-card.status-failed,
.project-card.status-killed {
  border-left: 3px solid #ef4444;
}

.card-header {
  padding: 1rem 1.25rem;
  border-bottom: 1px solid rgba(255, 255, 255, 0.05);
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.card-title {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.card-title h3 {
  font-size: 1rem;
  font-weight: 600;
  color: #f4f4f5;
  margin: 0;
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.status-dot.status-running {
  background: #60a5fa;
  box-shadow: 0 0 8px rgba(96, 165, 250, 0.6);
  animation: pulse 2s ease-in-out infinite;
}

.status-dot.status-consultation {
  background: #fbbf24;
  box-shadow: 0 0 8px rgba(251, 191, 36, 0.6);
  animation: pulse 2s ease-in-out infinite;
}

.status-dot.status-completed {
  background: #34d399;
}

.status-dot.status-paused {
  background: #a1a1aa;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

.card-actions {
  display: flex;
  gap: 0.5rem;
}

.card-body {
  padding: 1.25rem;
}

.status-badge {
  display: inline-block;
  padding: 0.25rem 0.75rem;
  border-radius: 0.375rem;
  font-size: 0.75rem;
  font-weight: 600;
  margin-bottom: 1rem;
}

.status-badge.status-running {
  background: rgba(96, 165, 250, 0.15);
  color: #60a5fa;
}

.status-badge.status-consultation {
  background: rgba(251, 191, 36, 0.15);
  color: #fbbf24;
}

.status-badge.status-completed {
  background: rgba(52, 211, 153, 0.15);
  color: #34d399;
}

.status-badge.status-paused {
  background: rgba(161, 161, 170, 0.15);
  color: #a1a1aa;
}

.status-badge.status-failed,
.status-badge.status-killed {
  background: rgba(239, 68, 68, 0.15);
  color: #ef4444;
}

.consultation-alert {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.75rem;
  background: rgba(251, 191, 36, 0.1);
  border: 1px solid rgba(251, 191, 36, 0.3);
  border-radius: 0.5rem;
  margin-bottom: 1rem;
  font-size: 0.875rem;
  color: #fbbf24;
}

.progress-section {
  margin-bottom: 1rem;
}

.progress-label {
  display: flex;
  justify-content: space-between;
  font-size: 0.875rem;
  color: #a1a1aa;
  margin-bottom: 0.5rem;
}

.progress-percent {
  font-weight: 600;
  color: #60a5fa;
}

.progress-bar {
  height: 6px;
  background: rgba(255, 255, 255, 0.05);
  border-radius: 3px;
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: linear-gradient(90deg, #60a5fa, #3b82f6);
  border-radius: 3px;
  transition: width 0.3s ease;
}

.metadata {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.75rem;
}

.metadata-item {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.metadata-label {
  font-size: 0.75rem;
  color: #71717a;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.metadata-value {
  font-size: 0.875rem;
  color: #d4d4d8;
  font-family: 'Monaco', 'Menlo', monospace;
}

.card-footer {
  padding: 1rem 1.25rem;
  border-top: 1px solid rgba(255, 255, 255, 0.05);
}

.btn {
  padding: 0.5rem 1rem;
  border: none;
  border-radius: 0.375rem;
  font-size: 0.875rem;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s ease;
  background: rgba(255, 255, 255, 0.05);
  color: #e4e4e7;
}

.btn:hover {
  background: rgba(255, 255, 255, 0.1);
}

.btn-sm {
  padding: 0.375rem 0.625rem;
  font-size: 0.75rem;
}

.btn-primary {
  background: #3b82f6;
  color: white;
}

.btn-primary:hover {
  background: #2563eb;
}

.btn-danger {
  background: rgba(239, 68, 68, 0.2);
  color: #ef4444;
}

.btn-danger:hover {
  background: rgba(239, 68, 68, 0.3);
}

.btn-block {
  width: 100%;
}
</style>
