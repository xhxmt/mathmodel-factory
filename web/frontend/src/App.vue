<template>
  <Toasts />

  <LoginForm v-if="!isAuthenticated" @login-success="onLogin" />

  <template v-else>
    <div class="console">
      <!-- status rail -->
      <header class="rail">
        <div class="brand">
          <div class="mark"><Icon name="layers" :size="18" /></div>
          <div class="brand-tx">
            <div class="brand-name mono">PAPER FACTORY</div>
            <div class="brand-sub mono">建模工坊 · CONTROL</div>
          </div>
        </div>

        <div class="kpis">
          <button class="kpi amber" :class="{ flash: counts.needs > 0 }" @click="jumpNeeds" :disabled="!counts.needs">
            <span class="k-val tnum">{{ counts.needs }}</span><span class="k-lbl">待你处理</span>
          </button>
          <div class="kpi"><span class="k-val live tnum">{{ counts.running }}</span><span class="k-lbl">运行中</span></div>
          <div class="kpi"><span class="k-val ok tnum">{{ counts.completed }}</span><span class="k-lbl">已完成</span></div>
          <div class="kpi"><span class="k-val tnum">{{ counts.total }}</span><span class="k-lbl">总数</span></div>
        </div>

        <div class="rr">
          <button class="btn btn-amber" @click="openNew"><Icon name="plus" :size="15" /> <span class="hide-sm">新建</span></button>
          <div class="hb mono" :class="{ off: !wsConnected }" :title="wsConnected ? '实时连接正常' : '正在重连'">
            <span class="dot" :class="wsConnected ? 'live' : 'bad'"></span>{{ wsConnected ? 'LIVE' : 'RECONN' }}
          </div>
          <button class="btn btn-icon btn-ghost" @click="showPalette = true" title="命令面板 (⌘K)"><Icon name="command" :size="15" /></button>
          <button class="btn btn-icon btn-ghost" @click="showModels = true" title="模型管理"><Icon name="cpu" :size="15" /></button>
          <button class="btn btn-icon btn-ghost" @click="toggleTheme" title="切换主题"><Icon :name="theme === 'dark' ? 'sun' : 'moon'" :size="15" /></button>
          <div class="user">
            <Icon name="user" :size="14" />
            <span class="mono u-name hide-sm">{{ username }}</span>
            <button class="u-out" @click="logout" title="退出登录"><Icon name="log-out" :size="14" /></button>
          </div>
        </div>
      </header>

      <main class="main">
        <!-- needs-you lane -->
        <section v-if="needsYou.length" class="lane">
          <div class="lane-h">
            <Icon name="alert-triangle" :size="14" />
            <span>等待你处理</span>
            <span class="lane-n mono">{{ needsYou.length }}</span>
          </div>
          <div class="grid">
            <ProjectCard v-for="p in needsYou" :key="p.base_name" :project="p" @open="openProject" @action="onAction" />
          </div>
        </section>

        <!-- fleet -->
        <section class="fleet">
          <div class="fleet-h">
            <span class="label">项目 · PROJECTS <b class="mono">{{ others.length }}</b></span>
            <div class="filters">
              <div class="search">
                <Icon name="search" :size="13" />
                <input v-model="query" class="search-in mono" placeholder="搜索项目…" spellcheck="false" />
                <button v-if="query" class="clr" @click="query = ''"><Icon name="x" :size="11" /></button>
              </div>
              <div class="chips">
                <button v-for="f in filterChips" :key="f.key" class="chip" :class="{ on: statusFilter === f.key }" @click="statusFilter = f.key">{{ f.label }}</button>
              </div>
            </div>
          </div>

          <div v-if="loading" class="grid">
            <div v-for="i in 4" :key="i" class="skel panel"></div>
          </div>
          <div v-else-if="filteredOthers.length" class="grid">
            <ProjectCard v-for="p in filteredOthers" :key="p.base_name" :project="p" @open="openProject" @action="onAction" />
          </div>
          <div v-else class="empty panel">
            <Icon name="inbox" :size="34" />
            <p>{{ query || statusFilter !== 'all' ? '无匹配项目' : '暂无项目' }}</p>
            <span class="hint mono">点击「新建」或 ./launch_agents.sh new 创建</span>
          </div>
        </section>
      </main>
    </div>

    <ProjectWorkspace v-if="selectedProject" :project="selectedProject" @close="closeWorkspace" @action="onAction" @refresh="fetchProjects" />
    <NewProjectModal v-if="showNew" @close="showNew = false" @project-created="onCreated" />
    <CommandPalette :visible="showPalette" :projects="projects" @close="showPalette = false" @open-project="openByBase" @new-project="openNew" @toggle-theme="toggleTheme" />
    <ModelManager v-if="showModels" @close="showModels = false" @saved="() => {}" />
  </template>
</template>

<script>
import { defineAsyncComponent, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Icon from './components/Icon.vue'
import Toasts from './components/Toasts.vue'
import LoginForm from './components/LoginForm.vue'
import ProjectCard from './components/ProjectCard.vue'
import NewProjectModal from './components/NewProjectModal.vue'
import CommandPalette from './components/CommandPalette.vue'
import ModelManager from './components/ModelManager.vue'
import { Projects, setUnauthorizedHandler, setServerErrorHandler } from './lib/api.js'
import { useTheme } from './composables/useTheme.js'
import { useToasts, notifyDesktop } from './composables/useToasts.js'
import { useModels } from './composables/useModels.js'
import { useAuth } from './composables/useAuth.js'
import { useProjects } from './composables/useProjects.js'
import { useRealtime } from './composables/useRealtime.js'
import { runAuthenticatedStartup, runLoginFlow } from './lib/appStartup.js'

// Lazy-loaded overlay: only mounted when a project is opened. Pulls the whole
// ProjectWorkspace subtree (and KaTeX, via markdown.js) out of the initial bundle.
const ProjectWorkspace = defineAsyncComponent({
  loader: () => import('./components/ProjectWorkspace.vue'),
  loadingComponent: { template: '<div class="ws-overlay-loading"><div class="spinner"></div></div>' },
  delay: 120,
})

const AsyncOverlayFallback = {
  template: '<div class="overlay-loading panel"><div class="spinner"></div></div>',
}

const AsyncNewProjectModal = defineAsyncComponent({ loader: () => import('./components/NewProjectModal.vue'), loadingComponent: AsyncOverlayFallback, delay: 120 })
const AsyncCommandPalette = defineAsyncComponent({ loader: () => import('./components/CommandPalette.vue'), loadingComponent: AsyncOverlayFallback, delay: 120 })
const AsyncModelManager = defineAsyncComponent({ loader: () => import('./components/ModelManager.vue'), loadingComponent: AsyncOverlayFallback, delay: 120 })

export default {
  name: 'App',
  components: { Icon, Toasts, LoginForm, ProjectCard, ProjectWorkspace, NewProjectModal: AsyncNewProjectModal, CommandPalette: AsyncCommandPalette, ModelManager: AsyncModelManager },
  setup() {
    const { theme, toggle: toggleTheme } = useTheme()
    const route = useRoute()
    const router = useRouter()
    const toasts = useToasts()
    const { invalidate: invalidateModels, load: loadModels } = useModels()
    const { isAuthenticated, username, bootstrap, login, logout: clearAuth } = useAuth()
    const {
      projects,
      loading,
      selectedBase,
      selectedProject,
      query,
      statusFilter,
      filterChips,
      needsYou,
      others,
      filteredOthers,
      counts,
      fetchProjects,
      applyProjects,
      patchProject,
      openProject,
      openByBase,
      closeWorkspace,
      resetProjects,
    } = useProjects()
    const { wsConnected, connect, close } = useRealtime()

    const showNew = ref(false)
    const showPalette = ref(false)
    const showModels = ref(false)

    function notifyAwaiting(baseName) {
      toasts.warn(`项目 ${baseName} 需要你的决策`, '人工咨询')
      notifyDesktop('Paper Factory · 需要你决策', `${baseName} 已在关卡处暂停`)
    }

    async function refreshProjects() {
      try {
        await fetchProjects(notifyAwaiting)
      } catch (e) {
        // surfaced elsewhere
      }
    }

    async function connectWS() {
      await connect((message) => {
        try {
          switch (message.type) {
            case 'status_update':
              if (message.projects) applyProjects(message.projects, notifyAwaiting)
              break
            case 'project_updated':
              if (message.status) patchProject(message.status, notifyAwaiting)
              break
            case 'models_updated':
              invalidateModels()
              break
            case 'project_created':
            case 'project_action':
            case 'consultation_answered':
              refreshProjects()
              break
            default:
              break
          }
        } catch (e) {
          // ignore malformed ws messages
        }
      })
    }

    function onModelWarmupError(error) {
      const detail = error?.response?.data?.detail || error?.message || '模型配置暂不可用'
      toasts.warn(detail, '模型配置')
    }

    // ---- auth ----
    async function onLogin(data) {
      loading.value = true
      await runLoginFlow(
        {
          login,
          refreshProjects,
          loadModels,
          connectWS,
          onModelWarmupError,
        },
        data,
      )
    }
    function logout() {
      clearAuth()
      close()
      resetProjects()
    }
    setUnauthorizedHandler(logout)
    setServerErrorHandler((msg) => toasts.error(msg || '服务暂时不可用'))

    async function checkAuth() {
      try {
        const ok = await runAuthenticatedStartup({
          bootstrap,
          refreshProjects,
          loadModels,
          connectWS,
          onModelWarmupError,
        })
        if (!ok) {
          loading.value = false
          return
        }
      } catch (e) {
        logout()
        loading.value = false
      }
    }

    // ---- actions ----
    async function onAction(project, action) {
      try {
        await Projects.action(project.base_name, action)
        const labels = { pause: '已暂停', resume: '已恢复', kill: '已终止' }
        toasts.success(`${project.base_name} ${labels[action] || action}`)
        if (action === 'kill') selectedBase.value = null
        await refreshProjects()
      } catch (e) {
        toasts.error(e.response?.data?.detail || '操作失败')
      }
    }
    function onCreated(result) {
      toasts.success(`项目 ${result.base_name} 已创建`)
      refreshProjects()
    }

    // ---- navigation / deep-link ----
    function openProjectFromCard(project) { openProject(project) }
    function openByBaseFromPalette(baseName) { openByBase(baseName); showPalette.value = false }
    function closeSelectedWorkspace() { closeWorkspace() }
    function openNew() { showNew.value = true; showPalette.value = false }
    function jumpNeeds() { if (needsYou.value.length) openProject(needsYou.value[0]) }

    function onKey(e) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault(); showPalette.value = !showPalette.value
      }
    }

    let syncingRoute = false
    watch(
      () => route.params.baseName,
      (baseName) => {
        const next = baseName ? String(baseName) : null
        if (selectedBase.value === next) return
        syncingRoute = true
        selectedBase.value = next
        syncingRoute = false
      },
      { immediate: true },
    )
    watch(selectedBase, (baseName) => {
      if (syncingRoute) return
      const target = baseName ? { name: 'project', params: { baseName } } : { name: 'dashboard' }
      router.replace(target).catch(() => {})
    })

    onMounted(async () => {
      await checkAuth()
      window.addEventListener('keydown', onKey)
    })
    onUnmounted(() => {
      close()
      window.removeEventListener('keydown', onKey)
    })

    return {
      theme, toggleTheme,
      isAuthenticated, username, projects, loading, wsConnected,
      selectedBase, selectedProject, showNew, showPalette, showModels, query, statusFilter, filterChips,
      needsYou, others, filteredOthers, counts,
      onLogin, logout, onAction, onCreated,
      openProject: openProjectFromCard,
      openByBase: openByBaseFromPalette,
      closeWorkspace: closeSelectedWorkspace,
      openNew,
      jumpNeeds,
      fetchProjects: refreshProjects,
    }
  },
}
</script>

<style scoped>
.console { min-height: 100vh; display: flex; flex-direction: column; }

/* ---- status rail ---- */
.rail {
  position: sticky; top: 0; z-index: 90;
  display: flex; align-items: center; gap: 22px;
  padding: 11px 22px; min-height: var(--header-h);
  border-bottom: 1px solid var(--line);
  background: color-mix(in srgb, var(--bg) 86%, transparent);
  backdrop-filter: blur(10px);
}
.brand { display: flex; align-items: center; gap: 11px; }
.mark { width: 38px; height: 38px; border-radius: var(--r); background: var(--amber); color: var(--amber-ink); display: flex; align-items: center; justify-content: center; box-shadow: 0 0 18px var(--amber-glow); }
.brand-name { font-size: 14px; font-weight: 700; letter-spacing: 0.06em; }
.brand-sub { font-size: 9.5px; color: var(--ink-3); letter-spacing: 0.14em; margin-top: 1px; }

.kpis { display: flex; align-items: stretch; gap: 8px; margin-left: 8px; }
.kpi { display: flex; flex-direction: column; align-items: center; gap: 3px; padding: 6px 16px; background: none; border: 1px solid transparent; border-radius: var(--r); }
.kpi + .kpi { border-left: 1px solid var(--line); border-radius: 0; }
.k-val { font-size: 20px; font-weight: 700; line-height: 1; color: var(--ink); }
.k-val.live { color: var(--live); } .k-val.ok { color: var(--ok); }
.k-lbl { font: 500 9.5px/1 var(--mono); letter-spacing: 0.1em; text-transform: uppercase; color: var(--ink-3); }
.kpi.amber { cursor: pointer; border-radius: var(--r); border-color: transparent; }
.kpi.amber .k-val { color: var(--ink-3); }
.kpi.amber.flash { background: var(--amber-dim); border-color: var(--amber-line); }
.kpi.amber.flash .k-val { color: var(--amber); }
.kpi.amber.flash .k-lbl { color: var(--amber); }
.kpi.amber:disabled { cursor: default; }
.kpi.amber.flash { animation: kflash 2.2s var(--ease) infinite; }
@keyframes kflash { 0%,100% { box-shadow: 0 0 0 0 transparent; } 50% { box-shadow: 0 0 0 1px var(--amber-line); } }

.rr { display: flex; align-items: center; gap: 9px; margin-left: auto; }
.hb { display: inline-flex; align-items: center; gap: 6px; font-size: 10px; letter-spacing: 0.1em; color: var(--ok); padding: 6px 9px; border: 1px solid var(--line); border-radius: var(--r-sm); }
.hb.off { color: var(--bad); }
.user { display: flex; align-items: center; gap: 8px; padding: 6px 8px 6px 11px; border: 1px solid var(--line); border-radius: 100px; color: var(--ink-2); }
.u-name { font-size: 12px; }
.u-out { background: none; border: none; color: var(--ink-3); cursor: pointer; display: flex; padding: 4px; border-radius: 50%; }
.u-out:hover { color: var(--bad); background: var(--bad-dim); }

/* ---- main ---- */
.main { flex: 1; max-width: 1480px; width: 100%; margin: 0 auto; padding: 22px 22px 60px; }

.lane { margin-bottom: 26px; }
.lane-h { display: flex; align-items: center; gap: 9px; margin-bottom: 13px; color: var(--amber); font-weight: 700; font-size: 13px; }
.lane-n { font: 700 11px/1 var(--mono); background: var(--amber); color: var(--amber-ink); padding: 3px 7px; border-radius: 100px; }

.fleet-h { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-bottom: 14px; flex-wrap: wrap; }
.fleet-h .label b { color: var(--ink); margin-left: 4px; }
.filters { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.search { display: flex; align-items: center; gap: 7px; padding: 7px 11px; background: var(--panel); border: 1px solid var(--line-2); border-radius: var(--r); color: var(--ink-3); }
.search-in { background: none; border: none; outline: none; color: var(--ink); font-size: 12.5px; width: 150px; }
.clr { background: none; border: none; color: var(--ink-3); cursor: pointer; display: flex; padding: 0; }
.clr:hover { color: var(--ink); }
.chips { display: flex; gap: 4px; padding: 3px; background: var(--panel); border: 1px solid var(--line); border-radius: var(--r); }
.chip { padding: 6px 11px; background: none; border: none; border-radius: var(--r-sm); color: var(--ink-3); font: 600 12px/1 var(--sans); cursor: pointer; }
.chip:hover { color: var(--ink); }
.chip.on { background: var(--panel-3); color: var(--ink); }

.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 14px; }

.skel { height: 188px; position: relative; overflow: hidden; }
.skel::after { content: ''; position: absolute; inset: 0; background: linear-gradient(90deg, transparent, var(--panel-2), transparent); animation: sweep 1.4s infinite; }

.empty { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 11px; padding: 70px 20px; color: var(--ink-3); }
.empty p { font-size: 15px; color: var(--ink-2); }
.empty .hint { font-size: 11px; }

@media (max-width: 720px) {
  .rail { flex-wrap: wrap; gap: 14px; }
  .kpis { order: 3; width: 100%; justify-content: space-between; }
  .kpi { flex: 1; }
  .rr { margin-left: 0; }
  .hide-sm { display: none; }
  .grid { grid-template-columns: 1fr; }
}
</style>
