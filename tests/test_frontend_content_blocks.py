import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_frontend_content_block_registry_and_artifact_paths():
    script = r"""
import assert from 'node:assert/strict'
import { artifactRequestFromBlock, blockWidget, normalizeContentBlocks } from './web/frontend/src/lib/contentBlocks.js'

assert.equal(blockWidget({ render_type: 'dag' }), 'dag')
assert.equal(blockWidget({ render_type: 'future_widget' }), 'unsupported')
assert.deepEqual(normalizeContentBlocks(null), [])
assert.equal(normalizeContentBlocks([{ id: 'x', label: 'X', render_type: 'notice' }])[0].id, 'x')
assert.equal(artifactRequestFromBlock({ content: { path: 'problem/problem_plan.json' } }).type, 'json')
assert.equal(artifactRequestFromBlock({ content: { path: '../secret' } }), null)
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
