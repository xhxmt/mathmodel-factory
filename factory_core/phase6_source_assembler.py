"""Trusted current-reader assembler for the durable Phase-1--8 chain.

The low-level Phase-6 store deliberately accepts immutable values.  This
module is the audited producer boundary that is allowed to create a Phase-6
source eligible for Phase-7/8.  It never accepts caller-supplied Phase-3/4/5
hashes: it selects their exact durable heads through supported readers,
revalidates the complete Authority/Phase-3 graph, and emits one canonical
``trusted-source-chain-v1`` receipt.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .authority_read_repository import (
    AuthorityCurrentRunGeneration,
    AuthorityReadRepository,
    AuthorityRevisionCommandIdentity,
    AuthorityWorkflowCoordinate,
)
from .canonical import canonical_bytes, canonical_sha256
from .durable_operation import OperationStatus, build_worker_launch_identity
from .phase3_artifacts import (
    ArtifactLedgerOccurrence,
    ArtifactOccurrenceKind,
    artifact_occurrence_from_dict,
)
from .phase4_shadow_runtime import (
    Phase4RuntimeState,
    Phase4ShadowStore,
    Phase4SourceChainBinding,
    phase4_runtime_state_from_dict,
)
from .phase5_shadow_supervisor import (
    Phase5SupervisorStore,
    SupervisorScopeBinding,
    SupervisorState,
    SupervisorStatus,
    supervisor_state_from_dict,
)
from .phase6_snapshot_grants import (
    AuthoritySourceBinding,
    Phase6SnapshotGrantStore,
    Phase6SnapshotNotFound,
    VerifiedShadowSnapshot,
    build_authority_source_binding,
)


TRUSTED_SOURCE_CHAIN_SCHEMA = "trusted-source-chain-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}\Z")


class TrustedSourceChainError(RuntimeError):
    """The exact Phase-1--6 durable chain cannot be proven current."""

    code = "TRUSTED_SOURCE_CHAIN_INVALID"


def _sha(value: object, field: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise TrustedSourceChainError(f"{field} must be a lowercase SHA-256")
    return value


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise TrustedSourceChainError(f"{field} must be a canonical identifier")
    return value


@lru_cache(maxsize=1)
def trusted_source_implementation_identity() -> str:
    """Hash exact regular-file producer/current-reader implementation bytes.

    This is deliberately named an implementation identity.  It is not a Git
    candidate/tree identity and must not be represented as one by callers.
    Each member is opened without following symlinks, must have one link, and
    is rejected if its inode metadata changes while it is being read.
    """

    root = Path(__file__).absolute().parent
    names = (
        "authority_read_repository.py",
        "phase4_shadow_runtime.py",
        "phase5_shadow_supervisor.py",
        "phase6_snapshot_grants.py",
        "phase6_source_assembler.py",
        "phase78_current.py",
    )
    members = []
    for name in names:
        path = root / name
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_nlink != 1
        ):
            raise TrustedSourceChainError(
                f"trusted-source implementation member is not a unique regular file: {name}"
            )
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino)
                != (before.st_dev, before.st_ino)
            ):
                raise TrustedSourceChainError(
                    f"trusted-source implementation member changed before read: {name}"
                )
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after_open = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after_path = path.lstat()
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_nlink,
        )
        identity_opened = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
            opened.st_nlink,
        )
        identity_after_open = (
            after_open.st_dev,
            after_open.st_ino,
            after_open.st_size,
            after_open.st_mtime_ns,
            after_open.st_ctime_ns,
            after_open.st_nlink,
        )
        identity_after_path = (
            after_path.st_dev,
            after_path.st_ino,
            after_path.st_size,
            after_path.st_mtime_ns,
            after_path.st_ctime_ns,
            after_path.st_nlink,
        )
        if not (
            identity_before
            == identity_opened
            == identity_after_open
            == identity_after_path
        ):
            raise TrustedSourceChainError(
                f"trusted-source implementation member changed during read: {name}"
            )
        raw = b"".join(chunks)
        if len(raw) != before.st_size:
            raise TrustedSourceChainError(
                f"trusted-source implementation member size differs: {name}"
            )
        members.append(
            {"path": f"factory_core/{name}", "sha256": hashlib.sha256(raw).hexdigest()}
        )
    return canonical_sha256(
        {
            "schema_version": "trusted-source-chain-implementation-v1",
            "members": members,
        }
    )


def _coordinate(value: object) -> AuthorityWorkflowCoordinate:
    expected = {
        "schema", "workflow_id", "project_id", "project_generation",
        "run_generation", "runtime_generation", "scheduler_generation",
        "current_revision", "contract_pin_set_sha256", "authority_state",
        "source_fence_sha256", "switch_mode", "switch_epoch",
    }
    if type(value) is not dict or set(value) != expected:
        raise TrustedSourceChainError("Authority coordinate is malformed")
    try:
        result = AuthorityWorkflowCoordinate(
            workflow_id=value["workflow_id"],
            project_id=value["project_id"],
            project_generation=value["project_generation"],
            run_generation=value["run_generation"],
            runtime_generation=value["runtime_generation"],
            scheduler_generation=value["scheduler_generation"],
            current_revision=value["current_revision"],
            contract_pin_set_sha256=value["contract_pin_set_sha256"],
            authority_state=value["authority_state"],
            source_fence_sha256=value["source_fence_sha256"],
            switch_mode=value["switch_mode"],
            switch_epoch=value["switch_epoch"],
        )
    except (KeyError, TypeError) as exc:
        raise TrustedSourceChainError("Authority coordinate is malformed") from exc
    if result.as_dict() != value:
        raise TrustedSourceChainError("Authority coordinate schema differs")
    return result


def _command(value: object) -> AuthorityRevisionCommandIdentity:
    expected = {
        "schema", "workflow_id", "revision", "command_id", "message_id",
        "command_sha256", "event_sha256", "receipt_sha256", "outbox_sha256",
        "bundle_sha256", "phase3_mutation_sha256",
    }
    if type(value) is not dict or set(value) != expected:
        raise TrustedSourceChainError("Authority command identity is malformed")
    try:
        result = AuthorityRevisionCommandIdentity(
            workflow_id=_identifier(value["workflow_id"], "command.workflow_id"),
            revision=value["revision"],
            command_id=_identifier(value["command_id"], "command.command_id"),
            message_id=_identifier(value["message_id"], "command.message_id"),
            command_sha256=_sha(value["command_sha256"], "command.command_sha256"),
            event_sha256=_sha(value["event_sha256"], "command.event_sha256"),
            receipt_sha256=_sha(value["receipt_sha256"], "command.receipt_sha256"),
            outbox_sha256=_sha(value["outbox_sha256"], "command.outbox_sha256"),
            bundle_sha256=_sha(value["bundle_sha256"], "command.bundle_sha256"),
            phase3_mutation_sha256=_sha(
                value["phase3_mutation_sha256"],
                "command.phase3_mutation_sha256",
            ),
        )
    except (KeyError, TypeError) as exc:
        raise TrustedSourceChainError("Authority command identity is malformed") from exc
    if type(result.revision) is not int or result.revision < 1 or result.as_dict() != value:
        raise TrustedSourceChainError("Authority command identity differs")
    return result


def _run_generation(value: object) -> AuthorityCurrentRunGeneration:
    fields = tuple(AuthorityCurrentRunGeneration.__dataclass_fields__)
    if (
        type(value) is not dict
        or set(value) != {"schema", *fields}
        or value.get("schema") != "authority-current-run-generation-v1"
    ):
        raise TrustedSourceChainError("run-generation identity is malformed")
    try:
        result = AuthorityCurrentRunGeneration(
            **{name: value[name] for name in fields}
        )
    except (KeyError, TypeError) as exc:
        raise TrustedSourceChainError("run-generation identity is malformed") from exc
    if result.as_dict() != value:
        raise TrustedSourceChainError("run-generation identity differs")
    for name in (
        "contract_pin_set_sha256", "official_input_manifest_sha256",
        "official_input_raw_bytes_set_sha256",
        "execution_context_receipt_sha256",
        "operator_authorization_receipt_sha256", "request_sha256",
        "creation_receipt_sha256",
    ):
        _sha(getattr(result, name), f"run_generation.{name}")
    if result.delivery_capability != "DISABLED":
        raise TrustedSourceChainError("run generation is not delivery-disabled")
    return result


@dataclass(frozen=True, slots=True)
class TrustedSourceChainReceipt:
    implementation_identity_sha256: str
    authority_coordinate: AuthorityWorkflowCoordinate
    authority_coordinate_sha256: str
    run_generation_identity: AuthorityCurrentRunGeneration
    authority_revision_snapshot_sha256: str
    authority_command: AuthorityRevisionCommandIdentity
    authority_predecessor_event_sha256: str | None
    phase3_artifact_state: object
    selected_occurrence: ArtifactLedgerOccurrence
    phase3_current_graph_sha256: str
    phase3_current_occurrence_ids: tuple[str, ...]
    phase4_state: Phase4RuntimeState
    phase4_predecessor_state_sha256: str
    phase5_state: SupervisorState
    phase6_previous_snapshot_id: str | None
    phase6_previous_source_binding_sha256: str | None
    receipt_sha256: str

    def identity_dict(self) -> dict[str, object]:
        value = {
            "schema_version": TRUSTED_SOURCE_CHAIN_SCHEMA,
            "implementation_identity_sha256": self.implementation_identity_sha256,
            "authority_coordinate": self.authority_coordinate.as_dict(),
            "authority_coordinate_sha256": self.authority_coordinate_sha256,
            "run_generation_identity": self.run_generation_identity.as_dict(),
            "authority_revision_snapshot_sha256": (
                self.authority_revision_snapshot_sha256
            ),
            "authority_command": self.authority_command.as_dict(),
            "authority_predecessor_event_sha256": (
                self.authority_predecessor_event_sha256
            ),
            "phase3_artifact_state": self.phase3_artifact_state.as_dict(),
            "selected_occurrence": self.selected_occurrence.as_dict(),
            "phase3_current_graph_sha256": self.phase3_current_graph_sha256,
            "phase3_current_occurrence_ids": list(
                self.phase3_current_occurrence_ids
            ),
            "phase4_state": self.phase4_state.as_dict(),
            "phase4_current_state_sha256": self.phase4_state.state_sha256,
            "phase4_predecessor_state_sha256": (
                self.phase4_predecessor_state_sha256
            ),
            "phase5_state": self.phase5_state.as_dict(),
            "phase5_current_state_sha256": self.phase5_state.state_sha256,
            "phase6_previous_snapshot_id": self.phase6_previous_snapshot_id,
            "phase6_previous_source_binding_sha256": (
                self.phase6_previous_source_binding_sha256
            ),
            "authoritative": False,
            "authority_transferred": False,
            "dispatch_performed": False,
            "provider_call_performed": False,
        }
        return json.loads(canonical_bytes(value).decode("utf-8"))

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "receipt_sha256": self.receipt_sha256}


def trusted_source_chain_receipt_from_dict(
    value: object,
) -> TrustedSourceChainReceipt:
    if type(value) is not dict:
        raise TrustedSourceChainError("trusted-source receipt must be an object")
    identity = dict(value)
    supplied = identity.pop("receipt_sha256", None)
    expected = {
        "schema_version", "implementation_identity_sha256",
        "authority_coordinate", "authority_coordinate_sha256",
        "run_generation_identity",
        "authority_revision_snapshot_sha256", "authority_command",
        "authority_predecessor_event_sha256", "phase3_artifact_state",
        "selected_occurrence", "phase3_current_graph_sha256",
        "phase3_current_occurrence_ids", "phase4_state",
        "phase4_current_state_sha256", "phase4_predecessor_state_sha256",
        "phase5_state", "phase5_current_state_sha256",
        "phase6_previous_snapshot_id", "phase6_previous_source_binding_sha256",
        "authoritative", "authority_transferred", "dispatch_performed",
        "provider_call_performed",
    }
    if set(identity) != expected or identity.get("schema_version") != TRUSTED_SOURCE_CHAIN_SCHEMA:
        raise TrustedSourceChainError("trusted-source receipt fields differ")
    if any(
        identity.get(name) is not False
        for name in (
            "authoritative", "authority_transferred", "dispatch_performed",
            "provider_call_performed",
        )
    ):
        raise TrustedSourceChainError("trusted-source receipt claims authority or effects")
    digest = _sha(supplied, "receipt_sha256")
    if canonical_sha256(identity) != digest:
        raise TrustedSourceChainError("trusted-source receipt SHA-256 differs")
    coordinate = _coordinate(identity["authority_coordinate"])
    if coordinate.coordinate_sha256 != _sha(
        identity["authority_coordinate_sha256"], "authority_coordinate_sha256"
    ):
        raise TrustedSourceChainError("Authority coordinate SHA-256 differs")
    from .authority_read_repository import authority_phase3_artifact_state_from_dict

    try:
        state = authority_phase3_artifact_state_from_dict(
            identity["phase3_artifact_state"]
        )
        occurrence = artifact_occurrence_from_dict(identity["selected_occurrence"])
        phase4 = phase4_runtime_state_from_dict(identity["phase4_state"])
        phase5 = supervisor_state_from_dict(identity["phase5_state"])
        command = _command(identity["authority_command"])
        run_generation = _run_generation(identity["run_generation_identity"])
    except Exception as exc:
        if isinstance(exc, TrustedSourceChainError):
            raise
        raise TrustedSourceChainError(
            "trusted-source receipt nested facts do not revalidate"
        ) from exc
    ids = identity["phase3_current_occurrence_ids"]
    if type(ids) is not list or not all(type(item) is str for item in ids):
        raise TrustedSourceChainError("Phase-3 current occurrence inventory differs")
    if (
        phase4.state_sha256 != identity["phase4_current_state_sha256"]
        or phase5.state_sha256 != identity["phase5_current_state_sha256"]
    ):
        raise TrustedSourceChainError("Phase-4/5 state SHA-256 differs")
    source = phase4.source_chain_binding
    if (
        run_generation.workflow_id != coordinate.workflow_id
        or run_generation.project_id != coordinate.project_id
        or run_generation.project_generation != coordinate.project_generation
        or run_generation.run_generation != coordinate.run_generation
        or run_generation.runtime_generation != coordinate.runtime_generation
        or run_generation.scheduler_generation != coordinate.scheduler_generation
        or run_generation.contract_pin_set_sha256
        != coordinate.contract_pin_set_sha256
        or source is None
        or source.source_commit != run_generation.source_commit
        or source.source_tree != run_generation.source_tree
        or source.source_parent != run_generation.source_parent
        or source.run_generation_request_sha256 != run_generation.request_sha256
        or source.run_generation_creation_receipt_sha256
        != run_generation.creation_receipt_sha256
    ):
        raise TrustedSourceChainError(
            "run-generation identity differs from Authority/P4 coordinate"
        )
    previous_snapshot = identity["phase6_previous_snapshot_id"]
    previous_binding = identity["phase6_previous_source_binding_sha256"]
    if (previous_snapshot is None) != (previous_binding is None):
        raise TrustedSourceChainError("Phase-6 predecessor binding is incomplete")
    if previous_snapshot is not None:
        _sha(previous_snapshot, "phase6_previous_snapshot_id")
        _sha(previous_binding, "phase6_previous_source_binding_sha256")
    result = TrustedSourceChainReceipt(
        _sha(
            identity["implementation_identity_sha256"],
            "implementation_identity_sha256",
        ),
        coordinate,
        identity["authority_coordinate_sha256"],
        run_generation,
        _sha(
            identity["authority_revision_snapshot_sha256"],
            "authority_revision_snapshot_sha256",
        ),
        command,
        identity["authority_predecessor_event_sha256"],
        state,
        occurrence,
        _sha(identity["phase3_current_graph_sha256"], "phase3_current_graph_sha256"),
        tuple(ids),
        phase4,
        _sha(
            identity["phase4_predecessor_state_sha256"],
            "phase4_predecessor_state_sha256",
        ),
        phase5,
        previous_snapshot,
        previous_binding,
        digest,
    )
    if result.as_dict() != value:
        differing = sorted(
            name
            for name in set(result.as_dict()) | set(value)
            if result.as_dict().get(name) != value.get(name)
        )
        raise TrustedSourceChainError(
            "trusted-source receipt canonical shape differs: "
            + ",".join(differing)
        )
    return result


class Phase6TrustedSourceAssembler:
    """Read-only assembler plus narrowly scoped durable P4 producer helpers."""

    def __init__(
        self,
        *,
        authority_database: str | Path,
        authority_source_fence_sha256: str,
        phase4_database: str | Path,
        phase5_database: str | Path,
        phase6_store: Phase6SnapshotGrantStore,
        deadline: object | None = None,
    ) -> None:
        self._authority = AuthorityReadRepository(
            authority_database,
            expected_source_fence_sha256=authority_source_fence_sha256,
            deadline=deadline,
        )
        self._phase4 = Phase4ShadowStore(Path(phase4_database))
        self._phase5 = Phase5SupervisorStore(Path(phase5_database))
        self._phase6 = phase6_store
        self._deadline = deadline

    def _phase3_facts(
        self,
        *,
        workflow_id: str,
        occurrence_id: str,
    ) -> tuple[
        AuthorityWorkflowCoordinate,
        object,
        ArtifactLedgerOccurrence,
        AuthorityRevisionCommandIdentity,
        str,
        str | None,
        str,
        tuple[str, ...],
        AuthorityCurrentRunGeneration,
    ]:
        snapshot = self._authority.trusted_phase3_source_snapshot(
            workflow_id=workflow_id,
            occurrence_id=occurrence_id,
        )
        coordinate = snapshot.coordinate
        if coordinate.contract_pin_set_sha256 is None:
            raise TrustedSourceChainError("Authority contract pins are unavailable")
        for name in (
            coordinate.project_generation, coordinate.run_generation,
            coordinate.runtime_generation, coordinate.scheduler_generation,
        ):
            if name == "legacy_unknown":
                raise TrustedSourceChainError("legacy_unknown generations are ineligible")
        state = snapshot.artifact_state
        occurrence = snapshot.selected_occurrence
        if occurrence.kind is not ArtifactOccurrenceKind.RECORD:
            raise TrustedSourceChainError("selected Phase-3 occurrence is not a record")
        command = snapshot.revision_command
        if (
            command.command_id != occurrence.command_id
            or command.phase3_mutation_sha256 != occurrence.mutation_sha256
        ):
            raise TrustedSourceChainError(
                "selected occurrence command/mutation binding differs"
            )
        revision_snapshot = snapshot.revision_snapshot
        events = tuple(revision_snapshot.events)
        selected_events = tuple(
            event
            for event in events
            if event.revision == occurrence.revision
            and event.command_id == occurrence.command_id
            and event.envelope_sha256 == command.event_sha256
        )
        if len(selected_events) != 1:
            raise TrustedSourceChainError("selected command is absent from revision graph")
        predecessor = snapshot.predecessor_event_sha256
        occurrence_ids = tuple(item.occurrence_id for item in state.occurrences)
        graph_sha = canonical_sha256(
            {
                "schema_version": "phase3-current-complete-graph-v1",
                "authority_coordinate_sha256": coordinate.coordinate_sha256,
                "authority_revision_snapshot_sha256": revision_snapshot.snapshot_sha256,
                "artifact_state": state.as_dict(),
                "current_occurrence_ids": list(occurrence_ids),
            }
        )
        return (
            coordinate,
            state,
            occurrence,
            command,
            revision_snapshot.snapshot_sha256,
            predecessor,
            graph_sha,
            occurrence_ids,
            snapshot.run_generation,
        )

    def prepare_phase4_binding(
        self,
        *,
        workflow_id: str,
        occurrence_id: str,
    ) -> Phase4SourceChainBinding:
        (
            coordinate, state, occurrence, command, revision_sha, predecessor,
            graph_sha, _occurrence_ids, run_generation,
        ) = self._phase3_facts(
            workflow_id=workflow_id,
            occurrence_id=occurrence_id,
        )
        return self._phase4_binding_from_facts(
            coordinate=coordinate,
            state=state,
            occurrence=occurrence,
            command=command,
            revision_sha=revision_sha,
            predecessor=predecessor,
            graph_sha=graph_sha,
            run_generation=run_generation,
        )

    @staticmethod
    def _phase4_binding_from_facts(
        *,
        coordinate: AuthorityWorkflowCoordinate,
        state: object,
        occurrence: ArtifactLedgerOccurrence,
        command: AuthorityRevisionCommandIdentity,
        revision_sha: str,
        predecessor: str | None,
        graph_sha: str,
        run_generation: AuthorityCurrentRunGeneration,
    ) -> Phase4SourceChainBinding:
        return Phase4SourceChainBinding(
            project_id=coordinate.project_id,
            workflow_id=coordinate.workflow_id,
            authority_revision=coordinate.current_revision,
            project_generation=coordinate.project_generation,
            run_generation=coordinate.run_generation,
            runtime_generation=coordinate.runtime_generation,
            scheduler_generation=coordinate.scheduler_generation,
            contract_pin_set_sha256=coordinate.contract_pin_set_sha256,
            phase3_artifact_state_sha256=state.state_sha256,
            selected_occurrence_id=occurrence.occurrence_id,
            selected_occurrence_semantic_sha256=occurrence.semantic_sha256,
            phase3_current_graph_sha256=graph_sha,
            authority_command_id=command.command_id,
            authority_command_sha256=command.command_sha256,
            authority_mutation_sha256=command.phase3_mutation_sha256,
            authority_revision_snapshot_sha256=revision_sha,
            authority_outbox_message_id=command.message_id,
            authority_predecessor_event_sha256=predecessor,
            implementation_identity_sha256=(
                trusted_source_implementation_identity()
            ),
            source_commit=run_generation.source_commit,
            source_tree=run_generation.source_tree,
            source_parent=run_generation.source_parent,
            run_generation_request_sha256=run_generation.request_sha256,
            run_generation_creation_receipt_sha256=(
                run_generation.creation_receipt_sha256
            ),
        )

    def produce_phase4_operation(
        self,
        *,
        workflow_id: str,
        occurrence_id: str,
        invocation_id: str,
        attempt_id: str,
        process_scope_id: str,
        occurred_at: int,
    ):
        """Reserve an operation whose outbox/payload identities are reader-derived."""

        binding = self.prepare_phase4_binding(
            workflow_id=workflow_id,
            occurrence_id=occurrence_id,
        )
        identity = build_worker_launch_identity(
            outbox_command_id=binding.authority_outbox_message_id,
            invocation_id=invocation_id,
            attempt_id=attempt_id,
            process_scope_id=process_scope_id,
            payload_sha256=binding.phase3_current_graph_sha256,
        )
        return self._phase4.reserve_operation(
            identity,
            occurred_at=occurred_at,
            source_chain_binding=binding,
        )

    def phase5_binding_from_current_phase4(
        self,
        *,
        operation_identity_sha256: str,
        scope_kind: Enum,
    ) -> SupervisorScopeBinding:
        """Build P5 only from the exact current typed P4 predecessor."""

        phase4 = self._phase4.load(operation_identity_sha256)
        source = phase4.source_chain_binding
        if source is None:
            raise TrustedSourceChainError("Phase-4 source-chain binding is unavailable")
        current = self._phase4.load_current_for_source_chain(
            workflow_id=source.workflow_id,
            source_chain_binding_sha256=source.binding_sha256,
        )
        if current != phase4:
            raise TrustedSourceChainError(
                "Phase-4 operation is not the unique current source head"
            )
        identity = phase4.operation.identity
        return SupervisorScopeBinding(
            workflow_id=source.workflow_id,
            invocation_id=identity.invocation_id,
            attempt_id=identity.attempt_id,
            process_scope_id=identity.process_scope_id,
            operation_identity_sha256=identity.identity_sha256,
            scope_kind=scope_kind,
            phase4_predecessor_state_sha256=phase4.state_sha256,
            phase4_source_chain_binding_sha256=source.binding_sha256,
        )

    def _current_phase6_predecessor(
        self,
        coordinate: AuthorityWorkflowCoordinate,
    ) -> tuple[str | None, str | None]:
        try:
            snapshot = self._phase6.current_snapshot(
                workflow_id=coordinate.workflow_id,
                project_id=coordinate.project_id,
            )
        except Phase6SnapshotNotFound:
            return None, None
        return snapshot.snapshot_id, snapshot.source_binding.binding_sha256

    def assemble(
        self,
        *,
        workflow_id: str,
        occurrence_id: str,
        operation_identity_sha256: str,
        phase5_request_id: str,
    ) -> TrustedSourceChainReceipt:
        (
            coordinate, state, occurrence, command, revision_sha, predecessor,
            graph_sha, occurrence_ids, run_generation,
        ) = self._phase3_facts(
            workflow_id=workflow_id,
            occurrence_id=occurrence_id,
        )
        expected_p4 = self._phase4_binding_from_facts(
            coordinate=coordinate,
            state=state,
            occurrence=occurrence,
            command=command,
            revision_sha=revision_sha,
            predecessor=predecessor,
            graph_sha=graph_sha,
            run_generation=run_generation,
        )
        phase4 = self._phase4.load_current_for_source_chain(
            workflow_id=workflow_id,
            source_chain_binding_sha256=expected_p4.binding_sha256,
        )
        if (
            phase4.operation.identity.identity_sha256
            != operation_identity_sha256
            or
            phase4.source_chain_binding != expected_p4
            or phase4.operation.identity.outbox_command_id != command.message_id
            or phase4.operation.identity.payload_sha256 != graph_sha
            or phase4.operation.status is not OperationStatus.SUCCEEDED
        ):
            raise TrustedSourceChainError(
                "Phase-4 current head is stale, incomplete, or differently bound"
            )
        phase5 = self._phase5.load(phase5_request_id)
        operation_head = self._phase5.load_current_for_operation(
            workflow_id=workflow_id,
            operation_identity_sha256=operation_identity_sha256,
        )
        binding = phase5.binding
        if (
            phase5 != operation_head
            or phase5.status is not SupervisorStatus.COMPLETED
            or binding.workflow_id != workflow_id
            or binding.invocation_id != phase4.operation.identity.invocation_id
            or binding.attempt_id != phase4.operation.identity.attempt_id
            or binding.process_scope_id != phase4.operation.identity.process_scope_id
            or binding.operation_identity_sha256 != operation_identity_sha256
            or binding.phase4_source_chain_binding_sha256 != expected_p4.binding_sha256
            or binding.phase4_predecessor_state_sha256 is None
        ):
            raise TrustedSourceChainError(
                "Phase-5 current head is stale, incomplete, or differently bound"
            )
        phase4_predecessor = self._phase4.load_state_by_sha256(
            binding.phase4_predecessor_state_sha256
        )
        if (
            phase4_predecessor.operation.identity
            != phase4.operation.identity
            or phase4_predecessor.source_chain_binding != expected_p4
            or phase4_predecessor.operation.status
            in {OperationStatus.CANCELLED, OperationStatus.CANCEL_REQUESTED}
        ):
            raise TrustedSourceChainError("Phase-5 selected the wrong P4 predecessor")
        p6_id, p6_binding = self._current_phase6_predecessor(coordinate)
        identity = {
            "schema_version": TRUSTED_SOURCE_CHAIN_SCHEMA,
            "implementation_identity_sha256": (
                trusted_source_implementation_identity()
            ),
            "authority_coordinate": coordinate.as_dict(),
            "authority_coordinate_sha256": coordinate.coordinate_sha256,
            "run_generation_identity": run_generation.as_dict(),
            "authority_revision_snapshot_sha256": revision_sha,
            "authority_command": command.as_dict(),
            "authority_predecessor_event_sha256": predecessor,
            "phase3_artifact_state": state.as_dict(),
            "selected_occurrence": occurrence.as_dict(),
            "phase3_current_graph_sha256": graph_sha,
            "phase3_current_occurrence_ids": list(occurrence_ids),
            "phase4_state": phase4.as_dict(),
            "phase4_current_state_sha256": phase4.state_sha256,
            "phase4_predecessor_state_sha256": phase4_predecessor.state_sha256,
            "phase5_state": phase5.as_dict(),
            "phase5_current_state_sha256": phase5.state_sha256,
            "phase6_previous_snapshot_id": p6_id,
            "phase6_previous_source_binding_sha256": p6_binding,
            "authoritative": False,
            "authority_transferred": False,
            "dispatch_performed": False,
            "provider_call_performed": False,
        }
        wire = json.loads(canonical_bytes(identity).decode("utf-8"))
        return trusted_source_chain_receipt_from_dict(
            {**wire, "receipt_sha256": canonical_sha256(wire)}
        )

    def verify_current(
        self,
        receipt: TrustedSourceChainReceipt | object,
    ) -> TrustedSourceChainReceipt:
        checked = (
            receipt
            if type(receipt) is TrustedSourceChainReceipt
            else trusted_source_chain_receipt_from_dict(receipt)
        )
        workflow = checked.authority_coordinate.workflow_id
        (
            current_coordinate,
            live_state,
            live_occurrence,
            live_command,
            live_revision_sha,
            live_predecessor,
            live_graph_sha,
            live_occurrence_ids,
            live_run_generation,
        ) = self._phase3_facts(
            workflow_id=workflow,
            occurrence_id=checked.selected_occurrence.occurrence_id,
        )
        if (
            trusted_source_implementation_identity()
            != checked.implementation_identity_sha256
            or current_coordinate != checked.authority_coordinate
            or live_run_generation != checked.run_generation_identity
            or live_state != checked.phase3_artifact_state
            or live_occurrence != checked.selected_occurrence
            or live_command != checked.authority_command
            or live_revision_sha != checked.authority_revision_snapshot_sha256
            or live_predecessor != checked.authority_predecessor_event_sha256
            or live_graph_sha != checked.phase3_current_graph_sha256
            or live_occurrence_ids != checked.phase3_current_occurrence_ids
        ):
            raise TrustedSourceChainError(
                "Authority/Phase-3 trusted source is no longer current"
            )
        operation = checked.phase4_state.operation.identity
        source = checked.phase4_state.source_chain_binding
        if source is None:
            raise TrustedSourceChainError("Phase-4 trusted binding is unavailable")
        live_phase4 = self._phase4.load_current_for_source_chain(
            workflow_id=workflow,
            source_chain_binding_sha256=source.binding_sha256,
        )
        if live_phase4 != checked.phase4_state:
            raise TrustedSourceChainError("Phase-4 trusted source is no longer current")
        live_phase5 = self._phase5.load_current_for_operation(
            workflow_id=workflow,
            operation_identity_sha256=operation.identity_sha256,
        )
        binding = live_phase5.binding
        if (
            live_phase5 != checked.phase5_state
            or live_phase5.status is not SupervisorStatus.COMPLETED
            or binding.phase4_predecessor_state_sha256
            != checked.phase4_predecessor_state_sha256
            or binding.phase4_source_chain_binding_sha256
            != source.binding_sha256
        ):
            raise TrustedSourceChainError("Phase-5 trusted source is no longer current")
        return checked

    def build_phase6_source_binding(
        self,
        receipt: TrustedSourceChainReceipt,
        *,
        source_snapshot_schema: str,
        source_snapshot_semantic_sha256: str,
        source_snapshot_completeness: str,
        source_snapshot_coordinate: dict[str, object],
    ) -> AuthoritySourceBinding:
        checked = self.verify_current(receipt)
        return build_authority_source_binding(
            authority_coordinate=checked.authority_coordinate.as_dict(),
            authority_coordinate_sha256=checked.authority_coordinate_sha256,
            authority_revision_snapshot_sha256=(
                checked.authority_revision_snapshot_sha256
            ),
            authority_revision_through_revision=(
                checked.authority_coordinate.current_revision
            ),
            source_snapshot_schema=source_snapshot_schema,
            source_snapshot_semantic_sha256=source_snapshot_semantic_sha256,
            source_snapshot_completeness=source_snapshot_completeness,
            source_snapshot_coordinate=source_snapshot_coordinate,
            phase3_artifact_state_sha256=(
                checked.phase3_artifact_state.state_sha256
            ),
            phase4_operation_state_sha256=checked.phase4_state.state_sha256,
            phase5_supervisor_state_sha256=checked.phase5_state.state_sha256,
            trusted_source_chain_receipt=checked.as_dict(),
        )


def verify_receipt_bound_snapshot(
    receipt: TrustedSourceChainReceipt,
    snapshot: VerifiedShadowSnapshot,
) -> None:
    """Verify the append-time P6 predecessor CAS and embedded receipt."""

    source = snapshot.source_binding
    if (
        snapshot.previous_snapshot_id != receipt.phase6_previous_snapshot_id
        or source.trusted_source_chain_receipt != receipt.as_dict()
        or source.phase3_artifact_state_sha256
        != receipt.phase3_artifact_state.state_sha256
        or source.phase4_operation_state_sha256 != receipt.phase4_state.state_sha256
        or source.phase5_supervisor_state_sha256 != receipt.phase5_state.state_sha256
    ):
        raise TrustedSourceChainError(
            "Phase-6 snapshot is not bound to the exact trusted-source receipt"
        )
