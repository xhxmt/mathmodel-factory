<template>
  <Toasts />

  <div v-if="!authReady" class="app-boot"><div class="spinner"></div></div>

  <template v-else>
    <LoginForm v-if="showLogin && !isAuthenticated" closable @login-success="onLogin" @close="showLogin = false" />

    <div v-show="!selectedProject" class="console" :class="{ 'guest-console': !isAuthenticated }" :aria-hidden="selectedProject ? 'true' : 'false'">
      <header class="rail">
        <div class="brand">
          <div class="mark"><Icon name="layers" :size="18" /></div>
          <div class="brand-tx">
            <div class="brand-name mono">PAPER FACTORY</div>
            <div class="brand-sub mono">{{ isAuthenticated ? '建模工坊 · CONTROL' : '论文展厅 · SHOWCASE' }}</div>
          </div>
        </div>

        <div v-if="isAuthenticated" class="kpis">
          <button class="kpi amber" :class="{ flash: counts.needs > 0 }" @click="jumpNeeds" :disabled="!counts.needs">
            <span class="k-val tnum">{{ counts.needs }}</span><span class="k-lbl">待你处理</span>
          </button>
          <div class="kpi"><span class="k-val live tnum">{{ counts.running }}</span><span class="k-lbl">运行中</span></div>
          <div class="kpi"><span class="k-val ok tnum">{{ counts.completed }}</span><span class="k-lbl">已完成</span></div>
          <div class="kpi"><span class="k-val tnum">{{ counts.total }}</span><span class="k-lbl">总数</span></div>
        </div>
        <div v-else class="guest-count mono">
          <Icon name="book-open" :size="14" />
          <span class="tnum">{{ showcasePapers.length }}</span> 篇展示论文
        </div>

        <div class="rr">
          <template v-if="isAuthenticated">
            <button class="btn btn-amber" @click="openNew"><Icon :name="isAdmin ? 'plus' : 'send'" :size="15" /> <span class="hide-sm">{{ isAdmin ? '新建' : '申请' }}</span></button>
            <div class="hb mono" :class="{ off: !wsConnected }" :title="wsConnected ? '实时连接正常' : '正在重连'">
              <span class="dot" :class="wsConnected ? 'live' : 'bad'"></span>{{ wsConnected ? 'LIVE' : 'RECONN' }}
            </div>
            <button class="btn btn-icon btn-ghost" @click="showPalette = true" title="命令面板 (⌘K)"><Icon name="command" :size="15" /></button>
            <button v-if="isAdmin" class="btn btn-icon btn-ghost" @click="showAdmin = true" title="管理员"><Icon name="shield" :size="15" /></button>
            <button class="btn btn-icon btn-ghost" @click="showRequests = true" title="项目申请"><Icon name="inbox" :size="15" /></button>
            <button v-if="isAdmin" class="btn btn-icon btn-ghost" @click="showModels = true" title="模型管理"><Icon name="cpu" :size="15" /></button>
          </template>
          <button class="btn btn-icon btn-ghost" @click="toggleTheme" title="切换主题"><Icon :name="theme === 'dark' ? 'sun' : 'moon'" :size="15" /></button>
          <div v-if="isAuthenticated" class="user">
            <Icon name="user" :size="14" />
            <span class="mono u-name hide-sm">{{ username }}</span>
            <button class="u-out" @click="logout" title="退出登录"><Icon name="log-out" :size="14" /></button>
          </div>
          <div v-else class="user guest-user" title="当前为只读默认用户">
            <Icon name="user" :size="14" />
            <span class="mono u-name">默认用户</span>
            <span class="guest-badge mono">访客</span>
          </div>
          <button v-if="!isAuthenticated" class="btn btn-amber login-btn" @click="showLogin = true">
            <Icon name="lock" :size="14" /> <span>登录</span>
          </button>
        </div>
      </header>

      <main v-if="isAuthenticated" class="main">
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

        <section class="fleet">
          <div class="fleet-h">
            <span class="label">题目归档 · PROBLEMS <b class="mono">{{ archives.length }}</b><small class="run-total mono">{{ others.length }} RUNS</small></span>
            <div class="filters">
              <div class="search">
                <Icon name="search" :size="13" />
                <input v-model="query" class="search-in mono" placeholder="搜索题目或运行…" spellcheck="false" />
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
          <div v-else-if="filteredArchives.length" class="grid">
            <template v-for="archive in filteredArchives" :key="archive.key">
              <ProblemArchiveCard v-if="archive.run_count > 1" :archive="archive" @open="openProject" @action="onAction" />
              <ProjectCard v-else :project="archive.latest" @open="openProject" @action="onAction" />
            </template>
          </div>
          <div v-else class="empty panel">
            <Icon name="inbox" :size="34" />
            <p>{{ query || statusFilter !== 'all' ? '无匹配项目' : '暂无项目' }}</p>
            <span class="hint mono">点击「{{ isAdmin ? '新建' : '申请' }}」{{ isAdmin ? '创建项目' : '提交项目申请' }}</span>
          </div>
        </section>
      </main>

      <main v-else class="main showcase-main">
        <section class="showcase-heading">
          <div>
            <span class="label">PUBLIC PAPER ARCHIVE</span>
            <h1>展示论文</h1>
          </div>
          <div class="readonly mono"><Icon name="shield" :size="13" /> READ ONLY</div>
        </section>

        <section class="fleet">
          <div class="fleet-h">
            <span class="label">论文 · PAPERS <b class="mono">{{ filteredShowcasePapers.length }}</b></span>
            <div class="search">
              <Icon name="search" :size="13" />
              <input v-model="showcaseQuery" class="search-in mono" placeholder="搜索展示论文…" spellcheck="false" />
              <button v-if="showcaseQuery" class="clr" @click="showcaseQuery = ''" title="清除搜索"><Icon name="x" :size="11" /></button>
            </div>
          </div>

          <div v-if="showcaseLoading" class="grid showcase-grid">
            <div v-for="i in 3" :key="i" class="skel showcase-skel panel"></div>
          </div>
          <div v-else-if="filteredShowcasePapers.length" class="grid showcase-grid">
            <ShowcasePaperCard v-for="paper in filteredShowcasePapers" :key="paper.base_name" :paper="paper" @open="openShowcasePaper" />
          </div>
          <div v-else class="empty panel">
            <Icon :name="showcaseError ? 'alert-triangle' : 'book-open'" :size="34" />
            <p>{{ showcaseError || (showcaseQuery ? '无匹配论文' : '暂无展示论文') }}</p>
            <button v-if="showcaseError" class="btn btn-sm btn-ghost" @click="loadShowcase"><Icon name="refresh" :size="13" /> 重试</button>
          </div>
        </section>
      </main>
    </div>

    <ProjectWorkspace v-if="isAuthenticated && selectedProject" :project="selectedProject" :is-admin="isAdmin" @close="closeWorkspace" @action="onAction" @refresh="fetchProjects" />
    <NewProjectModal v-if="isAuthenticated && showNew" :is-admin="isAdmin" @close="showNew = false" @project-created="onCreated" @project-requested="onRequested" />
    <AdminPanel v-if="isAuthenticated && showAdmin" @close="showAdmin = false" @changed="onAdminChanged" />
    <ProjectRequestsPanel v-if="isAuthenticated && showRequests" :admin="isAdmin" @close="showRequests = false" @changed="onAdminChanged" />
    <CommandPalette v-if="isAuthenticated" :visible="showPalette" :projects="projects" @close="showPalette = false" @open-project="openByBase" @new-project="openNew" @toggle-theme="toggleTheme" />
    <ModelManager v-if="isAuthenticated && showModels && isAdmin" @close="showModels = false" @saved="() => {}" />
    <ShowcasePaperViewer v-if="!isAuthenticated && selectedShowcasePaper" :paper="selectedShowcasePaper" @close="selectedShowcasePaper = null" />
  </template>
</template>

<script>
import { computed, defineAsyncComponent, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Icon from './components/Icon.vue'
import Toasts from './components/Toasts.vue'
import LoginForm from './components/LoginForm.vue'
import ProjectCard from './components/ProjectCard.vue'
import ProblemArchiveCard from './components/ProblemArchiveCard.vue'
import ShowcasePaperCard from './components/ShowcasePaperCard.vue'
import ShowcasePaperViewer from './components/ShowcasePaperViewer.vue'
import { Projects, Showcase, setUnauthorizedHandler, setServerErrorHandler } from './lib/api.js'
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
const AsyncAdminPanel = defineAsyncComponent({ loader: () => import('./components/AdminPanel.vue'), loadingComponent: AsyncOverlayFallback, delay: 120 })
const AsyncProjectRequestsPanel = defineAsyncComponent({ loader: () => import('./components/ProjectRequestsPanel.vue'), loadingComponent: AsyncOverlayFallback, delay: 120 })

export default {
  name: 'App',
  components: { Icon, Toasts, LoginForm, ProjectCard, ProblemArchiveCard, ShowcasePaperCard, ShowcasePaperViewer, ProjectWorkspace, NewProjectModal: AsyncNewProjectModal, CommandPalette: AsyncCommandPalette, ModelManager: AsyncModelManager, AdminPanel: AsyncAdminPanel, ProjectRequestsPanel: AsyncProjectRequestsPanel },
  setup() {
    const { theme, toggle: toggleTheme } = useTheme()
    const route = useRoute()
    const router = useRouter()
    const toasts = useToasts()
    const { invalidate: invalidateModels, load: loadModels } = useModels()
    const { isAuthenticated, username, role, status, isAdmin, bootstrap, login, logout: clearAuth } = useAuth()
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
      archives,
      filteredOthers,
      filteredArchives,
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
    const showAdmin = ref(false)
    const showRequests = ref(false)
    const authReady = ref(false)
    const showLogin = ref(false)
    const showcasePapers = ref([])
    const showcaseLoading = ref(true)
    const showcaseError = ref('')
    const showcaseQuery = ref('')
    const selectedShowcasePaper = ref(null)
    const filteredShowcasePapers = computed(() => {
      const needle = showcaseQuery.value.trim().toLowerCase()
      if (!needle) return showcasePapers.value
      return showcasePapers.value.filter((paper) => (
        `${paper.title} ${paper.base_name} ${paper.collection}`.toLowerCase().includes(needle)
      ))
    })

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
            case 'project_request_created':
            case 'project_request_failed':
            case 'project_request_rejected':
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

    async function loadShowcase() {
      showcaseLoading.value = true
      showcaseError.value = ''
      try {
        showcasePapers.value = await Showcase.list()
      } catch (error) {
        showcasePapers.value = []
        showcaseError.value = '展示论文暂不可用'
      } finally {
        showcaseLoading.value = false
      }
    }

    // ---- auth ----
    async function onLogin(data) {
      showLogin.value = false
      selectedShowcasePaper.value = null
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
      selectedShowcasePaper.value = null
      showLogin.value = false
      router.replace({ name: 'dashboard' }).catch(() => {})
      void loadShowcase()
    }
    setUnauthorizedHandler(() => {
      if (isAuthenticated.value) logout()
      else clearAuth()
    })
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
          resetProjects()
          selectedBase.value = null
          await loadShowcase()
        }
      } catch (e) {
        clearAuth()
        close()
        resetProjects()
        selectedBase.value = null
        loading.value = false
        await loadShowcase()
      } finally {
        authReady.value = true
      }
    }

    // ---- actions ----
    async function onAction(project, action) {
      try {
        await Projects.action(project.base_name, action, project.revision)
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
    function onRequested(result) {
      toasts.success(`项目 ${result.base_name} 已提交审批`)
    }
    async function onAdminChanged() {
      await refreshProjects()
    }

    // ---- navigation / deep-link ----
    function openProjectFromCard(project) { openProject(project) }
    function openByBaseFromPalette(baseName) { openByBase(baseName); showPalette.value = false }
    function closeSelectedWorkspace() { closeWorkspace() }
    function openNew() { showNew.value = true; showPalette.value = false }
    function jumpNeeds() { if (needsYou.value.length) openProject(needsYou.value[0]) }
    function openShowcasePaper(paper) { selectedShowcasePaper.value = paper }

    function onKey(e) {
      if (isAuthenticated.value && (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault(); showPalette.value = !showPalette.value
      }
    }

    let syncingRoute = false
    watch(
      [() => route.params.baseName, isAuthenticated],
      ([baseName, authenticated]) => {
        if (!authenticated) return
        const next = baseName ? String(baseName) : null
        if (selectedBase.value === next) return
        syncingRoute = true
        selectedBase.value = next
        syncingRoute = false
      },
      { immediate: true },
    )
    watch(selectedBase, (baseName) => {
      if (syncingRoute || !isAuthenticated.value) return
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
      authReady, isAuthenticated, username, role, status, isAdmin, projects, loading, wsConnected,
      selectedBase, selectedProject, showNew, showPalette, showModels, showAdmin, showRequests, query, statusFilter, filterChips,
      needsYou, others, archives, filteredOthers, filteredArchives, counts,
      showLogin, showcasePapers, showcaseLoading, showcaseError, showcaseQuery, filteredShowcasePapers, selectedShowcasePaper,
      onLogin, logout, onAction, onCreated, onRequested, onAdminChanged,
      openProject: openProjectFromCard,
      openByBase: openByBaseFromPalette,
      closeWorkspace: closeSelectedWorkspace,
      openNew,
      jumpNeeds,
      loadShowcase,
      openShowcasePaper,
      fetchProjects: refreshProjects,
    }
  },
}
</script>

<style scoped>
.app-boot { min-height: 100vh; display: flex; align-items: center; justify-content: center; }
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
.guest-count { display: inline-flex; align-items: center; gap: 7px; margin-left: 8px; padding: 7px 11px; border-left: 1px solid var(--line); color: var(--ink-3); font-size: 11px; }
.guest-count svg, .guest-count .tnum { color: var(--live); }
.guest-user { padding-right: 6px; }
.guest-badge { padding: 3px 5px; border-radius: var(--r-xs); background: var(--live-dim); color: var(--live); font-size: 8.5px; font-weight: 700; }

/* ---- main ---- */
.main { flex: 1; max-width: 1480px; width: 100%; margin: 0 auto; padding: 22px 22px 60px; }

.lane { margin-bottom: 26px; }
.lane-h { display: flex; align-items: center; gap: 9px; margin-bottom: 13px; color: var(--amber); font-weight: 700; font-size: 13px; }
.lane-n { font: 700 11px/1 var(--mono); background: var(--amber); color: var(--amber-ink); padding: 3px 7px; border-radius: 100px; }

.fleet-h { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-bottom: 14px; flex-wrap: wrap; }
.fleet-h .label b { color: var(--ink); margin-left: 4px; }
.run-total { margin-left: 8px; color: var(--ink-3); font-size: 9px; letter-spacing: 0.08em; }
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

/* ---- public paper showcase ---- */
.showcase-main { padding-top: 28px; }
.showcase-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 18px; margin-bottom: 30px; padding: 0 2px 20px; border-bottom: 1px solid var(--line); }
.showcase-heading h1 { margin-top: 7px; font-size: 27px; line-height: 1.15; letter-spacing: 0; }
.readonly { display: inline-flex; align-items: center; gap: 7px; padding: 6px 9px; border: 1px solid var(--line); border-radius: var(--r-sm); color: var(--ink-3); font-size: 9.5px; }
.readonly svg { color: var(--ok); }
.showcase-skel { height: 208px; }

@media (max-width: 720px) {
  .rail { flex-wrap: wrap; gap: 14px; }
  .kpis { order: 3; width: 100%; justify-content: space-between; }
  .kpi { flex: 1; }
  .rr { margin-left: 0; width: 100%; flex-wrap: wrap; }
  .hide-sm { display: none; }
  .grid { grid-template-columns: 1fr; }
  .guest-console .rail { gap: 10px; padding: 9px 12px; }
  .guest-console .rr { margin-left: auto; gap: 6px; }
  .guest-count { order: 3; width: 100%; margin: 0; padding: 7px 0 2px; border-left: 0; border-top: 1px solid var(--line); }
  .showcase-main { padding-top: 20px; }
  .showcase-heading { margin-bottom: 22px; }
}

@media (max-width: 460px) {
  .guest-console .brand { gap: 8px; }
  .guest-console .brand-sub { display: none; }
  .guest-console .mark { width: 34px; height: 34px; }
  .guest-badge { display: none; }
  .login-btn { padding: 8px; }
  .login-btn span { display: none; }
  .main { padding-left: 12px; padding-right: 12px; }
  .showcase-heading { align-items: center; }
  .showcase-heading h1 { font-size: 23px; }
  .readonly { padding: 6px; }
}
</style>
