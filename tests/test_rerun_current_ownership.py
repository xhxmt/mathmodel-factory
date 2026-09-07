import pytest

from factory_core import current_artifact_ownership as current, artifact_ownership as frozen
from factory_core import current_dirty, dirty
from factory_core.finalization import build_final_input_manifest
from factory_core.legacy_classifier_compat import verify_legacy_classifier_contract_sha256_v9


@pytest.mark.parametrize('relative,stage', [('method_fit_suggestions.json', 1), ('STEP5_RECEIPT.json', 4)])
def test_native_root_evidence_is_included_and_routes_to_its_owner(tmp_path, relative, stage):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'project_paper.tex').write_text('Paper')
    path = project / relative
    path.write_text('{}')
    assert frozen.artifact_ownership(relative) is None
    assert current.artifact_ownership(relative).owner_stage == stage
    assert path in tuple(current.iter_owned_artifacts(project, final_input_only=True))
    manifest = build_final_input_manifest(project).manifest
    assert manifest['artifact_ownership_schema'] == current.ARTIFACT_OWNERSHIP_SCHEMA
    assert relative in {item['path'] for item in manifest['files']}
    changes = current_dirty.classify_manifest_changes({relative: 'old'}, {relative: 'new'})
    assert len(changes) == 1 and changes[0].owner_stage == stage
    assert current.reopen_after_step_for_artifact(relative) == (-1 if stage == 1 else 4)


def test_native_extension_does_not_rewrite_frozen_trust_roots():
    assert verify_legacy_classifier_contract_sha256_v9() == dirty.classifier_contract_sha256()
    assert current.ARTIFACT_OWNERSHIP_REGISTRY[:-3] == frozen.ARTIFACT_OWNERSHIP_REGISTRY
    assert current_dirty.classifier_contract_sha256() != dirty.classifier_contract_sha256()
    assert current.ARTIFACT_OWNERSHIP_SCHEMA != frozen.ARTIFACT_OWNERSHIP_SCHEMA


def test_unknown_artifact_still_has_no_owner_and_keeps_fail_closed_dirty_flags():
    path = 'unknown_authored_contract.json'
    assert current.artifact_ownership(path) is None
    assert current_dirty.classify_manifest_changes({}, {path: 'new'}) == dirty.classify_manifest_changes({}, {path: 'new'})


def test_existing_paths_have_exact_frozen_classifier_behavior():
    before = {'model.md': 'a', 'results/values.json': 'b'}
    after = {'model.md': 'c', 'results/values.json': 'd', 'unknown.txt': 'e'}
    assert current_dirty.classify_manifest_changes(before, after) == dirty.classify_manifest_changes(before, after)
