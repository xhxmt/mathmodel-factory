import hashlib
import json
from types import SimpleNamespace

import pytest

from factory_core import final_judge_projection as mod
from factory_core.domain import InvalidTransition
from factory_core.current_dirty import classifier_contract_sha256,capture_artifact_manifest,manifest_fingerprint
from factory_core.storage import SQLiteStateStore


@pytest.fixture
def bound(tmp_path, monkeypatch):
    tmp_path = tmp_path / 'ongoing' / 'demo'
    sid='a'*64
    (tmp_path/'.factory/audits'/sid).mkdir(parents=True)
    (tmp_path/'judge_outputs').mkdir()
    latest={'snapshot_id':sid,'status':'PASS','profile':'final','override':False,
            'judge_completed':True,'delivery_allowed':True}
    (tmp_path/'.factory/audits/latest.json').write_text(json.dumps(latest))
    (tmp_path/'.factory/audits'/sid/'snapshot.json').write_text('{}')
    report=tmp_path/'judge_evaluation.md';report.write_text('VERDICT: PASS\n')
    record={'path':report.name,'bytes':report.stat().st_size,
            'sha256':hashlib.sha256(report.read_bytes()).hexdigest()}
    receipt={'schema':'judgment-receipt-v1','status':'VALID','input_fingerprint':sid,
             'decision':{'effective_decision':'PASS'},'derived_artifacts':{'report':record}}
    (tmp_path/'judge_outputs/judgment_receipt.json').write_text(json.dumps(receipt))
    (tmp_path/'judge_outputs/final_acceptance_receipt.json').write_text('{}')
    monkeypatch.setattr(mod,'AuditSnapshot',lambda **kwargs: object())
    # Isolate the ownership boundary; the native acceptance verifier has its
    # own receipt/hash tests and remains mandatory in production.
    monkeypatch.setattr(mod,'verify_final_acceptance_receipt',lambda *a,**kw:(True,[]))
    return tmp_path,record


def change(record, artifact='judge_evaluation.md'):
    return {'flag':'MATH_DIRTY','owner_stage':8,'cause_artifact':artifact,
            'baseline_fingerprint':'b'*64,'current_fingerprint':record['sha256'],
            'classifier_contract_sha256':classifier_contract_sha256()}


def task(step=16):
    return SimpleNamespace(stage_id=10 if step==16 else 8,source_step_id=step,
                           subtask='delivery' if step==16 else 'conditional_math_preflight')


def test_only_final_generated_report_gets_delivery_ownership(bound):
    project,record=bound
    changes=[change(record)]
    out=mod.classify_delivery_report(project,task(),changes)
    assert (out[0]['flag'],out[0]['owner_stage'])==('FORMAT_DIRTY',10)
    assert changes[0]['flag']=='MATH_DIRTY'
    assert mod.classify_delivery_report(project,task(13),changes)==changes
    authored=changes+[change(record,'model.md')]
    assert mod.classify_delivery_report(project,task(),authored)==authored


@pytest.mark.parametrize('failure',['report_changed','acceptance_invalid','wrong_snapshot','overridden','not_final','missing_receipt'])
def test_unbound_or_invalid_report_remains_math_dirty(bound,monkeypatch,failure):
    project,record=bound
    if failure=='report_changed':
        (project/'judge_evaluation.md').write_text('unreviewed manual edit')
    elif failure=='acceptance_invalid':
        monkeypatch.setattr(mod,'verify_final_acceptance_receipt',lambda *a,**kw:(False,['invalid']))
    elif failure=='wrong_snapshot':
        p=project/'judge_outputs/judgment_receipt.json';v=json.loads(p.read_text());v['input_fingerprint']='c'*64;p.write_text(json.dumps(v))
    elif failure=='missing_receipt':
        (project/'judge_outputs/judgment_receipt.json').unlink()
    else:
        p=project/'.factory/audits/latest.json';v=json.loads(p.read_text())
        v['override' if failure=='overridden' else 'profile']=True if failure=='overridden' else 'precheck'
        p.write_text(json.dumps(v))
    changes=[change(record)]
    assert mod.classify_delivery_report(project,task(),changes)==changes


def make_store(project,record,*,other=False):
    store=SQLiteStateStore(project)
    state=store.initialize(project_id=project.name,project_type='modeling',last_completed_step=15,
                           scheduler_generation='stage_v1')
    changes=([change(record,'model.md')] if other else [])+[change(record)]
    state=store.transition(expected_revision=state.revision,event_type='STEP_FAILED',changes={},dirty_changes=changes)
    return store,state


def checkpoint(proof):
    return {'stage_id':10,'source_step_id':16,'subtask':'delivery','completed_step_id':16,
            'input_fingerprint':'b'*64,'output_fingerprint':'c'*64,
            'receipt':{'schema_version':'factory-stage-checkpoint-v1','status':'PASS','stage':10,
                       'generated_projection_receipts':[proof]}}


def test_recovery_reclassifies_only_exact_prior_cause_and_preserves_history(bound):
    project,record=bound;store,state=make_store(project,record)
    dirty=mod.classify_delivery_report(project,task(),[change(record)])[0]
    store.transition(expected_revision=state.revision,event_type='STEP_SUCCEEDED',changes={},
                     stage_checkpoint=checkpoint(dirty['generated_projection_receipt']),dirty_changes=[dirty])
    assert [(i['flag'],i['owner_stage']) for i in store.dirty_flags()]==[('FORMAT_DIRTY',10)]
    with store._session() as c:
        assert c.execute("SELECT COUNT(*) FROM dirty_causes WHERE flag='MATH_DIRTY'").fetchone()[0]==1
        proof=json.loads(c.execute('SELECT receipt_json FROM dirty_classifier_rebases').fetchone()[0])
        assert proof['schema_version']=='factory-final-judge-projection-reclassification-v1'
        assert proof['quality_override'] is False and proof['new_success_receipts']==0


def test_report_reclassification_cannot_hide_an_earlier_math_edit(bound):
    project,record=bound;store,state=make_store(project,record,other=True)
    dirty=mod.classify_delivery_report(project,task(),[change(record)])[0]
    with pytest.raises(InvalidTransition,match='unrelated mathematical'):
        store.transition(expected_revision=state.revision,event_type='STEP_SUCCEEDED',changes={},
                         stage_checkpoint=checkpoint(dirty['generated_projection_receipt']),dirty_changes=[dirty])
    assert store.load().revision==state.revision
    assert store.dirty_flags()[0]['flag']=='MATH_DIRTY'


def test_projection_cannot_reclassify_without_successful_delivery_checkpoint(bound):
    project,record=bound;store,state=make_store(project,record)
    dirty=mod.classify_delivery_report(project,task(),[change(record)])[0]
    with pytest.raises(InvalidTransition,match='valid delivery receipt'):
        store.transition(expected_revision=state.revision,event_type='STEP_FAILED',changes={},dirty_changes=[dirty])
    assert store.load().revision==state.revision


def published_old_report(project):
    sid='a'*64
    release=project.parent.parent/'papers/releases'/project.name/sid
    release.mkdir(parents=True)
    receipt=(project/'judge_outputs/judgment_receipt.json').read_bytes()
    acceptance={'status':'PASS','snapshot_id':sid,'artifacts':{'judgment_receipt':{
        'bytes':len(receipt),'sha256':hashlib.sha256(receipt).hexdigest()}}}
    audit={'status':'PASS','snapshot_id':sid,'override':False,'judge_completed':True}
    (release/'final_audit_receipt.json').write_text(json.dumps(acceptance))
    (release/'audit_result.json').write_text(json.dumps(audit))
    artifacts={}
    for key in ('audit_result','final_audit_receipt'):
        p=release/(key+'.json');artifacts[key]={'path':p.name,'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
    manifest={'schema_version':'paper-factory-release-v1','snapshot_id':sid,'status':'PASS','artifacts':artifacts}
    manifest['content_sha256']=mod.canonical_hash(manifest)
    (release/'delivery_manifest.json').write_text(json.dumps(manifest))
    return release


def _cache_verified_report(project):
    """Cache the verified receipt bytes the way older audits left them on disk."""
    proof=mod.verified_final_report(project)
    assert proof is not None
    target=project/'.factory/generated_judge_reports'/proof['report']['sha256']
    target.mkdir(parents=True,exist_ok=True)
    (target/'judgment_receipt.json').write_bytes((project/'judge_outputs/judgment_receipt.json').read_bytes())


def test_old_report_receipt_is_anchored_to_published_release(bound):
    project,record=bound;release=published_old_report(project)
    _cache_verified_report(project)
    assert mod.historical_final_report(project,record['sha256']) is not None
    (release/'final_audit_receipt.json').write_text('tampered')
    assert mod.historical_final_report(project,record['sha256']) is None


@pytest.mark.parametrize('field', ['report', 'decision'])
@pytest.mark.parametrize('value', [None, []])
def test_non_object_judgment_fields_do_not_crash_delivery_projection(bound, field, value):
    project, record = bound
    receipt_path = project / 'judge_outputs/judgment_receipt.json'
    receipt = json.loads(receipt_path.read_text())
    if field == 'report':
        receipt['derived_artifacts']['report'] = value
    else:
        receipt['decision'] = value
    receipt_path.write_text(json.dumps(receipt))
    assert mod.verified_final_report(project) is None
    if field == 'report':
        published_old_report(project)
        cached = project / '.factory/generated_judge_reports' / record['sha256'] / 'judgment_receipt.json'
        cached.parent.mkdir(parents=True)
        cached.write_bytes(receipt_path.read_bytes())
        assert mod.historical_final_report(project, record['sha256']) is None


def test_new_final_audit_can_correct_authentic_old_generated_report(bound):
    project,record=bound;published_old_report(project)
    _cache_verified_report(project)
    store,state=make_store(project,record)
    p=project/'judge_evaluation.md';p.write_text('VERDICT: PASS\nNew native final review\n')
    updated={'path':p.name,'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
    r=project/'judge_outputs/judgment_receipt.json';v=json.loads(r.read_text());v['derived_artifacts']['report']=updated;r.write_text(json.dumps(v))
    dirty=mod.classify_delivery_report(project,task(),[change(updated)])[0]
    store.transition(expected_revision=state.revision,event_type='STEP_SUCCEEDED',changes={},
                     stage_checkpoint=checkpoint(dirty['generated_projection_receipt']),dirty_changes=[dirty])
    assert [(i['flag'],i['owner_stage']) for i in store.dirty_flags()]==[('FORMAT_DIRTY',10)]
    with store._session() as c:
        correction=json.loads(c.execute('SELECT receipt_json FROM dirty_classifier_rebases').fetchone()[0])
    assert record['sha256'] in correction['historical_projection_receipts']


def test_successful_delivery_does_not_reconstruct_corrected_historical_math_cause(bound):
    project,record=bound;store,state=make_store(project,record)
    dirty=mod.classify_delivery_report(project,task(),[change(record)])[0]
    fp=manifest_fingerprint(capture_artifact_manifest(project))
    cp=checkpoint(dirty['generated_projection_receipt'])
    cp['output_fingerprint']=fp
    cp['receipt'].update(output_fingerprint=fp,classifier_contract_sha256=classifier_contract_sha256())
    store.transition(expected_revision=state.revision,event_type='STEP_SUCCEEDED',changes={},
                     stage_checkpoint=cp,dirty_changes=[dirty],
                     clear_dirty_stage={'owner_stage':10,'cleared_fingerprint':fp,
                        'classifier_contract_sha256':classifier_contract_sha256(),'success_receipt':cp['receipt']})
    assert store.dirty_flags()==[]
    assert store.dirty_clear_receipts()


def test_native_rebase_removes_only_a_corrected_flag_reconstructed_by_an_old_worker(bound):
    project,record=bound;store,state=make_store(project,record)
    original=dict(store.dirty_flags()[0])
    dirty=mod.classify_delivery_report(project,task(),[change(record)])[0]
    state=store.transition(expected_revision=state.revision,event_type='STEP_SUCCEEDED',changes={},
                     stage_checkpoint=checkpoint(dirty['generated_projection_receipt']),dirty_changes=[dirty])
    with store._session() as c:
        # Simulate the old rebase implementation's mutable-index reconstruction;
        # it points at the original immutable cause and adds no new cause.
        c.execute('INSERT INTO dirty_flags VALUES (?,?,?,?,?,?,?)',tuple(original.values()))
        c.commit()
    state=store.rebase_dirty_classifier(expected_revision=state.revision)
    assert all(item['flag']!='MATH_DIRTY' for item in store.dirty_flags())
    assert any(item.get('removed_by_exact_correction') for receipt in store.dirty_classifier_rebase_receipts()
               for item in receipt['receipt'].get('obligations',[]))
    state=store.transition(expected_revision=state.revision,event_type='REAL_MATH_EDIT',changes={},
                           dirty_changes=[change(record,'model.md')])
    store.rebase_dirty_classifier(expected_revision=state.revision)
    assert any(item['flag']=='MODEL_DIRTY' and item['owner_stage']==3
               and item['cause_artifact']=='model.md' for item in store.dirty_flags())
