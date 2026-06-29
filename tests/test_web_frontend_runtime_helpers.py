import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_node(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "--input-type=module"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
        cwd=REPO_ROOT,
    )


def test_authenticated_startup_survives_model_load_failure():
    result = run_node(
        """
import assert from 'node:assert/strict'
import { runAuthenticatedStartup, runLoginFlow } from './web/frontend/src/lib/appStartup.js'

let warned = 0
let wsConnected = 0

const loginSteps = []
await runLoginFlow({
  login(data) { loginSteps.push(`login:${data.username}`) },
  refreshProjects: async () => { loginSteps.push('refresh') },
  loadModels: async () => { throw new Error('models down') },
  connectWS: async () => { wsConnected += 1; loginSteps.push('ws') },
  onModelWarmupError: () => { warned += 1 },
}, { username: 'admin' })

const startupSteps = []
const ok = await runAuthenticatedStartup({
  bootstrap: async () => true,
  refreshProjects: async () => { startupSteps.push('refresh') },
  loadModels: async () => { throw new Error('models down') },
  connectWS: async () => { wsConnected += 1; startupSteps.push('ws') },
  onModelWarmupError: () => { warned += 1 },
})

await new Promise((resolve) => setTimeout(resolve, 0))

assert.equal(ok, true)
assert.deepEqual(loginSteps, ['login:admin', 'refresh', 'ws'])
assert.deepEqual(startupSteps, ['refresh', 'ws'])
assert.equal(wsConnected, 2)
assert.equal(warned, 2)
"""
    )

    assert result.returncode == 0, result.stderr


def test_authenticated_startup_stops_when_bootstrap_fails():
    result = run_node(
        """
import assert from 'node:assert/strict'
import { runAuthenticatedStartup } from './web/frontend/src/lib/appStartup.js'

const calls = []
const ok = await runAuthenticatedStartup({
  bootstrap: async () => false,
  refreshProjects: async () => { calls.push('refresh') },
  loadModels: async () => { calls.push('models') },
  connectWS: async () => { calls.push('ws') },
})

assert.equal(ok, false)
assert.deepEqual(calls, [])
"""
    )

    assert result.returncode == 0, result.stderr


def test_frontend_runtime_helpers_handle_bad_ws_messages_and_failed_status():
    result = run_node(
        """
import assert from 'node:assert/strict'
import { parseRealtimeMessage } from './web/frontend/src/lib/realtime.js'
import { statusLabel } from './web/frontend/src/lib/status.js'

assert.deepEqual(parseRealtimeMessage('{"type":"status_update"}'), { type: 'status_update' })
assert.equal(parseRealtimeMessage('not-json'), null)
assert.equal(statusLabel('failed'), '失败')
"""
    )

    assert result.returncode == 0, result.stderr


def test_markdown_renderer_blocks_scriptable_links_and_escapes_attributes():
    result = run_node(
        """
import assert from 'node:assert/strict'
import { renderMarkdown } from './web/frontend/src/lib/markdown.js'

const html = renderMarkdown('[bad](javascript:alert(1)) [quote](https://example.test/"onclick=alert(1))')

assert.equal(html.includes('javascript:'), false)
assert.equal(html.includes('onclick='), false)
assert.match(html, /href="https:\\/\\/example\\.test\\/%22/)
"""
    )

    assert result.returncode == 0, result.stderr


def test_frontend_contract_normalizers_shape_api_payloads():
    result = run_node(
        """
import assert from 'node:assert/strict'
import {
  normalizeArtifact,
  normalizeCloudConfig,
  normalizeProjectStatus,
  normalizeStepsPayload,
} from './web/frontend/src/lib/contracts.js'

assert.deepEqual(normalizeProjectStatus({ base_name: 'demo', is_running: 1 }), {
  base_name: 'demo',
  status: 'unknown',
  current_step: -1,
  progress_percent: 0,
  is_running: true,
  pid: null,
  consultation_pending: false,
  consultation_gate: null,
  last_updated: null,
})

assert.deepEqual(normalizeArtifact({ path: 'models/a.py', size: '42' }), {
  path: 'models/a.py',
  name: 'a.py',
  type: 'text',
  group: 'other',
  size: 42,
  mtime: null,
})

assert.equal(normalizeStepsPayload({ steps: [{ artifacts: [{ path: 'paper.md' }] }] }).steps[0].artifacts[0].name, 'paper.md')
assert.deepEqual(normalizeCloudConfig({ enabled: 1, solver_types: 'python,julia' }).solver_types, ['python', 'julia'])
"""
    )

    assert result.returncode == 0, result.stderr


def test_project_store_can_be_created_independently():
    result = run_node(
        """
import assert from 'node:assert/strict'
import { createProjectStore } from './web/frontend/src/composables/useProjects.js'

const api = {
  list: async () => [
    { base_name: 'a', status: 'running', current_step: 2, progress_percent: 20, is_running: true },
    { base_name: 'b', status: 'completed', current_step: 16, progress_percent: 100, consultation_pending: true },
  ],
}
const storeA = createProjectStore({ projectsApi: api })
const storeB = createProjectStore({ projectsApi: api })

await storeA.fetchProjects()
storeA.openByBase('a')
storeB.openByBase('b')

assert.equal(storeA.projects.value.length, 2)
assert.equal(storeA.counts.value.running, 1)
assert.equal(storeA.counts.value.needs, 1)
assert.equal(storeA.selectedBase.value, 'a')
assert.equal(storeB.selectedBase.value, 'b')
assert.notEqual(storeA.selectedBase, storeB.selectedBase)
"""
    )

    assert result.returncode == 0, result.stderr


def test_project_step_and_log_controllers_are_reusable_outside_components():
    result = run_node(
        """
import assert from 'node:assert/strict'
import { createProjectStepsController } from './web/frontend/src/composables/useProjectSteps.js'
import { createProjectLogsController } from './web/frontend/src/composables/useProjectLogs.js'

const stepsController = createProjectStepsController({
  projectsApi: {
    steps: async (base, signal) => ({
      current_step: 3,
      verdict: 'PASS',
      open_issues: 0,
      paper_available: true,
      editorial_gate: { ready: true, verdict: 'PASS' },
      steps: [{ artifacts: [{ path: `${base}/model.md`, size: 10 }] }],
    }),
  },
})
await stepsController.fetchSteps('demo')
assert.equal(stepsController.stepsData.value.current_step, 3)
assert.equal(stepsController.stepsFp(stepsController.stepsData.value).includes('PASS'), true)

const logsController = createProjectLogsController({
  projectsApi: {
    logs: async () => ({ file: 'runner.log', logs: ['one', 'two', ''] }),
  },
  schedule: false,
})
await logsController.fetchNow('demo')
await logsController.fetchNow('demo', true)
assert.equal(logsController.file.value, 'runner.log')
assert.deepEqual(logsController.lines.value.map((l) => l.text), ['one', 'two'])
"""
    )

    assert result.returncode == 0, result.stderr


def test_project_polling_controller_centralizes_timer_lifecycle():
    result = run_node(
        """
import assert from 'node:assert/strict'
import { createProjectPollingController } from './web/frontend/src/composables/useProjectPolling.js'

const intervals = []
const cleared = []
const events = {}
const scheduler = {
  setInterval(fn, ms) {
    intervals.push({ fn, ms })
    return intervals.length
  },
  clearInterval(id) { cleared.push(id) },
}
const documentRef = {
  hidden: false,
  addEventListener(name, fn) { events[name] = fn },
  removeEventListener(name, fn) {
    if (events[name] === fn) delete events[name]
  },
}
let ticks = 0
let hidden = 0
const polling = createProjectPollingController({ intervalMs: 25, scheduler, documentRef })
polling.startPolling(() => { ticks += 1 }, { shouldRun: () => true, onHidden: () => { hidden += 1 } })

assert.equal(intervals[0].ms, 25)
intervals[0].fn()
documentRef.hidden = true
events.visibilitychange()
polling.stopPolling()

assert.equal(ticks, 1)
assert.equal(hidden, 1)
assert.deepEqual(cleared, [1])
assert.equal(events.visibilitychange, undefined)
"""
    )

    assert result.returncode == 0, result.stderr


def test_frontend_router_defines_project_deep_links():
    result = run_node(
        """
import assert from 'node:assert/strict'
import { createDashboardRouter } from './web/frontend/src/router.js'

const router = createDashboardRouter()
assert.equal(router.resolve('/').matched.length, 1)
assert.equal(router.resolve('/p/demo-project').params.baseName, 'demo-project')
"""
    )

    assert result.returncode == 0, result.stderr


def test_workspace_ui_helpers_cover_dashboard_enhancements():
    result = run_node(
        """
import assert from 'node:assert/strict'
import {
  LOG_LEVELS,
  buildCloudTaskPanel,
  buildConsultationWorkflow,
  buildLogErrorContext,
  filterLogLines,
  priorityArtifacts,
  workspaceTabs,
} from './web/frontend/src/lib/workspaceUi.js'

const logLines = [
  { n: 1, text: 'STEP 5 start' },
  { n: 2, text: 'warning: fallback model used' },
  { n: 3, text: 'Traceback: solver failed' },
  { n: 4, text: 'completed cleanup' },
]
assert.deepEqual(LOG_LEVELS.map((l) => l.key), ['all', 'err', 'warn', 'info', 'ok'])
assert.deepEqual(filterLogLines(logLines, { query: 'solver', level: 'err' }).map((l) => l.n), [3])
assert.deepEqual(buildLogErrorContext(logLines, 1).map((l) => l.n), [2, 3, 4])

const tabs = workspaceTabs({
  consultationPending: true,
  diagnostics: { status: { reason_code: 'runner_failed' } },
  cloudEnabled: true,
})
assert.deepEqual(tabs.map((t) => t.key), ['overview', 'pipeline', 'logs', 'artifacts', 'diagnostics', 'consultation', 'cloud'])
assert.equal(tabs.find((t) => t.key === 'diagnostics').attention, true)

const artifacts = priorityArtifacts([
  { path: 'logs/runner.log', name: 'runner.log', type: 'text', group: 'diagnostics' },
  { path: 'solve_log.md', name: 'solve_log.md', type: 'markdown', group: 'solve' },
  { path: 'papers/final.pdf', name: 'final.pdf', type: 'pdf', group: 'paper' },
  { path: 'other.txt', name: 'other.txt', type: 'text', group: 'other' },
], 6)
assert.deepEqual(artifacts.map((a) => a.path), ['papers/final.pdf', 'solve_log.md', 'logs/runner.log'])

const workflow = buildConsultationWorkflow({
  content: 'Need choose method',
  key_files: ['review.md', 'solve_log.md'],
}, 'short answer')
assert.equal(workflow.ready, false)
assert.equal(workflow.checks.find((c) => c.key === 'length').ok, false)
assert.equal(workflow.evidence.length, 2)

assert.deepEqual(buildCloudTaskPanel(
  { available: true, region: 'asia-east1', service: 'solver', solvers: ['python'] },
  { enabled: true, solver_types: ['python', 'julia'], threshold_time: 300 },
).badges, ['Cloud Run 可用', '本项目已启用', 'python, julia'])
"""
    )

    assert result.returncode == 0, result.stderr
