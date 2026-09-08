"""Offline regressions for complete materials, delivery, grounding and reuse."""
import base64
from dataclasses import replace
import io
import json
from pathlib import Path
import shutil
import zipfile

import pytest

from scripts import document_evidence_view as doc
from scripts.judge_packet import build_packets, packet_payloads
from scripts.packet_evidence import PacketEvidence
from scripts.evidence_grounding import validate_grounding, validate_grounding_bytes
from tests.test_normal_run_packet_chain import packet_project, role_outputs, aggregate, write
from tests.test_aggregate_judges import _hard


def workbook(rows=3, *, extra=None):
    output = io.BytesIO()
    ns = doc.NS['s']
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('xl/workbook.xml', f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<workbookPr date1904="1"/><sheets><sheet name="测量数据" state="hidden" sheetId="1" r:id="r1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml', f'<worksheet xmlns="{ns}"><sheetData>' + ''.join(
            f'<row r="{r}" spans="1:2"><c r="A{r}"><v>{r}.123456789012345</v></c>'
            f'<c r="B{r}"><v>{r}.987654321098765</v></c></row>' for r in range(1, rows + 1)) +
            '<row r="1000000" hidden="1"><c r="C1000000" s="2"><f>SUM(A1:A3)</f><v>6.370370367037035</v></c></row>'
            '</sheetData><mergeCells><mergeCell ref="D1:E1"/></mergeCells></worksheet>')
        if extra:
            z.writestr(*extra)
    return output.getvalue()


def pdf_bytes():
    # Two real pages with distinct visible labels, using a built-in PDF font.
    contents = [b'BT /F1 24 Tf 40 120 Td (Page one) Tj ET', b'BT /F1 24 Tf 40 120 Td (Page two) Tj ET']
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 240 180] /Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 240 180] /Resources << /Font << /F1 5 0 R >> >> /Contents 7 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    objects += [f'<< /Length {len(c)} >>\nstream\n'.encode() + c + b'\nendstream' for c in contents]
    result = b'%PDF-1.4\n'
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result += f'{i} 0 obj\n'.encode() + obj + b'\nendobj\n'
    xref = len(result)
    result += f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode()
    result += b''.join(f'{o:010d} 00000 n \n'.encode() for o in offsets)
    return result + f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()


def require_pdf():
    if not all(shutil.which(t) for t in ('pdfinfo', 'pdftotext', 'pdftoppm')):
        pytest.skip('native PDF tools are unavailable')


def document_project(project, *, pdf=False, rows=3, duplicate=False):
    packet_project(project)
    materials = {'data/observations.xlsx': workbook(rows)}
    if pdf:
        require_pdf()
        materials['results/plots.pdf'] = pdf_bytes()
    if duplicate:
        materials['data/copy.xlsx'] = materials['data/observations.xlsx']
    registry = json.loads((project / 'claim_registry.json').read_text())
    for relative, data in materials.items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        registry['claims'][0]['artifacts'].append({'path': relative, 'roles': ['execution']})
    write(project, 'claim_registry.json', json.dumps(registry))
    return build_packets(project)


def test_xlsx_every_cell_formula_hidden_sheet_and_raw_binding(tmp_path):
    manifests = document_project(tmp_path, rows=7470, duplicate=True)
    m = manifests['execution']
    evidence = PacketEvidence(m['files'])
    item = evidence.resolve('data/observations.xlsx')
    assert evidence.complete('data/observations.xlsx') and evidence.complete('data/copy.xlsx')
    b = item['document_review']
    assert b['cell_count'] == 14941
    assert b['sheets'][0]['state'] == 'hidden'
    text, details = doc.xlsx_view((tmp_path / item['path']).read_bytes())
    assert 'ROW 7470\tA=7470.123456789012345\tB=7470.987654321098765' in text
    assert 'SUM(A1:A3)' in text and 'date1904' in text and 'D1:E1' in text
    assert b['source_sha256'] == doc.sha((tmp_path / item['path']).read_bytes())
    doc.verify_view(text, item, lambda r: doc.read_asset(tmp_path, r))
    assert m['completeness']['eligible']
    paths = role_outputs(tmp_path, manifests)
    _hard(paths['execution'], 'execution', evidence=[dict(ref_id='last-row', claim='The final data row is present.',
        chunk_id=item['chunk_id'], quote='ROW 7470\tA=7470.123456789012345\tB=7470.987654321098765',
        finding='Complete numeric row.', severity='support')])
    assert aggregate(tmp_path, paths).status == 'PASS'
    packet = tmp_path / 'judge_packets/execution'
    assets = {r: doc.read_asset(tmp_path, r) for r in doc.manifest_assets(m)}
    assert validate_grounding_bytes(paths['execution'].read_bytes(), (packet/'manifest.json').read_bytes(),
        (packet/'context.txt').read_bytes(), role='execution', assets=assets)['valid']


def test_xlsx_redecodes_source_even_if_outer_hashes_match(tmp_path):
    manifests = document_project(tmp_path)
    item = PacketEvidence(manifests['execution']['files']).resolve('data/observations.xlsx')
    text, _ = doc.xlsx_view((tmp_path/item['path']).read_bytes())
    changed = text.replace('ROW 3\tA=3.123456789012345\tB=3.987654321098765\n', '')
    item.update(included_sha256=doc.sha(changed.encode()), included_bytes=len(changed.encode()))
    item['document_review'].update(view_sha256=item['included_sha256'], view_bytes=item['included_bytes'])
    with pytest.raises(ValueError, match='document_view_mismatch'):
        doc.verify_view(changed, item, lambda r: doc.read_asset(tmp_path, r))


@pytest.mark.parametrize('limit,reason', [('DOCUMENT_CONTEXT_BYTES','document_context_byte_limit'), ('MAX_ASSET_BYTES','document_asset_byte_limit')])
def test_required_document_limits_do_not_remove_requirements(tmp_path, monkeypatch, limit, reason):
    monkeypatch.setattr('scripts.judge_packet.' + limit, 100)
    m = document_project(tmp_path)['execution']
    item = PacketEvidence(m['files']).by_path['data/observations.xlsx']
    assert item['status'] == 'omitted' and item['reason'] == reason
    assert not m['completeness']['eligible']
    assert any(item['path'] in r['paths'] for r in m['completeness']['requirements'])


@pytest.mark.parametrize('extra', [('xl/drawings/drawing1.xml','<drawing/>'), ('xl/externalLinks/externalLink1.xml','<link/>')])
def test_unsupported_workbook_content_is_explicit(extra):
    with pytest.raises(ValueError, match='xlsx_unsupported_member'):
        doc.xlsx_view(workbook(extra=extra))


def test_corrupt_xlsx_is_an_explicit_material_failure(tmp_path):
    document_project(tmp_path)
    (tmp_path/'data/observations.xlsx').write_bytes(b'not a zip')
    m = build_packets(tmp_path)['execution']
    item = PacketEvidence(m['files']).by_path['data/observations.xlsx']
    assert item['reason'] == 'xlsx_invalid_zip' and not m['completeness']['eligible']


def test_pdf_pages_reach_native_grounding_and_forged_page_map_fails(tmp_path):
    manifests = document_project(tmp_path, pdf=True)
    item = PacketEvidence(manifests['execution']['files']).resolve('results/plots.pdf')
    assert item['document_review']['page_count'] == 2
    images = doc.image_records(manifests['execution'])
    assert [im['page'] for im in images] == [1,2]
    paths = role_outputs(tmp_path, manifests)
    quote = next(line for line in doc.pdf_text(item['document_review']).splitlines() if line.startswith('PAGE 2 IMAGE '))
    _hard(paths['execution'], 'execution', evidence=[dict(ref_id='page-two', claim='Page two is present.',
        chunk_id=item['chunk_id'], quote=quote, finding='Bound rendered page.', severity='support')])
    assert aggregate(tmp_path, paths).status == 'PASS'
    assert packet_payloads(tmp_path)['execution']['manifest'] == manifests['execution']
    b = item['document_review']
    b['pages'][1]['text'] = 'Text from a different PDF.'
    text = doc.pdf_text(b)
    item.update(included_sha256=doc.sha(text.encode()), included_bytes=len(text.encode()))
    b.update(view_sha256=item['included_sha256'], view_bytes=item['included_bytes'])
    with pytest.raises(ValueError, match='pdf_source_derivation_mismatch'):
        doc.verify_view(text, item, lambda r: doc.read_asset(tmp_path, r), pdf_verifier=doc.verify_pdf_source)


def test_api_inlines_full_large_packet_and_bounded_overflow_fails(tmp_path, monkeypatch):
    from scripts.api_agent_run import build_effective_prompt
    manifests = document_project(tmp_path, rows=7470)
    path = 'judge_packets/execution/context.txt'
    original = (tmp_path/path).read_text()
    assert len(original.encode()) > 200_000
    prompt, records = build_effective_prompt(tmp_path, 'Audit.', [path], 'judge_outputs/execution.md')
    assert original in prompt and all(r['status'] == 'included' for r in records)
    monkeypatch.setattr('scripts.api_agent_run._MAX_JUDGE_INPUT_BYTES', 200_000)
    with pytest.raises(ValueError, match='byte limit'):
        build_effective_prompt(tmp_path, 'Audit.', [path], 'judge_outputs/execution.md')


def test_codex_attaches_every_page_and_other_cli_adapters_fail_closed(tmp_path):
    from factory_core.adapters.models.backends import ModelRequest, CodexCliBackend, ClaudeCliBackend, AgyBackend
    manifests = document_project(tmp_path, pdf=True)
    images = doc.image_records(manifests['execution'])
    request = ModelRequest(tmp_path, 16, 1, 'Audit.', 30, 30, isolated=True,
        image_files=tuple(i['path'] for i in images))
    _, command = CodexCliBackend(tmp_path).command(request)
    assert [command[i+1] for i, value in enumerate(command) if value == '--image'] == [str(tmp_path/i['path']) for i in images]
    assert command[-2:] == ['--','Audit.']
    assert ClaudeCliBackend(tmp_path).execute(request).error_class == 'PERMANENT_MULTIMODAL_UNSUPPORTED'
    assert AgyBackend(tmp_path).execute(request).error_class == 'PERMANENT_MULTIMODAL_UNSUPPORTED'


def test_api_capacity_includes_task_prompt_and_framing(tmp_path, monkeypatch):
    from scripts import api_agent_run as api
    document_project(tmp_path)
    monkeypatch.setattr(api, '_MAX_JUDGE_INPUT_BYTES', 100_000)
    with pytest.raises(ValueError, match='combined judge prompt and context'):
        api.build_effective_prompt(tmp_path, 'x'*100_001,
            ['judge_packets/execution/context.txt'], 'judge_outputs/execution.md')


@pytest.mark.parametrize('backend', ['openai', 'gemini'])
def test_http_request_contains_exact_png_bytes(monkeypatch, backend):
    from scripts import llm_judge_call as llm
    sent = []
    png = b'\x89PNG\r\n\x1a\ncontrolled-image'
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self):
            return json.dumps({'choices':[{'message':{'content':'PASS'}}],
                'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':'PASS'}]}}]}).encode()
    def capture(request, **kwargs):
        sent.append(json.loads(request.data))
        return Response()
    monkeypatch.setenv('CONTROLLED_TEST_KEY', 'test-placeholder')
    monkeypatch.setenv('GEMINI_API_KEY', 'test-placeholder')
    monkeypatch.setattr(llm.urllib.request, 'urlopen', capture)
    assert llm.call('Full context', 'controlled', 10, 20, backend=backend,
        base_url='https://example.invalid', key_env='CONTROLLED_TEST_KEY', images=[png]) == 'PASS'
    if backend == 'openai':
        parts = sent[0]['messages'][1]['content']
        encoded = parts[1]['image_url']['url'].split(',',1)[1]
    else:
        parts = sent[0]['contents'][0]['parts']
        encoded = parts[1]['inline_data']['data']
    assert parts[0]['text'] == 'Full context'
    assert base64.b64decode(encoded) == png


def test_native_batch_freezes_assets_and_requires_transport_receipt(tmp_path):
    from tests.test_rerun_judge_batch import fixture
    from factory_core.judge_batch import verify, JudgeBatchError
    run, dispatcher = fixture(tmp_path)
    manifests = document_project(tmp_path, pdf=True)
    original = dispatcher.execute
    def execute(request, **kwargs):
        result = original(request, **kwargs)
        return replace(result, metadata={**result.metadata,
            'image_inputs': doc.image_inputs(tmp_path, request.image_files)})
    dispatcher.execute = execute
    first = run(role='execution')
    assert first.returncode == 0, first
    binding = first.metadata['audit_binding']
    assert run(role='execution').metadata['reused']
    seal = json.loads((tmp_path/binding['archive']/'committed.json').read_text())
    assert len(seal['request']['image_inputs']) == 2
    assert set(doc.manifest_assets(manifests['execution'])) <= set(seal['request']['inputs'])
    image = tmp_path/doc.image_records(manifests['execution'])[0]['path']
    image.write_bytes(image.read_bytes() + b'changed')
    with pytest.raises(JudgeBatchError):
        verify(tmp_path, binding)
    assert run(role='execution').returncode != 0


def test_missing_image_delivery_cannot_commit_native_result(tmp_path):
    from tests.test_rerun_judge_batch import fixture
    run, _ = fixture(tmp_path)
    document_project(tmp_path, pdf=True)
    assert run(role='execution').error_class == 'PERMANENT_JUDGE_EVIDENCE_BINDING'
    assert not list((tmp_path/'judge_outputs/batches').rglob('committed.json'))


def test_document_implementation_change_invalidates_evaluator(tmp_path):
    from scripts.submission_fingerprint import evaluator_contract_payload
    path = write(tmp_path, 'scripts/document_evidence_view.py', 'first revision')
    before = evaluator_contract_payload('demo', tmp_path)['implementation']
    path.write_text('next revision')
    assert before != evaluator_contract_payload('demo', tmp_path)['implementation']


def test_adopted_job_automatically_includes_xlsx_input_and_pdf_output(tmp_path):
    require_pdf()
    from tests.support.normal_run import controlled_service
    from scripts.canonical_claims import accept
    from scripts.solver_job_receipt import receipt_paths, build_evidence
    service, project, backend = controlled_service(tmp_path)
    packet_project(project)
    (project/'data').mkdir(exist_ok=True)
    (project/'data/observations.xlsx').write_bytes(workbook())
    script = write(project, 'models/03_solve.py',
        "import os, io, zipfile\nfrom pathlib import Path\nimport xml.etree.ElementTree as E\n"
        "from factory_core.json_values import dumps\n"
        "raw = Path('../data/observations.xlsx').read_bytes()\n"
        "with zipfile.ZipFile(io.BytesIO(raw)) as archive:\n"
        "    cells = E.fromstring(archive.read('xl/worksheets/sheet1.xml'))\n"
        "v = cells.find('.//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v').text\n"
        "Path('../results/values.json').write_text(dumps({'estimate': int(float(v)) + 1, "
        "'provenance': {'job_id': os.environ['FACTORY_SOLVER_JOB_ID']}}))\n"
        f"Path('../results/plots.pdf').write_bytes({pdf_bytes()!r})\n")
    job = service.submit_solver(project, runtime='python', script=script, max_time_seconds=10,
        input_paths=('data/observations.xlsx',), output_paths=('results/values.json','results/plots.pdf'))
    job = service.solver_status(project, job['job_id'])
    assert job['status'] == 'completed', backend.result.stderr
    submitted, completed = receipt_paths(project/'.factory/solver_receipts', job['job_id'])
    assert build_evidence(project, submitted, completed)['receipt_ready']
    accept(project, 'Q1_ESTIMATE', 'results/values.json::estimate',
           completed.relative_to(project).as_posix(), [], 'Adopt the controlled document-backed result.')
    manifests = build_packets(project)
    m = manifests['execution']
    chains = [r for r in m['completeness']['requirements'] if r['id'].startswith('solver_chain:')]
    assert chains and all(r['satisfied'] for r in chains)
    assert {'data/observations.xlsx','results/plots.pdf'} <= {p for r in chains for p in r['paths']}
    assert aggregate(project, role_outputs(project, manifests)).status == 'PASS'


def test_api_runner_binds_images_and_refuses_missing_attachments(tmp_path, monkeypatch):
    from scripts import api_agent_run as api
    import sys
    manifests = document_project(tmp_path, pdf=True)
    image_paths = [im['path'] for im in doc.image_records(manifests['execution'])]
    prompt_file = write(tmp_path, 'request.txt', 'Review complete materials.')
    argv = ['api_agent_run', '--project', str(tmp_path), '--prompt-file', str(prompt_file),
        '--output-file', 'judge_outputs/execution.md', '--model', 'controlled', '--backend', 'openai',
        '--context-file', 'judge_packets/execution/context.txt', '--overwrite']
    calls = []
    def call(prompt, *args, **kwargs):
        calls.append((prompt, kwargs['images']))
        return 'VERDICT: PASS\n{}\n'
    monkeypatch.setattr(api.llm_judge_call, 'call', call)
    monkeypatch.setattr(sys, 'argv', argv)
    assert api.main() == 2 and not calls
    monkeypatch.setattr(sys, 'argv', argv + [arg for p in image_paths for arg in ('--image-file', p)])
    assert api.main() == 0
    assert calls[0][1] == [doc.read_asset(tmp_path, p) for p in image_paths]
    assert (tmp_path/'judge_packets/execution/context.txt').read_text() in calls[0][0]
    metadata = json.loads((tmp_path/'judge_outputs/execution.md.llm-result.json').read_text())
    assert metadata['image_inputs'] == doc.image_inputs(tmp_path, image_paths)
