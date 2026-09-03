"""Explicit non-formal fence used only by legacy delivery-mechanics tests."""

from pathlib import Path

from factory_core.phase9_delivery_fence import Phase9DeliveryFence


def nonformal_delivery_fence(project, **_kwargs) -> Phase9DeliveryFence:
    return Phase9DeliveryFence(
        project_id=Path(project).resolve().name,
        workflow_id="test-fixture:workflow",
        run_generation="test-fixture:generation",
        replay_id="test-fixture:replay",
        replay_mode="TEST_FIXTURE_DELIVERY",
        terminal_receipt_sha256="f" * 64,
        run_mode="TEST_FIXTURE_DELIVERY",
        modeling_consultation_contract="TEST_FIXTURE",
        delivery_capability="TEST_FIXTURE_ENABLED",
    )
