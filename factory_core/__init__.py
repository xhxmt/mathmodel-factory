"""Authoritative orchestration core for migrated Modeling Factory projects."""

from .domain import WorkflowState, WorkflowStatus

__all__ = ["FactoryEngine", "SQLiteStateStore", "WorkflowState", "WorkflowStatus"]


def __getattr__(name: str):
    """Keep low-level evidence tools independent of the full control plane."""

    if name == "FactoryEngine":
        from .engine import FactoryEngine

        return FactoryEngine
    if name == "SQLiteStateStore":
        from .storage import SQLiteStateStore

        return SQLiteStateStore
    raise AttributeError(name)
