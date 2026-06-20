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
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import Icon from './components/Icon.vue'
import Toasts from './components/Toasts.vue'
import LoginForm from './components/LoginForm.vue'
import ProjectCard from './components/ProjectCard.vue'
import ProjectWorkspace from './components/ProjectWorkspace.vue'
import NewProjectModal from './components/NewProjectModal.vue'
import CommandPalette from './components/CommandPalette.vue'
import ModelManager from './components/ModelManager.vue'
import { Projects, authMe, setUnauthorizedHandler } from './lib/api.js'
import { useTheme } from './composables/useTheme.js'
import { useToasts, notifyDesktop } from './composables/useToasts.js'

export default {
  name: 'App',
  components: { Icon, Toasts, LoginForm, ProjectCard, ProjectWorkspace, NewProjectModal, CommandPalette, ModelManager },
  setup() {
    const { theme, toggle: toggleTheme } = useTheme()
    const toasts = useToasts()

    const isAuthenticated = ref(false)
    const username = ref('')
    const projects = ref([])
    const loading = ref(true)
    const wsConnected = ref(false)
    const selectedBase = ref(null)
    const showNew = ref(false)
    const showPalette = ref(false)
    const showModels = ref(false)
    const query = ref('')
    const statusFilter = ref('all')

    let ws = null
    let reconnect = null
    let awaitingSeen = null // Set, null until seeded

    const filterChips = [
      { key: 'all', label: '全部' },
      { key: 'running', label: '运行中' },
      { key: 'completed', label: '已完成' },
      { key: 'paused', label: '已暂停' },
    ]

    const needsYou = computed(() => projects.value.filter((p) => p.consultation_pending))
    const others = computed(() => projects.value.filter((p) => !p.consultation_pending))
    const filteredOthers = computed(() => {
      const q = query.value.trim().toLowerCase()
      return others.value.filter((p) => {
        if (q && !p.base_name.toLowerCase().includes(q)) return false
        if (statusFilter.value === 'all') return true
        if (statusFilter.value === 'running') return p.is_running || p.status === 'running'
        return p.status === statusFilter.value
      })
    })
    const counts = computed(() => ({
      needs: needsYou.value.length,
      running: projects.value.filter((p) => p.is_running || p.status === 'running').length,
      completed: projects.value.filter((p) => p.status === 'completed').length,
      total: projects.value.length,
    }))
    const selectedProject = computed(() => projects.value.find((p) => p.base_name === selectedBase.value) || null)

    function applyProjects(list) {
      projects.value = list
      // detect newly-awaiting projects → notify (skip on first seed)
      const nowAwaiting = list.filter((p) => p.consultation_pending).map((p) => p.base_name)
      if (awaitingSeen === null) {
        awaitingSeen = new Set(nowAwaiting)
      } else {
        for (const b of nowAwaiting) {
          if (!awaitingSeen.has(b)) {
            toasts.warn(`项目 ${b} 需要你的决策`, '人工咨询')
            notifyDesktop('Paper Factory · 需要你决策', `${b} 已在关卡处暂停`)
          }
        }
        awaitingSeen = new Set(nowAwaiting)
      }
    }

    async function fetchProjects() {
      try {
        applyProjects(await Projects.list())
      } catch (e) {
        // surfaced elsewhere
      } finally {
        loading.value = false
      }
    }

    function connectWS() {
      const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      ws = new WebSocket(`${proto}//${window.location.host}/ws`)
      ws.onopen = () => { wsConnected.value = true }
      ws.onmessage = (ev) => {
        try {
          const d = JSON.parse(ev.data)
          if (d.type === 'status_update' && d.projects) applyProjects(d.projects)
          else fetchProjects()
        } catch (e) { /* ignore */ }
      }
      ws.onclose = () => { wsConnected.value = false; reconnect = setTimeout(connectWS, 3000) }
      ws.onerror = () => { try { ws.close() } catch (e) { /* */ } }
    }

    // ---- auth ----
    function onLogin(data) {
      isAuthenticated.value = true
      username.value = data.username
      loading.value = true
      fetchProjects()
      connectWS()
    }
    function logout() {
      localStorage.removeItem('access_token')
      localStorage.removeItem('username')
      isAuthenticated.value = false
      username.value = ''
      selectedBase.value = null
      if (ws) { try { ws.close() } catch (e) { /* */ } }
    }
    setUnauthorizedHandler(logout)

    async function checkAuth() {
      const token = localStorage.getItem('access_token')
      const u = localStorage.getItem('username')
      if (!token || !u) { loading.value = false; return }
      try {
        await authMe()
        isAuthenticated.value = true
        username.value = u
        fetchProjects()
        connectWS()
      } catch (e) {
        logout(); loading.value = false
      }
    }

    // ---- actions ----
    async function onAction(project, action) {
      try {
        await Projects.action(project.base_name, action)
        const labels = { pause: '已暂停', resume: '已恢复', kill: '已终止' }
        toasts.success(`${project.base_name} ${labels[action] || action}`)
        if (action === 'kill') selectedBase.value = null
        await fetchProjects()
      } catch (e) {
        toasts.error(e.response?.data?.detail || '操作失败')
      }
    }
    function onCreated(result) {
      toasts.success(`项目 ${result.base_name} 已创建`)
      fetchProjects()
    }

    // ---- navigation / deep-link ----
    function openProject(p) { selectedBase.value = p.base_name }
    function openByBase(b) { selectedBase.value = b; showPalette.value = false }
    function closeWorkspace() { selectedBase.value = null }
    function openNew() { showNew.value = true; showPalette.value = false }
    function jumpNeeds() { if (needsYou.value.length) openProject(needsYou.value[0]) }

    function syncHash() {
      const want = selectedBase.value ? `#/p/${selectedBase.value}` : ''
      if (location.hash !== want) {
        if (want) location.hash = want
        else history.replaceState(null, '', location.pathname + location.search)
      }
    }
    function readHash() {
      const m = location.hash.match(/^#\/p\/(.+)$/)
      selectedBase.value = m ? decodeURIComponent(m[1]) : null
    }

    function onKey(e) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault(); showPalette.value = !showPalette.value
      }
    }

    // keep the URL hash in sync with the selected project (deep-linkable)
    watch(selectedBase, syncHash)

    onMounted(async () => {
      readHash()
      await checkAuth()
      window.addEventListener('hashchange', readHash)
      window.addEventListener('keydown', onKey)
    })
    onUnmounted(() => {
      if (ws) ws.close()
      if (reconnect) clearTimeout(reconnect)
      window.removeEventListener('hashchange', readHash)
      window.removeEventListener('keydown', onKey)
    })

    return {
      theme, toggleTheme,
      isAuthenticated, username, projects, loading, wsConnected,
      selectedBase, selectedProject, showNew, showPalette, showModels, query, statusFilter, filterChips,
      needsYou, others, filteredOthers, counts,
      onLogin, logout, onAction, onCreated,
      openProject, openByBase, closeWorkspace, openNew, jumpNeeds, fetchProjects,
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
