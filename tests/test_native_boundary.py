"""Native-only entry points must reject removed runtimes before side effects."""
import ast
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from factory_core.domain import InvalidTransition
from factory_core.engine import FactoryEngine
from factory_core.native_boundary import NativeBoundaryError, require_native_project
from factory_core.service import FactoryService
from factory_core.storage import SQLiteStateStore

ROOT = Path(__file__).resolve().parents[1]


def _database_bytes(project):
    return {p.name: (p.stat().st_ino, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in (project / '.factory').glob('state.db*') if p.is_file()}


def test_experimental_wal_schema_is_rejected_without_source_mutation(tmp_path):
    project = tmp_path / 'project'
    (project / '.factory').mkdir(parents=True)
    database = project / '.factory/state.db'
    connection = sqlite3.connect(database)
    try:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_autocheckpoint=0')
        connection.execute('CREATE TABLE authority_projects(id TEXT)')
        connection.commit()
        before = _database_bytes(project)
        assert 'state.db-wal' in before
        with pytest.raises(NativeBoundaryError, match='NATIVE_WORKFLOW_REQUIRED'):
            require_native_project(project)
        with pytest.raises(NativeBoundaryError, match='NATIVE_WORKFLOW_REQUIRED'):
            SQLiteStateStore(project).load()
        assert _database_bytes(project) == before
    finally:
        connection.close()


def test_native_stage_dry_run_and_no_work_run_preserve_valid_runtime(tmp_path):
    service = FactoryService(tmp_path)
    initial, _ = service.create_project('demo', 'fixture', start=False)
    project = tmp_path / 'ongoing/demo'
    script = project / 'models/solve.py'
    script.parent.mkdir(exist_ok=True)
    script.write_text('raise AssertionError("dry run must not execute")\n')
    require_native_project(project)
    plan = service.submit_solver(project, runtime='python', script=script,
                                 max_time_seconds=10, dry_run=True)
    assert plan['dry_run'] and plan['backend'] == 'local'
    assert SQLiteStateStore(project).load().revision == initial.revision
    assert SQLiteStateStore(project).solver_jobs() == []
    state = service.run(project, max_steps=0)
    assert state.scheduler_generation == 'stage_v1'


@pytest.mark.parametrize('method', ['rollback_migration', 'rollback_stage_scheduler'])
def test_retired_rollback_cannot_mutate_native_project(tmp_path, method):
    service = FactoryService(tmp_path)
    before, _ = service.create_project('demo', 'fixture', start=False)
    with pytest.raises(InvalidTransition, match='retired'):
        getattr(service, method)('demo', expected_revision=before.revision)
    assert service.inspect('demo') == before


def test_retired_workflow_deactivation_cannot_mutate_native_project(tmp_path):
    service = FactoryService(tmp_path)
    before, _ = service.create_project('demo', 'fixture', start=False)
    engine = FactoryEngine(tmp_path / 'ongoing/demo')
    with pytest.raises(InvalidTransition, match='retired'):
        engine.deactivate(expected_revision=before.revision)
    assert service.inspect('demo') == before


def test_old_native_scheduler_requires_explicit_activation_before_execution(tmp_path):
    project = tmp_path / 'ongoing/demo'
    project.mkdir(parents=True)
    store = SQLiteStateStore(project)
    before = store.initialize(project_id='demo', project_type='modeling')
    service = FactoryService(tmp_path)
    for action in (service.start, service.run):
        with pytest.raises(InvalidTransition, match='STAGE_SCHEDULER_REQUIRED'):
            action(project)
    assert store.load() == before
    service.activate_stage_scheduler(project, expected_revision=before.revision)
    assert service.run(project, max_steps=0).scheduler_generation == 'stage_v1'


def test_historical_artifacts_cannot_trigger_cli_execution_or_solver(tmp_path):
    (tmp_path / 'checkpoint.md').write_text('- **Last completed step**: 16\n')
    script = tmp_path / 'solve.py'
    script.write_text('raise AssertionError("must not run")\n')
    before = {p.name for p in tmp_path.iterdir()}
    for argv in ([str(ROOT / 'run_paper.sh'), '--infer-step', str(tmp_path)],
                 [str(ROOT / 'solver_submit.sh'), '--type', 'python', str(script)]):
        result = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True, timeout=10)
        assert result.returncode != 0
        assert 'NATIVE_WORKFLOW_REQUIRED' in result.stderr
    assert {p.name for p in tmp_path.iterdir()} == before


def test_active_python_imports_do_not_depend_on_relocated_modules():
    manifest = json.loads((ROOT / 'docs/architecture/EXPERIMENTAL_CODE_SPLIT.json').read_text())
    removed = {x['path'][:-3].replace('/', '.') for x in manifest['relocated']
               if x['path'].endswith('.py')}
    for directory in ('factory_core', 'scripts', 'web/backend'):
        for path in (ROOT / directory).rglob('*.py'):
            package = path.relative_to(ROOT).with_suffix('').parts[:-1]
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    prefix = '.'.join(package[:len(package)-node.level+1]) if node.level else ''
                    module = '.'.join(filter(None, (prefix, node.module)))
                    modules = [module] + [module + '.' + a.name for a in node.names]
                else:
                    continue
                assert not removed.intersection(modules), (path, modules)


def test_parallel_native_reads_do_not_race_sqlite_wal_cleanup(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    service = FactoryService(tmp_path)
    initial, _ = service.create_project('demo', 'fixture', start=False)
    project = tmp_path / 'ongoing/demo'

    def read_many(_):
        for _ in range(20):
            assert SQLiteStateStore(project).load() == initial

    with ThreadPoolExecutor(max_workers=6) as workers:
        list(workers.map(read_many, range(6)))


def test_launcher_creates_controls_and_inspects_native_stage_project(tmp_path):
    import os

    environment = {**os.environ, 'FACTORY': str(tmp_path)}
    def launch(*args):
        result = subprocess.run([str(ROOT / 'launch_agents.sh'), *args],
                                env=environment, capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        return result.stdout

    launch('new', '--no-start', 'demo', 'fixture question')
    project = tmp_path / 'ongoing/demo'
    state = SQLiteStateStore(project).load()
    assert (state.control_mode, state.runtime_generation, state.scheduler_generation) == (
        'engine', 'native_v2', 'stage_v1')
    assert 'demo' in launch('status')
    launch('pause', 'demo')
    assert SQLiteStateStore(project).load().status.value == 'paused'
    launch('kill', 'demo')
    assert SQLiteStateStore(project).load().status.value == 'killed'
