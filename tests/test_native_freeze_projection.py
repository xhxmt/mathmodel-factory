from factory_core.storage import SQLiteStateStore
from factory_core.contest import ContestPolicy
from web.backend.selection_service import build_content_freeze_options, write_selection_decision

def test_native_freeze_retains_reviewed_subject_bytes(tmp_path):
    p=tmp_path/'project'; p.mkdir()
    (p/'project_paper.tex').write_text('\\documentclass{article}\\begin{document}Test.\\end{document}')
    review=p/'human_review.md'; review.write_text('Existing user scope.\n')
    (p/'review.md').write_text('Reviewed unchanged input.\n')
    store=SQLiteStateStore(p)
    store.initialize(project_id=p.name,project_type='modeling',contest_policy=ContestPolicy.default(started_at=1000).to_dict())
    build_content_freeze_options(p,now_epoch=1100)
    before=review.read_bytes()
    decision=write_selection_decision(p,gate='content_freeze',selected_option_id='approve_content_freeze',source='manual-cli',reason='Existing authorization.',now_epoch=1200)
    assert decision['approved'] and not decision['mirrored_to_human_review']
    assert store.decision('content_freeze') is not None
    assert review.read_bytes()==before
    assert all(r.get('path') != 'human_review.md' for r in decision['artifact_refs'])
