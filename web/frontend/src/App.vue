<template>
  <div class="dashboard">
    <!-- Header -->
    <header class="header">
      <div class="header-content">
        <h1 class="title">
          <span class="icon">📄</span>
          Paper Factory Dashboard
        </h1>
        <div class="header-stats">
          <div class="stat">
            <span class="stat-label">运行中</span>
            <span class="stat-value">{{ runningCount }}</span>
          </div>
          <div class="stat">
            <span class="stat-label">等待咨询</span>
            <span class="stat-value consultation">{{ consultationCount }}</span>
          </div>
          <div class="stat">
            <span class="stat-label">已完成</span>
            <span class="stat-value">{{ completedCount }}</span>
          </div>
        </div>
      </div>
    </header>

    <!-- Main Content -->
    <main class="main-content">
      <div class="container">
        <!-- Connection Status -->
        <div v-if="!wsConnected" class="alert alert-warning">
          ⚠️ WebSocket 连接断开，正在重连...
        </div>

        <!-- Projects Grid -->
        <div class="projects-grid">
          <ProjectCard
            v-for="project in projects"
            :key="project.base_name"
            :project="project"
            @view-details="viewProjectDetails"
            @action="handleProjectAction"
          />
        </div>

        <!-- Empty State -->
        <div v-if="projects.length === 0 && !loading" class="empty-state">
          <div class="empty-icon">📭</div>
          <p>暂无项目</p>
          <p class="empty-hint">使用 <code>./launch_agents.sh new</code> 创建新项目</p>
        </div>

        <!-- Loading State -->
        <div v-if="loading" class="loading">
          <div class="spinner"></div>
          <p>加载中...</p>
        </div>
      </div>
    </main>

    <!-- Project Detail Modal -->
    <ProjectDetailModal
      v-if="selectedProject"
      :project="selectedProject"
      @close="selectedProject = null"
      @consultation-submit="handleConsultationSubmit"
    />
  </div>
</template>

<script>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import axios from 'axios'
import ProjectCard from './components/ProjectCard.vue'
import ProjectDetailModal from './components/ProjectDetailModal.vue'

export default {
  name: 'App',
  components: {
    ProjectCard,
    ProjectDetailModal
  },
  setup() {
    const projects = ref([])
    const selectedProject = ref(null)
    const loading = ref(true)
    const wsConnected = ref(false)
    let ws = null
    let reconnectTimer = null

    const runningCount = computed(() =>
      projects.value.filter(p => p.status === 'running').length
    )

    const consultationCount = computed(() =>
      projects.value.filter(p => p.consultation_pending).length
    )

    const completedCount = computed(() =>
      projects.value.filter(p => p.status === 'completed').length
    )

    const fetchProjects = async () => {
      try {
        const response = await axios.get('/api/projects')
        projects.value = response.data
        loading.value = false
      } catch (error) {
        console.error('Failed to fetch projects:', error)
        loading.value = false
      }
    }

    const connectWebSocket = () => {
      ws = new WebSocket(`ws://${window.location.hostname}:8000/ws`)

      ws.onopen = () => {
        wsConnected.value = true
        console.log('WebSocket connected')
      }

      ws.onmessage = (event) => {
        const data = JSON.parse(event.data)

        if (data.type === 'status_update') {
          projects.value = data.projects
        } else if (data.type === 'project_updated') {
          const index = projects.value.findIndex(p => p.base_name === data.project)
          if (index !== -1) {
            projects.value[index] = data.status
          } else {
            fetchProjects()
          }
        }
      }

      ws.onclose = () => {
        wsConnected.value = false
        console.log('WebSocket disconnected, reconnecting in 3s...')
        reconnectTimer = setTimeout(connectWebSocket, 3000)
      }

      ws.onerror = (error) => {
        console.error('WebSocket error:', error)
      }
    }

    const viewProjectDetails = (project) => {
      selectedProject.value = project
    }

    const handleProjectAction = async (project, action) => {
      try {
        await axios.post(`/api/projects/${project.base_name}/action`, { action })
        await fetchProjects()
      } catch (error) {
        console.error('Action failed:', error)
        alert(`操作失败: ${error.message}`)
      }
    }

    const handleConsultationSubmit = async (projectName, answer) => {
      try {
        await axios.post(`/api/projects/${projectName}/consultation/answer`, { answer })
        selectedProject.value = null
        await fetchProjects()
      } catch (error) {
        console.error('Consultation submission failed:', error)
        alert(`提交失败: ${error.message}`)
      }
    }

    onMounted(() => {
      fetchProjects()
      connectWebSocket()
    })

    onUnmounted(() => {
      if (ws) ws.close()
      if (reconnectTimer) clearTimeout(reconnectTimer)
    })

    return {
      projects,
      selectedProject,
      loading,
      wsConnected,
      runningCount,
      consultationCount,
      completedCount,
      viewProjectDetails,
      handleProjectAction,
      handleConsultationSubmit
    }
  }
}
</script>

<style scoped>
.dashboard {
  min-height: 100vh;
  background: linear-gradient(135deg, #0a0e27 0%, #1a1d35 100%);
}

.header {
  background: rgba(26, 29, 53, 0.8);
  backdrop-filter: blur(10px);
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
  padding: 1.5rem 0;
  position: sticky;
  top: 0;
  z-index: 100;
}

.header-content {
  max-width: 1400px;
  margin: 0 auto;
  padding: 0 2rem;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.title {
  font-size: 1.75rem;
  font-weight: 700;
  color: #f4f4f5;
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.icon {
  font-size: 2rem;
}

.header-stats {
  display: flex;
  gap: 2rem;
}

.stat {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.25rem;
}

.stat-label {
  font-size: 0.75rem;
  color: #a1a1aa;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.stat-value {
  font-size: 1.5rem;
  font-weight: 700;
  color: #60a5fa;
}

.stat-value.consultation {
  color: #fbbf24;
}

.main-content {
  padding: 2rem 0;
}

.container {
  max-width: 1400px;
  margin: 0 auto;
  padding: 0 2rem;
}

.alert {
  padding: 1rem;
  border-radius: 0.5rem;
  margin-bottom: 1.5rem;
  font-size: 0.875rem;
}

.alert-warning {
  background: rgba(251, 191, 36, 0.1);
  border: 1px solid rgba(251, 191, 36, 0.3);
  color: #fbbf24;
}

.projects-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
  gap: 1.5rem;
}

.empty-state {
  text-align: center;
  padding: 4rem 2rem;
  color: #71717a;
}

.empty-icon {
  font-size: 4rem;
  margin-bottom: 1rem;
}

.empty-state p {
  font-size: 1.125rem;
  margin-bottom: 0.5rem;
}

.empty-hint {
  font-size: 0.875rem;
  color: #52525b;
}

.empty-hint code {
  background: rgba(255, 255, 255, 0.05);
  padding: 0.25rem 0.5rem;
  border-radius: 0.25rem;
  font-family: 'Monaco', 'Menlo', monospace;
  color: #a1a1aa;
}

.loading {
  text-align: center;
  padding: 4rem 2rem;
}

.spinner {
  width: 40px;
  height: 40px;
  margin: 0 auto 1rem;
  border: 3px solid rgba(96, 165, 250, 0.2);
  border-top-color: #60a5fa;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

@media (max-width: 768px) {
  .header-content {
    flex-direction: column;
    gap: 1rem;
  }

  .projects-grid {
    grid-template-columns: 1fr;
  }
}
</style>
