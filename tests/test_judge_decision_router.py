from scripts.judge_decision_router import route_decision


def _aggregate(verdict="PASS", eligible=True):
    statuses = {"math": "PASS", "execution": "PASS", "paper": "PASS"}
    if verdict == "REOPEN_REVISION_MODEL":
        statuses["math"] = "FAIL"
    elif verdict == "REOPEN_REVISION_TEXT":
        statuses["paper"] = "REVISE"
    elif verdict == "INDETERMINATE_REVIEW":
        statuses["math"] = "INDETERMINATE"
    return {
        "schema_version": "judge-aggregate-v3",
        "verdict": verdict,
        "vetoes": [role for role in ("math", "execution") if statuses[role] == "FAIL"],
        "indeterminate_roles": [role for role in statuses if statuses[role] == "INDETERMINATE"],
        "role_statuses": statuses,
        "packet_completeness": {
            role: {
                "enforced": True,
                "eligible": eligible,
                "unmet_requirements": [] if eligible else ["claim:Q2"],
                "error": None,
            }
            for role in ("math", "execution", "paper")
        },
    }


def test_packet_failure_routes_to_rebuild_instead_of_model_revision():
    route = route_decision(_aggregate(eligible=False), {"status": "PASS"}, policy_mode="enforce")

    assert route["new_decision"] == "PACKET_REBUILD"
    assert route["effective_decision"] == "PACKET_REBUILD"


def test_visual_failure_routes_to_text_and_shadow_does_not_cut_over():
    visual = {
        "status": "FAIL",
        "findings": [{"code": "CONTENT_OUTSIDE_MEDIA_BOX", "severity": "blocking"}],
    }

    route = route_decision(_aggregate(), visual, policy_mode="shadow")

    assert route["new_decision"] == "REOPEN_REVISION_TEXT"
    assert route["effective_decision"] == "PASS"
    assert route["decision_changed"] is True
    assert route["automatic_cutover"] is False


def test_visual_infrastructure_failure_is_retryable():
    route = route_decision(
        _aggregate(), {"status": "INDETERMINATE", "error": "pdftoppm missing"}, policy_mode="enforce"
    )

    assert route["new_decision"] == "INFRA_RETRY"


def test_model_veto_precedes_visual_text_issue():
    route = route_decision(
        _aggregate("REOPEN_REVISION_MODEL"),
        {"status": "FAIL", "findings": [{"code": "OVERFULL_HBOX", "severity": "blocking"}]},
        policy_mode="enforce",
    )

    assert route["new_decision"] == "REOPEN_REVISION_MODEL"


def test_visual_failure_cannot_mask_packet_rebuild():
    route = route_decision(
        _aggregate(eligible=False),
        {"status": "FAIL", "findings": [{"severity": "blocking"}]},
        policy_mode="enforce",
    )
    assert route["new_decision"] == "PACKET_REBUILD"


def test_visual_indeterminate_cannot_mask_hard_model_veto():
    route = route_decision(
        _aggregate("REOPEN_REVISION_MODEL"),
        {"status": "INDETERMINATE", "error": "render unavailable"},
        policy_mode="enforce",
    )
    assert route["new_decision"] == "REOPEN_REVISION_MODEL"


def test_missing_roles_fail_closed_to_packet_rebuild():
    aggregate = _aggregate()
    aggregate["packet_completeness"].pop("paper")
    route = route_decision(aggregate, {"status": "PASS"}, policy_mode="enforce")
    assert route["new_decision"] == "PACKET_REBUILD"


def test_empty_packet_completeness_fail_closed_to_packet_rebuild():
    aggregate = _aggregate()
    aggregate["packet_completeness"] = {}
    route = route_decision(aggregate, {"status": "PASS"}, policy_mode="enforce")
    assert route["new_decision"] == "PACKET_REBUILD"


def test_missing_visual_gate_cannot_mask_packet_rebuild():
    route = route_decision(_aggregate(eligible=False), None, policy_mode="enforce")
    assert route["new_decision"] == "PACKET_REBUILD"


def test_missing_visual_gate_cannot_mask_hard_model_veto():
    route = route_decision(
        _aggregate("REOPEN_REVISION_MODEL"), None, policy_mode="enforce"
    )
    assert route["new_decision"] == "REOPEN_REVISION_MODEL"


def test_crafted_pass_with_inconsistent_role_statuses_cannot_route_pass():
    aggregate = _aggregate()
    aggregate["role_statuses"]["math"] = "FAIL"
    route = route_decision(aggregate, {"status": "PASS"}, policy_mode="enforce")
    assert route["new_decision"] == "INDETERMINATE_REVIEW"


def test_text_revision_is_not_rewritten_as_visual_infrastructure_failure():
    route = route_decision(
        _aggregate("REOPEN_REVISION_TEXT"),
        {"status": "INDETERMINATE", "error": "renderer unavailable"},
        policy_mode="enforce",
    )
    assert route["new_decision"] == "REOPEN_REVISION_TEXT"
