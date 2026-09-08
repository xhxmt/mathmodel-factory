"""Normal binary producer -> guard -> dual receipt -> adoption -> review."""
import hashlib
import io
import json

import numpy as np
import pytest

from scripts.canonical_claims import accept
from scripts.judge_packet import build_packets
from scripts.numpy_evidence_view import render_bytes, render_file, verify_capsule
from scripts.packet_evidence import PacketEvidence
from scripts.solver_job_receipt import build_evidence, receipt_paths
from tests.support.normal_run import controlled_service
from tests.test_normal_run_packet_chain import packet_project, write, role_outputs, aggregate
from tests.test_aggregate_judges import _hard


def numpy_job(tmp_path, binary_role='input_npy', *, count=2, duplicate=False):
    service, project, backend = controlled_service(tmp_path)
    packet_project(project)
    inputs, outputs = (), ('results/values.json',)
    if binary_role in {'input_npy', 'input_output_npy'}:
        (project / 'data').mkdir(exist_ok=True)
        np.save(project / 'data/operand.npy', np.ones(count, dtype=np.int64))
        binary_path = 'data/operand.npy'
        inputs = (binary_path,)
        calculation = "samples = np.load('../data/operand.npy', allow_pickle=False)\n"
        if binary_role == 'input_output_npy':
            outputs += (binary_path,)
            calculation += "samples = samples + 1\nnp.save('../data/operand.npy', samples)\n"
    else:
        outputs += ('results/samples.npz',)
        binary_path = outputs[1]
        calculation = f"samples = np.ones({count}, dtype=np.int64)\nnp.savez('../results/samples.npz', samples=samples)\n"
        if duplicate:
            outputs += ('results/copy.npz',)
            calculation += "Path('../results/copy.npz').write_bytes(Path('../results/samples.npz').read_bytes())\n"
    script = write(project, 'models/03_solve.py',
        "import os\nimport numpy as np\nfrom pathlib import Path\n"
        "from factory_core.json_values import dumps\n" + calculation +
        "Path('../results/values.json').write_text(dumps({'estimate': samples.sum(), "
        "'provenance': {'job_id': os.environ['FACTORY_SOLVER_JOB_ID']}}))\n")
    job = service.submit_solver(project, runtime='python', script=script,
        max_time_seconds=10, input_paths=inputs, output_paths=outputs)
    job = service.solver_status(project, job['job_id'])
    assert job['status'] == 'completed', backend.result.stderr
    submitted, completed = receipt_paths(project / '.factory/solver_receipts', job['job_id'])
    assert build_evidence(project, submitted, completed)['receipt_ready']
    accept(project, 'Q1_ESTIMATE', 'results/values.json::estimate',
           completed.relative_to(project).as_posix(), [], 'Adopt the tiny normal NumPy result.')
    return project, binary_path, submitted


def binary_outputs(project, manifests, binary_path):
    paths = role_outputs(project, manifests)
    item = PacketEvidence(manifests['execution']['files']).resolve(binary_path)
    text, _ = render_file(project / item['path'])
    quote = next(line for line in text.splitlines() if '"index":[0]' in line)
    _hard(paths['execution'], 'execution', evidence=[dict(ref_id='array-element',
        claim='The computation uses the first element at its recorded position.',
        chunk_id=item['chunk_id'], quote=quote, finding='Exact decoded element.', severity='support')])
    return paths


@pytest.mark.parametrize('binary_role', ['input_npy', 'output_npz', 'input_output_npy'])
def test_native_numpy_adoption_reaches_grounded_review(tmp_path, binary_role):
    project, binary_path, submitted = numpy_job(tmp_path, binary_role)
    manifests = build_packets(project)
    manifest = manifests['execution']
    selected = PacketEvidence(manifest['files']).resolve(binary_path)
    chains = [r for r in manifest['completeness']['requirements'] if r['id'].startswith('solver_chain:')]
    assert chains and any(binary_path in r['paths'] for r in chains)
    assert all(not r.get('binding_error') for r in chains)
    assert manifest['completeness']['eligible'], selected
    assert selected['binary_review']['coverage'] == 'full'
    raw = (project / binary_path).read_bytes()
    assert selected['sha256'] == hashlib.sha256(raw).hexdigest()
    assert selected['size'] == len(raw)
    if binary_role == 'input_output_npy':
        snapshots = json.loads(submitted.read_text())['input_output_snapshots']
        snapshot_path = snapshots[0]['snapshot']['path']
        assert any(snapshot_path in r['paths'] for r in chains)
        before = PacketEvidence(manifest['files']).resolve(snapshot_path)
        assert before['status'] == 'included' and before['sha256'] != selected['sha256']
        text, _ = render_file(project / snapshot_path)
        assert '"value":"1"' in text
        assert '"value":"2"' in render_file(project / binary_path)[0]
    result = aggregate(project, binary_outputs(project, manifests, binary_path))
    assert result.status == 'PASS'
    assert result.evidence_grounding['execution']['valid']


def test_binary_aliases_share_one_verified_full_decoding(tmp_path):
    project, binary_path, _ = numpy_job(tmp_path, 'output_npz', duplicate=True)
    manifests = build_packets(project)
    evidence = PacketEvidence(manifests['execution']['files'])
    alias = evidence.by_path['results/samples.npz']
    assert alias['status'] == 'alias'
    assert evidence.complete(alias['path']) and evidence.complete('results/copy.npz')
    assert evidence.resolve(alias['path'])['path'] == 'results/copy.npz'
    assert aggregate(project, binary_outputs(project, manifests, binary_path)).status == 'PASS'


@pytest.mark.parametrize('limit', ['packet_budget', 'source_bytes', 'elements'])
def test_binary_limits_keep_required_paths_and_prevent_review_pass(tmp_path, monkeypatch, limit):
    project, binary_path, _ = numpy_job(tmp_path, count=20000 if limit == 'source_bytes' else 5000 if limit == 'elements' else 2)
    if limit == 'packet_budget':
        monkeypatch.setattr('scripts.judge_packet.EXECUTION_CONTEXT_BYTES', 500)
    manifests = build_packets(project)
    manifest = manifests['execution']
    selected = PacketEvidence(manifest['files']).by_path[binary_path]
    assert any(binary_path in r['paths'] for r in manifest['completeness']['requirements'])
    assert selected['status'] == 'omitted'
    assert selected['reason'] in {'context_byte_limit', 'numpy_source_byte_limit', 'numpy_shape_unsupported'}
    assert not manifest['completeness']['eligible']
    if limit != 'packet_budget':
        assert aggregate(project, role_outputs(project, manifests)).status == 'INDETERMINATE'


@pytest.mark.parametrize('array', [
    np.array([[0.1, -0.0], [float('inf'), float('nan')]], dtype='>f8'),
    np.asfortranarray(np.array([[1, 2], [3, 4]], dtype='<i2')),
    np.array([2**64 - 1], dtype='<u8'), np.array(0.25, dtype='<f2'),
    np.array([1+2j, -3-4j], dtype='>c16'), np.empty((0, 2), dtype='f4'),
    np.array([True, False]),
])
def test_full_numeric_decoding_matches_numpy_positions_and_scalar_bytes(array):
    source = io.BytesIO()
    np.save(source, array, allow_pickle=False)
    raw = source.getvalue()
    text, binding = render_bytes(raw, '.npy')
    rows = [json.loads(line) for line in text.splitlines()]
    assert rows[1]['shape'] == list(array.shape)
    assert len(rows[2:]) == array.size
    for row in rows[2:]:
        value = array[tuple(row['index'])]
        start = row['byte_offset']
        assert row['scalar_hex'] == raw[start:start + array.itemsize].hex()
        if array.dtype.kind in 'fc':
            expected = [repr(float(value.real)), repr(float(value.imag))] if array.dtype.kind == 'c' else [repr(float(value))]
            assert row['value'] == expected
        else:
            assert row['value'] == str(value)
    item = dict(binary_review=binding, sha256=hashlib.sha256(raw).hexdigest(), size=len(raw),
                included_sha256=hashlib.sha256(text.encode()).hexdigest(), included_bytes=len(text.encode()))
    verify_capsule(text, item)
    assert render_bytes(raw, '.npy') == (text, binding)


def test_compressed_npz_members_are_named_and_fully_decoded():
    source = io.BytesIO()
    np.savez_compressed(source, beta=np.array([3.5]), alpha=np.array([[1, 2]]))
    text, binding = render_bytes(source.getvalue(), '.npz')
    assert [row['member'] for row in binding['datasets']] == ['alpha.npy', 'beta.npy']
    assert '"index":[0,1]' in text and '"value":"2"' in text


@pytest.mark.parametrize('version', [(1, 0), (2, 0), (3, 0)])
def test_documented_npy_versions_decode_the_same_values(version):
    source = io.BytesIO()
    np.lib.format.write_array(source, np.array([1, 2]), version=version, allow_pickle=False)
    text, _ = render_bytes(source.getvalue(), '.npy')
    assert '"index":[0]' in text and '"value":"1"' in text
    assert '"index":[1]' in text and '"value":"2"' in text


@pytest.mark.parametrize('limit', ['expanded', 'members', 'elements', 'view'])
def test_normal_large_arrays_have_explicit_resource_limits(limit):
    source = io.BytesIO()
    suffix = '.npy'
    if limit == 'expanded':
        np.savez_compressed(source, samples=np.zeros(20000, dtype='i8'))
        suffix, reason = '.npz', 'numpy_archive_limit_or_duplicate'
    elif limit == 'members':
        np.savez(source, **{f'dataset_{i}': np.array([i]) for i in range(33)})
        suffix, reason = '.npz', 'numpy_archive_limit_or_duplicate'
    elif limit == 'elements':
        np.save(source, np.zeros((65, 65), dtype='u1'), allow_pickle=False)
        reason = 'numpy_element_limit'
    else:
        np.save(source, np.arange(2000, dtype='c16'), allow_pickle=False)
        reason = 'numpy_review_byte_limit'
    with pytest.raises(ValueError, match=reason):
        render_bytes(source.getvalue(), suffix)


@pytest.mark.parametrize('array', [np.array(['ordinary text']), np.array([(1, 2)], dtype=[('x', 'i4'), ('y', 'i4')])])
def test_other_normal_numpy_types_remain_explicitly_unsupported(array):
    source = io.BytesIO()
    np.save(source, array, allow_pickle=False)
    with pytest.raises(ValueError, match='numpy_dtype_unsupported'):
        render_bytes(source.getvalue(), '.npy')


def test_numpy_decoder_changes_invalidate_evaluator_implementation_identity(tmp_path):
    from scripts.submission_fingerprint import evaluator_contract_payload
    from pathlib import Path
    relative = 'scripts/numpy_evidence_view.py'
    original = Path(__file__).resolve().parents[1] / relative
    copy = write(tmp_path, relative, original.read_text())
    before = evaluator_contract_payload('demo', tmp_path)['implementation']
    copy.write_text(copy.read_text() + '\n# A changed local decoder revision.\n')
    after = evaluator_contract_payload('demo', tmp_path)['implementation']
    assert before[relative]['sha256'] == hashlib.sha256(original.read_bytes()).hexdigest()
    assert before[relative]['sha256'] != after[relative]['sha256']


def test_grounding_rechecks_the_decoding_instead_of_trusting_chunk_hashes(tmp_path):
    from scripts.evidence_grounding import validate_grounding_bytes
    project, binary_path, _ = numpy_job(tmp_path)
    manifests = build_packets(project)
    paths = binary_outputs(project, manifests, binary_path)
    manifest = manifests['execution']
    item = PacketEvidence(manifest['files']).resolve(binary_path)
    text, _ = render_file(project / binary_path)
    # A valid but noncanonical JSON rendering is an ordinary stale view. Even
    # when the outer packet hashes describe it, it is not this decoder's output.
    alternate = ''.join(json.dumps(json.loads(line), sort_keys=True) + '\n' for line in text.splitlines())
    context = (project / 'judge_packets/execution/context.txt').read_text().replace(text, alternate)
    digest = hashlib.sha256(alternate.encode()).hexdigest()
    item.update(included_sha256=digest, included_bytes=len(alternate.encode()))
    item['binary_review'].update(view_sha256=digest, view_size=len(alternate.encode()))
    manifest['context'].update(sha256=hashlib.sha256(context.encode()).hexdigest(), size=len(context.encode()))
    report = validate_grounding_bytes(role_output_bytes=paths['execution'].read_bytes(),
        manifest_bytes=json.dumps(manifest).encode(), context_bytes=context.encode(), role='execution')
    assert not report['valid']
    assert report['errors'][0]['code'] == 'BINARY_REVIEW_INVALID'
