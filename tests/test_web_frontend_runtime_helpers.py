import subprocess


def run_node(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "--input-type=module"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
        cwd="/home/tfisher/paper_factory/.worktrees/control-plane-refactor",
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
