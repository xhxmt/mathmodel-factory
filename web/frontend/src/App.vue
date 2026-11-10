<template>
  <Toasts />

  <div v-if="!authReady" class="app-boot"><div class="spinner"></div></div>

  <template v-else>
    <LoginForm v-if="showLogin && !isAuthenticated" closable @login-success="onLogin" @close="showLogin = false" />

    <div v-show="!selectedProject" class="console" :aria-hidden="selectedProject ? 'true' : 'false'">
      <header class="topbar">
        <div class="topbar-in">
          <div class="brand">
            <div class="mark"><Icon name="layers" :size="15" /></div>
            <span class="brand-name">建模工坊</span>
          </div>

          <div v-if="isAuthenticated" class="search">
            <Icon name="search" :size="14" />
            <input v-model="query" class="search-in" placeholder="搜索题目或运行…" spellcheck="false" />
            <button v-if="query" class="clr" @click="query = ''" title="清除"><Icon name="x" :size="12" /></button>
          </div>
          <div v-else class="spacer"></div>

          <div class="tools">
            <template v-if="isAuthenticated">
              <button class="btn btn-primary btn-sm" @click="openNew"><Icon :name="isAdmin ? 'plus' : 'send'" :size="14" /><span class="hide-sm">{{ isAdmin ? '新建项目' : '申请项目' }}</span></button>
              <span class="conn" :class="{ off: !wsConnected }" role="status" :aria-label="wsConnected ? '实时连接正常' : '实时连接断开，正在重连'" :title="wsConnected ? '实时连接正常' : '实时连接断开，正在重连'"><span class="dot" :class="wsConnected ? 'ok' : 'bad'"></span></span>
              <button class="btn btn-icon btn-ghost palette-btn" @click="showPalette = true" title="命令面板 (⌘K)"><Icon name="command" :size="16" /></button>
              <button class="btn btn-icon btn-ghost" @click="showRequests = true" title="项目申请"><Icon name="inbox" :size="16" /></button>
              <button v-if="isAdmin" class="btn btn-icon btn-ghost" @click="showModels = true" title="模型管理"><Icon name="cpu" :size="16" /></button>
              <button v-if="isAdmin" class="btn btn-icon btn-ghost" @click="showAdmin = true" title="用户与权限"><Icon name="shield" :size="16" /></button>
            </template>
            <button class="btn btn-icon btn-ghost" @click="toggleTheme" :title="theme === 'dark' ? '切换到浅色' : '切换到深色'"><Icon :name="theme === 'dark' ? 'sun' : 'moon'" :size="16" /></button>
            <div v-if="isAuthenticated" class="user">
              <span class="avatar">{{ (username || '?').slice(0, 1).toUpperCase() }}</span>
              <span class="u-name hide-sm">{{ username }}</span>
              <button class="btn btn-icon btn-ghost btn-sm" @click="logout" title="退出登录"><Icon name="log-out" :size="14" /></button>
            </div>
            <button v-else class="btn btn-primary btn-sm" @click="showLogin = true"><Icon name="lock" :size="14" />登录</button>
          </div>
        </div>
      </header>

      <main v-if="isAuthenticated" class="page">
        <div class="page-head">
          <h1>项目</h1>
          <p class="summary">
            <span v-if="counts.needs" class="s-warn">{{ counts.needs }} 个待你处理</span>
            <span>{{ counts.running }} 个运行中</span>
            <span>{{ counts.completed }} 个已完成</span>
            <span>共 {{ counts.total }} 次运行</span>
          </p>
        </div>

        <section v-if="needsYou.length" class="section">
          <h2 class="s-title warn"><Icon name="alert-triangle" :size="15" />需要你处理<span class="count">{{ needsYou.length }}</span></h2>
          <div class="list panel attention">
            <ProjectRow v-for="p in needsYou" :key="p.base_name" :project="p" @open="openProject" @action="onAction" />
          </div>
        </section>

        <section v-if="running.length" class="section">
          <h2 class="s-title"><span class="dot live"></span>运行中<span class="count">{{ running.length }}</span></h2>
          <div class="list panel">
            <ProjectRow v-for="p in running" :key="p.base_name" :project="p" @open="openProject" @action="onAction" />
          </div>
        </section>

        <section class="section">
          <div class="s-bar">
            <h2 class="s-title">全部题目<span class="count">{{ filteredArchives.length }}</span></h2>
            <div class="seg" role="tablist" aria-label="按状态筛选">
              <button v-for="f in filterChips" :key="f.key" role="tab" :aria-selected="statusFilter === f.key ? 'true' : 'false'" class="seg-b" :class="{ on: statusFilter === f.key }" @click="statusFilter = f.key">{{ f.label }}</button>
            </div>
          </div>
          <div v-if="loading" class="list panel">
            <div v-for="i in 4" :key="i" class="skel"></div>
          </div>
          <div v-else-if="filteredArchives.length" class="list panel">
            <template v-for="archive in filteredArchives" :key="archive.key">
              <ProblemGroup v-if="archive.run_count > 1" :archive="archive" @open="openProject" @action="onAction" />
              <ProjectRow v-else :project="archive.latest" @open="openProject" @action="onAction" />
            </template>
          </div>
          <p v-else-if="projects.length && !query.trim() && statusFilter === 'all'" class="rest-note">其余运行都列在上面。</p>
          <div v-else class="empty panel">
            <Icon name="inbox" :size="26" />
            <p>{{ query.trim() || statusFilter !== 'all' ? '没有匹配的项目' : '还没有项目' }}</p>
            <button v-if="!query.trim() && statusFilter === 'all'" class="btn btn-sm" @click="openNew">{{ isAdmin ? '新建项目' : '申请项目' }}</button>
          </div>
        </section>

        <section class="section">
          <div class="s-bar">
            <h2 class="s-title">展示论文<span class="count">{{ filteredShowcasePapers.length }}</span></h2>
            <div class="search small">
              <Icon name="search" :size="13" />
              <input v-model="showcaseQuery" class="search-in" placeholder="搜索论文…" spellcheck="false" />
              <button v-if="showcaseQuery" class="clr" @click="showcaseQuery = ''" title="清除"><Icon name="x" :size="12" /></button>
            </div>
          </div>
          <div v-if="showcaseLoading" class="list panel"><div v-for="i in 2" :key="i" class="skel"></div></div>
          <div v-else-if="filteredShowcasePapers.length" class="list panel">
            <ShowcasePaperCard v-for="paper in filteredShowcasePapers" :key="paper.base_name" :paper="paper" @open="openShowcasePaper" />
          </div>
          <div v-else class="empty panel">
            <Icon :name="showcaseError ? 'alert-triangle' : 'book-open'" :size="26" />
            <p>{{ showcaseError || (showcaseQuery ? '没有匹配的论文' : '暂无向你展示的论文') }}</p>
            <button v-if="showcaseError" class="btn btn-sm" @click="loadShowcase"><Icon name="refresh" :size="13" />重试</button>
          </div>
        </section>
      </main>

      <main v-else class="page">
        <div class="page-head">
          <h1>展示论文</h1>
          <p class="summary"><span>数学建模竞赛论文，可在线阅读与下载</span></p>
        </div>
        <section class="section">
          <div class="s-bar">
            <h2 class="s-title">全部论文<span class="count">{{ filteredShowcasePapers.length }}</span></h2>
            <div class="search small">
              <Icon name="search" :size="13" />
              <input v-model="showcaseQuery" class="search-in" placeholder="搜索论文…" spellcheck="false" />
              <button v-if="showcaseQuery" class="clr" @click="showcaseQuery = ''" title="清除"><Icon name="x" :size="12" /></button>
            </div>
          </div>
          <div v-if="showcaseLoading" class="list panel"><div v-for="i in 3" :key="i" class="skel"></div></div>
          <div v-else-if="filteredShowcasePapers.length" class="list panel">
            <ShowcasePaperCard v-for="paper in filteredShowcasePapers" :key="paper.base_name" :paper="paper" @open="openShowcasePaper" />
          </div>
          <div v-else class="empty panel">
            <Icon :name="showcaseError ? 'alert-triangle' : 'book-open'" :size="26" />
            <p>{{ showcaseError || (showcaseQuery ? '没有匹配的论文' : '暂无展示论文') }}</p>
            <button v-if="showcaseError" class="btn btn-sm" @click="loadShowcase"><Icon name="refresh" :size="13" />重试</button>
          </div>
        </section>
      </main>
    </div>

    <ProjectWorkspace v-if="isAuthenticated && selectedProject" :project="selectedProject" :is-admin="isAdmin" @close="closeWorkspace" @action="onAction" @refresh="fetchProjects" />
    <NewProjectModal v-if="isAuthenticated && showNew" :is-admin="isAdmin" @close="showNew = false" @project-created="onCreated" @project-requested="onRequested" />
    <AdminPanel v-if="isAuthenticated && showAdmin" @close="showAdmin = false" @changed="onAdminChanged" />
    <ProjectRequestsPanel v-if="isAuthenticated && showRequests" :admin="isAdmin" @close="showRequests = false" @changed="onAdminChanged" />
    <CommandPalette v-if="isAuthenticated && showPalette" :visible="showPalette" :projects="projects" @close="showPalette = false" @open-project="openByBase" @new-project="openNew" @toggle-theme="toggleTheme" />
    <ModelManager v-if="isAuthenticated && showModels && isAdmin" @close="showModels = false" @saved="() => {}" />
    <ShowcasePaperViewer v-if="selectedShowcasePaper" :paper="selectedShowcasePaper" @close="selectedShowcasePaper = null" />
  </template>
</template>

<script>
import { computed, defineAsyncComponent, h, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Icon from './components/Icon.vue'
import AsyncLoadError from './components/AsyncLoadError.vue'
import Toasts from './components/Toasts.vue'
import LoginForm from './components/LoginForm.vue'
import ProjectRow from './components/ProjectRow.vue'
import ProblemGroup from './components/ProblemGroup.vue'
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

// A failed lazy chunk must fail loudly instead of leaving the loading fallback
// on screen forever (Vue keeps the loading component when no error component is
// configured), so every async surface also gets this error state.
const AsyncOverlayError = {
  render: () => h(AsyncLoadError),
}

// Lazy-loaded overlay: only mounted when a project is opened. Pulls the whole
// ProjectWorkspace subtree (and KaTeX, via markdown.js) out of the initial bundle.
const ProjectWorkspace = defineAsyncComponent({
  loader: () => import('./components/ProjectWorkspace.vue'),
  loadingComponent: { render: () => h('div', { class: 'ws-overlay-loading' }, [h('div', { class: 'spinner' })]) },
  errorComponent: AsyncOverlayError,
  delay: 120,
})

const AsyncOverlayFallback = {
  render: () => h('div', { class: 'overlay-loading panel' }, [h('div', { class: 'spinner' })]),
}

const asyncOverlayOpts = { loadingComponent: AsyncOverlayFallback, errorComponent: AsyncOverlayError, delay: 120 }

const AsyncNewProjectModal = defineAsyncComponent({ loader: () => import('./components/NewProjectModal.vue'), ...asyncOverlayOpts })
const AsyncCommandPalette = defineAsyncComponent({ loader: () => import('./components/CommandPalette.vue'), ...asyncOverlayOpts })
const AsyncModelManager = defineAsyncComponent({ loader: () => import('./components/ModelManager.vue'), ...asyncOverlayOpts })
const AsyncAdminPanel = defineAsyncComponent({ loader: () => import('./components/AdminPanel.vue'), ...asyncOverlayOpts })
const AsyncProjectRequestsPanel = defineAsyncComponent({ loader: () => import('./components/ProjectRequestsPanel.vue'), ...asyncOverlayOpts })

export default {
  name: 'App',
  components: { Icon, Toasts, LoginForm, ProjectRow, ProblemGroup, ShowcasePaperCard, ShowcasePaperViewer, ProjectWorkspace, NewProjectModal: AsyncNewProjectModal, CommandPalette: AsyncCommandPalette, ModelManager: AsyncModelManager, AdminPanel: AsyncAdminPanel, ProjectRequestsPanel: AsyncProjectRequestsPanel },
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
      running,
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
      toasts.warn(`项目 ${baseName} 需要你的决策`, '人工待办')
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
        showcasePapers.value = await Showcase.list(isAuthenticated.value)
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
      await loadShowcase()
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
        } else {
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
    async function onAction(project, action, extra = {}) {
      try {
        await Projects.action(project.base_name, action, project.revision, extra.reason ?? null, extra.step ?? null)
        const labels = { pause: '已暂停', resume: '已恢复', kill: '已终止', grant_reopen: '已批准回退并恢复', retry_step: '已重试失败的步骤' }
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
      await Promise.all([refreshProjects(), loadShowcase()])
    }

    // ---- navigation / deep-link ----
    function openProjectFromCard(project, tab) {
      openProject(project)
      if (tab) router.replace({ name: 'project', params: { baseName: project.base_name }, query: { tab } }).catch(() => {})
    }
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
      if ((route.params.baseName || null) === baseName) return
      const target = baseName ? { name: 'project', params: { baseName } } : { name: 'dashboard' }
      router.replace(target).catch(() => {})
    })

    // Keep the palette warm without blocking anything: the chunk is fetched in
    // the background, and a failure is swallowed here instead of holding the
    // console behind a loading overlay (see lib/chunkRecovery.js for the
    // stale-cached-build case).
    function warmOverlays() {
      import('./components/CommandPalette.vue').catch(() => {})
    }

    onMounted(async () => {
      await checkAuth()
      if (isAuthenticated.value) void warmOverlays()
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
      needsYou, others, running, archives, filteredOthers, filteredArchives, counts,
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

/* ---- top bar ---- */
.topbar {
  position: sticky; top: 0; z-index: 90;
  background: color-mix(in srgb, var(--bg) 88%, transparent);
  -webkit-backdrop-filter: blur(8px);
  backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--line);
}
.topbar-in {
  max-width: var(--content-w); margin: 0 auto;
  height: var(--header-h); padding: 0 24px;
  display: flex; align-items: center; gap: 20px;
}
.brand { display: flex; align-items: center; gap: 9px; flex-shrink: 0; }
.mark {
  width: 26px; height: 26px; border-radius: 7px;
  background: var(--accent); color: var(--accent-ink);
  display: flex; align-items: center; justify-content: center;
}
.brand-name { font-weight: 600; font-size: 15px; }
.spacer { flex: 1; }

.search {
  flex: 1; max-width: 420px;
  display: flex; align-items: center; gap: 8px;
  padding: 0 10px; height: 34px;
  background: var(--surface); border: 1px solid var(--line-2); border-radius: var(--r);
  color: var(--ink-3);
}
.search:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-dim); }
.search.small { flex: 0 1 240px; height: 30px; }
.search-in { flex: 1; min-width: 0; border: 0; outline: 0; background: transparent; color: var(--ink); font: 13.5px var(--sans); }
.search-in::placeholder { color: var(--ink-3); }
.clr { border: 0; background: none; color: var(--ink-3); cursor: pointer; display: flex; padding: 2px; border-radius: 4px; }
.clr:hover { color: var(--ink); background: var(--panel-2); }

.tools { margin-left: auto; display: flex; align-items: center; gap: 4px; }
.tools > .btn-primary { margin-right: 6px; }
.conn { display: inline-flex; padding: 0 8px; }
.user { display: flex; align-items: center; gap: 8px; margin-left: 6px; padding-left: 10px; border-left: 1px solid var(--line); }
.avatar {
  width: 26px; height: 26px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  background: var(--panel-3); color: var(--ink-2); font-size: 12px; font-weight: 600;
}
.u-name { font-size: 13px; color: var(--ink-2); }

/* ---- page ---- */
.page { width: 100%; max-width: var(--content-w); margin: 0 auto; padding: 28px 24px 64px; }
.page-head { margin-bottom: 22px; }
.page-head h1 { font-size: 22px; }
.rest-note { color: var(--ink-3); font-size: 13px; padding: 4px 2px; }
.summary { display: flex; flex-wrap: wrap; gap: 4px 0; margin-top: 4px; color: var(--ink-2); font-size: 13.5px; }
.summary span + span::before { content: '·'; margin: 0 8px; color: var(--ink-3); }
.s-warn { color: var(--amber); font-weight: 500; }

.section { margin-top: 26px; }
.section:first-of-type { margin-top: 0; }
.s-bar { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 10px; }
.s-bar .s-title { margin-bottom: 0; }
.s-title { display: flex; align-items: center; gap: 8px; font-size: 14px; font-weight: 600; margin-bottom: 10px; color: var(--ink); }
.s-title.warn { color: var(--amber); }
.count { font-weight: 500; font-size: 12px; color: var(--ink-3); background: var(--panel-2); padding: 1px 7px; border-radius: 999px; }

.list { overflow: hidden; }
.list.attention { border-color: var(--amber-line); }

.seg { display: inline-flex; padding: 2px; gap: 2px; background: var(--panel-2); border-radius: var(--r); overflow-x: auto; max-width: 100%; }
.seg-b {
  border: 0; background: transparent; color: var(--ink-2);
  font: 500 12.5px var(--sans); padding: 4px 10px; border-radius: 6px; cursor: pointer; white-space: nowrap;
}
.seg-b:hover { color: var(--ink); }
.seg-b.on { background: var(--surface); color: var(--ink); box-shadow: 0 1px 2px rgba(0,0,0,0.08); }

.skel { height: 58px; border-top: 1px solid var(--line); background: linear-gradient(90deg, transparent, var(--panel-2), transparent); background-size: 200% 100%; animation: shimmer 1.4s linear infinite; }
.skel:first-child { border-top: 0; }
@keyframes shimmer { from { background-position: 200% 0; } to { background-position: -200% 0; } }

.empty { display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 36px 16px; color: var(--ink-3); text-align: center; }
.empty p { color: var(--ink-2); }

@media (max-width: 720px) {
  .topbar-in { flex-wrap: wrap; height: auto; padding: 8px 14px; gap: 8px 10px; }
  .search { order: 3; flex: 1 0 100%; max-width: none; }
  .conn, .palette-btn, .brand-name { display: none; }
  .hide-sm { display: none; }
  .page { padding: 20px 14px 48px; }
}
</style>
