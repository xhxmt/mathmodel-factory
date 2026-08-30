from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "web/frontend/src/lib/projectSnapshotUi.js"
NODE_IMPORTS = r"""
import assert from 'node:assert/strict'
import {
  PROJECT_SNAPSHOT_VIEW_STATES,
  buildProjectSnapshotViewModel,
} from './web/frontend/src/lib/projectSnapshotUi.js'
"""


def run_node(assertions: str) -> None:
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", NODE_IMPORTS + assertions],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""


def test_exports_all_seven_distinct_view_states():
    run_node(
        r"""
assert.deepEqual(Object.values(PROJECT_SNAPSHOT_VIEW_STATES), [
  'loading',
  'ready',
  'empty',
  'legacy_unavailable',
  'auth_error',
  'api_error',
  'unknown',
])

for (const state of [
  'loading',
  'empty',
  'legacy_unavailable',
  'auth_error',
  'api_error',
  'unknown',
]) {
  const view = buildProjectSnapshotViewModel({
    state,
    sections: [{ key: 'must-not-render', data: { value: 1 } }],
    actions: [{ id: 'must-not-render', severity: 'critical' }],
  })
  assert.equal(view.state, state)
  assert.deepEqual(view.sections, [])
  assert.equal(view.actionCenter.mode, 'status')
  assert.equal(view.actionCenter.clear, false)
  assert.equal(view.actionCenter.interactive, false)
  assert.deepEqual(view.actionCenter.actions, [])
  assert.equal(Object.isFrozen(view), true)
  assert.equal(Object.isFrozen(view.status), true)
  assert.equal(Object.isFrozen(view.actionCenter), true)
}
"""
    )


def test_ready_without_actions_is_clear_but_empty_state_is_not_clear():
    run_node(
        r"""
const ready = buildProjectSnapshotViewModel({
  state: 'ready',
  snapshot_id: 'snapshot-1',
  revision: 0,
  sections: [],
  actions: [],
})
assert.equal(ready.state, 'ready')
assert.equal(ready.actionCenter.mode, 'clear')
assert.equal(ready.actionCenter.clear, true)
assert.equal(ready.actionCenter.interactive, false)

const empty = buildProjectSnapshotViewModel({ state: 'empty' })
assert.equal(empty.state, 'empty')
assert.equal(empty.actionCenter.mode, 'status')
assert.equal(empty.actionCenter.clear, false)
"""
    )


def test_ready_propagates_one_coordinate_to_every_projection():
    run_node(
        r"""
const view = buildProjectSnapshotViewModel({
  state: 'ready',
  coordinate: { snapshot_id: 'snapshot-7', revision: 7 },
  sections: [
    {
      key: 'summary',
      coordinate: { snapshot_id: 'snapshot-7', revision: 7 },
      data: { status: 'ok' },
    },
    { key: 'evidence', snapshot_id: 'snapshot-7', revision: 7, data: [] },
  ],
  actions: [{ id: 'review', severity: 'warning' }],
})

assert.deepEqual(view.coordinate, { snapshot_id: 'snapshot-7', revision: 7 })
assert.equal(view.snapshot_id, 'snapshot-7')
assert.equal(view.revision, 7)
assert.equal(view.sections[0].coordinate, view.coordinate)
assert.equal(view.sections[1].coordinate, view.coordinate)
assert.equal(view.actionCenter.coordinate, view.coordinate)
assert.equal(view.sections[0].snapshot_id, view.snapshot_id)
assert.equal(view.sections[1].revision, view.revision)
assert.equal(view.actionCenter.snapshot_id, view.snapshot_id)
assert.equal(view.actionCenter.revision, view.revision)
assert.equal(view.actionCenter.actions[0].coordinate, view.coordinate)
assert.equal(view.actionCenter.actions[0].snapshot_id, view.snapshot_id)
assert.equal(view.actionCenter.actions[0].revision, view.revision)
"""
    )


def test_mixed_action_coordinate_fails_closed_and_suppresses_action_center():
    run_node(
        r"""
for (const action of [
  { id: 'mixed', coordinate: { snapshot_id: 'other', revision: 4 } },
  { id: 'partial', snapshot_id: 'snapshot-4' },
  { id: 'revision', snapshot_id: 'snapshot-4', revision: 5 },
]) {
  const view = buildProjectSnapshotViewModel({
    state: 'ready',
    snapshot_id: 'snapshot-4',
    revision: 4,
    sections: [],
    actions: [action],
  })
  assert.equal(view.state, 'unknown')
  assert.equal(view.reason_code, 'MIXED_SNAPSHOT_COORDINATE')
  assert.equal(view.actionCenter.clear, false)
  assert.equal(view.actionCenter.interactive, false)
  assert.deepEqual(view.actionCenter.actions, [])
}
"""
    )


def test_missing_or_invalid_page_coordinate_fails_closed():
    run_node(
        r"""
for (const input of [
  { state: 'ready', snapshot_id: 'snapshot-1' },
  { state: 'ready', snapshot_id: '', revision: 1 },
  { state: 'ready', snapshot_id: 'snapshot-1', revision: -1 },
  { state: 'ready', snapshot_id: 'snapshot-1', revision: Number.MAX_SAFE_INTEGER + 1 },
  {
    state: 'ready',
    snapshot_id: 'snapshot-1',
    revision: 1,
    coordinate: { snapshot_id: 'snapshot-2', revision: 1 },
  },
]) {
  const view = buildProjectSnapshotViewModel(input)
  assert.equal(view.state, 'unknown')
  assert.equal(view.reason_code, 'INVALID_SNAPSHOT_COORDINATE')
  assert.deepEqual(view.sections, [])
  assert.equal(view.actionCenter.mode, 'status')
  assert.equal(view.actionCenter.clear, false)
}
"""
    )


def test_mixed_section_coordinate_fails_closed_and_suppresses_sections():
    run_node(
        r"""
for (const section of [
  {
    key: 'summary',
    coordinate: { snapshot_id: 'other-snapshot', revision: 4 },
    data: {},
  },
  { key: 'summary', snapshot_id: 'snapshot-4', revision: 5, data: {} },
  { key: 'summary', snapshot_id: 'snapshot-4', data: {} },
]) {
  const view = buildProjectSnapshotViewModel({
    state: 'ready',
    snapshot_id: 'snapshot-4',
    revision: 4,
    sections: [section, { key: 'later', data: { should: 'be suppressed' } }],
  })
  assert.equal(view.state, 'unknown')
  assert.equal(view.reason_code, 'MIXED_SNAPSHOT_COORDINATE')
  assert.deepEqual(view.sections, [])
  assert.equal(view.actionCenter.clear, false)
}
"""
    )


def test_duplicate_or_invalid_section_identity_fails_closed():
    run_node(
        r"""
for (const sections of [
  [{ key: '', data: {} }],
  [{ key: 'same', data: {} }, { key: ' same ', data: {} }],
  [{ data: {} }],
  [null],
]) {
  const view = buildProjectSnapshotViewModel({
    state: 'ready',
    snapshot_id: 'snapshot-identity',
    revision: 2,
    sections,
  })
  assert.equal(view.state, 'unknown')
  assert.equal(view.reason_code, 'INVALID_SECTION_IDENTITY')
  assert.deepEqual(view.sections, [])
}
"""
    )


def test_missing_or_uncloneable_section_data_fails_closed():
    run_node(
        r"""
for (const section of [
  { key: 'missing' },
  { key: 'function', data: { callback() {} } },
  { key: 'weak-map', data: new WeakMap() },
]) {
  const view = buildProjectSnapshotViewModel({
    state: 'ready',
    snapshot_id: 'snapshot-data',
    revision: 3,
    sections: [section],
  })
  assert.equal(view.state, 'unknown')
  assert.equal(view.reason_code, 'INVALID_SECTION_DATA')
  assert.deepEqual(view.sections, [])
  assert.equal(view.actionCenter.mode, 'status')
}
"""
    )


def test_unique_actions_are_normalized_and_sorted_deterministically():
    run_node(
        r"""
const view = buildProjectSnapshotViewModel({
  state: 'ready',
  snapshot_id: 'snapshot-actions',
  revision: 8,
  sections: [],
  actions: [
    { id: 'b-warning', severity: 'warning', title: 'first wins' },
    { id: 'z-expired', severity: 'expired' },
    { id: 'c-critical', severity: 'critical' },
    { id: 'a-warning', severity: 'warning' },
    { id: 'g-guarded', severity: 'guarded' },
    { id: 'i-future', severity: 'future-severity' },
  ],
})

assert.equal(view.actionCenter.mode, 'actions')
assert.equal(view.actionCenter.clear, false)
assert.equal(view.actionCenter.interactive, true)
assert.deepEqual(
  view.actionCenter.actions.map((action) => action.id),
  ['z-expired', 'c-critical', 'a-warning', 'b-warning', 'g-guarded', 'i-future'],
)
assert.equal(view.actionCenter.actions[3].title, 'first wins')
assert.equal(view.actionCenter.actions[5].severity, 'info')
"""
    )


def test_duplicate_or_invalid_action_identity_fails_closed():
    run_node(
        r"""
for (const actions of [
  [{ id: 'same' }, { id: ' same ' }],
  [{ id: ' same ' }, { id: 'same', severity: 'critical' }],
  [{ id: ' ' }],
  [{}],
  [null],
]) {
  const view = buildProjectSnapshotViewModel({
    state: 'ready',
    snapshot_id: 'snapshot-actions-invalid',
    revision: 9,
    sections: [],
    actions,
  })
  assert.equal(view.state, 'unknown')
  assert.equal(view.reason_code, 'INVALID_ACTION_DATA')
  assert.equal(view.actionCenter.mode, 'status')
  assert.equal(view.actionCenter.clear, false)
  assert.equal(view.actionCenter.interactive, false)
  assert.deepEqual(view.actionCenter.actions, [])
}
"""
    )


def test_future_state_projects_unknown_without_clear():
    run_node(
        r"""
const view = buildProjectSnapshotViewModel({
  state: 'future_snapshot_state',
  snapshot_id: 'snapshot-future',
  revision: 9,
  sections: [{ key: 'unsafe', data: { visible: true } }],
  actions: [],
})
assert.equal(view.state, 'unknown')
assert.equal(view.reason_code, 'UNKNOWN_SNAPSHOT_STATE')
assert.deepEqual(view.sections, [])
assert.equal(view.actionCenter.mode, 'status')
assert.equal(view.actionCenter.clear, false)
assert.equal(view.actionCenter.interactive, false)
"""
    )


def test_throwing_input_object_fails_closed_without_escaping():
    run_node(
        r"""
const hostile = new Proxy({}, {
  get() { throw new Error('hostile getter') },
})
const view = buildProjectSnapshotViewModel(hostile)
assert.equal(view.state, 'unknown')
assert.equal(view.reason_code, 'INVALID_SNAPSHOT_DATA')
assert.deepEqual(view.sections, [])
assert.equal(view.actionCenter.clear, false)
assert.equal(view.actionCenter.interactive, false)
"""
    )


def test_view_model_is_deeply_frozen_without_freezing_or_mutating_callers():
    run_node(
        r"""
const callerData = { nested: { values: [1, 2] } }
const callerAction = { id: 'inspect', severity: 'info', payload: { tab: 'evidence' } }
const view = buildProjectSnapshotViewModel({
  state: 'ready',
  snapshot_id: 'snapshot-frozen',
  revision: 10,
  sections: [{ key: 'summary', data: callerData }],
  actions: [callerAction],
})

assert.notEqual(view.sections[0].data, callerData)
assert.notEqual(view.actionCenter.actions[0], callerAction)
assert.equal(Object.isFrozen(callerData), false)
assert.equal(Object.isFrozen(callerData.nested), false)
assert.equal(Object.isFrozen(callerAction), false)
assert.equal(Object.isFrozen(view), true)
assert.equal(Object.isFrozen(view.coordinate), true)
assert.equal(Object.isFrozen(view.sections), true)
assert.equal(Object.isFrozen(view.sections[0]), true)
assert.equal(Object.isFrozen(view.sections[0].data.nested), true)
assert.equal(Object.isFrozen(view.sections[0].data.nested.values), true)
assert.equal(Object.isFrozen(view.actionCenter.actions[0].payload), true)
assert.throws(() => view.sections[0].data.nested.values.push(3), TypeError)
assert.throws(() => { view.actionCenter.mode = 'clear' }, TypeError)

callerData.nested.values.push(3)
callerAction.payload.tab = 'logs'
assert.deepEqual(view.sections[0].data.nested.values, [1, 2])
assert.equal(view.actionCenter.actions[0].payload.tab, 'evidence')
"""
    )


def test_default_off_frontend_is_lazy_and_cli_does_not_import_phase6_store():
    production_files = []
    for root in (
        REPO_ROOT / "web/frontend/src",
        REPO_ROOT / "web/backend",
        REPO_ROOT / "factory_core",
    ):
        production_files.extend(
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix in {".js", ".vue", ".py", ".sh"}
            and path != MODULE_PATH
        )
    production_files.extend(
        path
        for path in (REPO_ROOT / "launch_agents.sh", REPO_ROOT / "run_paper.sh")
        if path.is_file()
    )

    references = [
        str(path.relative_to(REPO_ROOT))
        for path in production_files
        if "projectSnapshotUi" in path.read_text(encoding="utf-8")
    ]
    assert sorted(references) == [
        "web/frontend/src/composables/usePhase6ProjectSnapshot.js",
        "web/frontend/src/lib/phase6SnapshotProjection.js",
    ]

    workspace = (
        REPO_ROOT / "web/frontend/src/components/ProjectWorkspace.vue"
    ).read_text(encoding="utf-8")
    assert "virtual:optional-workspace-snapshot" in workspace
    assert "OptionalWorkspaceExtensionPanel" in workspace
    assert "Phase6ProjectSnapshotPanel" not in workspace
    assert "phase6-snapshot" not in workspace

    disabled_extension = (
        REPO_ROOT
        / "web/frontend/src/lib/optionalSnapshotFeature.disabled.js"
    ).read_text(encoding="utf-8")
    enabled_extension = (
        REPO_ROOT
        / "web/frontend/src/lib/optionalSnapshotFeature.enabled.js"
    ).read_text(encoding="utf-8")
    vite_config = (REPO_ROOT / "web/frontend/vite.config.js").read_text(
        encoding="utf-8"
    )
    assert "Phase6ProjectSnapshotPanel" not in disabled_extension
    assert "phase6" not in disabled_extension.lower()
    assert "import('../components/Phase6ProjectSnapshotPanel.vue')" in enabled_extension
    assert "VITE_PHASE6_FULL_SHADOW_ENABLED === 'true'" in vite_config

    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import factory_core.cli,sys; "
                "print('phase6-not-loaded' if "
                "'factory_core.phase6_snapshot_grants' not in sys.modules "
                "else 'phase6-loaded')"
            ),
        ],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout == "phase6-not-loaded\n"
