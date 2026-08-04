#!/usr/bin/env python3
"""Build and validate the question-to-claim registry used by judge packets.

Projects may declare ``claim_registry.json`` using ``claim-registry-v1``.  Its
top-level arrays are ``questions``, ``claims``, and ``delivery_requirements``.
Every claim uses this shape::

    {
      "id": "Q1_PRIMARY_RESULT",
      "statement": "The paper answers question 1 with a reproducible result.",
      "question_ids": ["Q1"],
      "required_roles": ["paper", "math", "execution"],
      "artifacts": [
        {"path": "paper/paper.tex", "roles": ["paper", "math"]},
        {"path": "results/problem1/values.json", "roles": ["execution"]}
      ]
    }

Paths are project-relative POSIX paths.  Absolute paths, traversal, backslashes,
and symlink escapes are rejected.  When no declared registry exists, a
conservative registry is derived from problem headings, ``quality_contract``,
and ``problem/deliverables.json``.  Derived coverage is explicitly labelled and
missing question claims remain missing; the builder never invents mathematical
or execution evidence.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


REGISTRY_CONTRACT_VERSION = "claim-registry-v1"
COVERAGE_CONTRACT_VERSION = "claim-coverage-v1"
ROLES = ("paper", "math", "execution")
ROLE_SET = frozenset(ROLES)

_QUESTION_HEADING = re.compile(
    r"^#{2,6}\s*(?:问题|problem|question|task)\s*"
    r"(?P<number>[0-9]+|[一二三四五六七八九十]+)\s*(?:[：:.、]|$)",
    re.IGNORECASE,
)
_QUESTION_REFERENCE = re.compile(
    r"(?:问题|problem|question|task)[-_\s]*"
    r"(?P<number>[0-9]+|[一二三四五六七八九十]+)",
    re.IGNORECASE,
)
_CLAIM_ID_QUESTION = re.compile(r"^P(?P<number>[0-9]+)(?:_|$)", re.IGNORECASE)
_CHINESE_DIGITS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


def _nonempty_string(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} must be a nonempty string")
    return value.strip()


def _roles(value: object, where: str, *, default: Iterable[str] = ROLES) -> list[str]:
    if value is None:
        result = list(default)
    elif isinstance(value, list):
        result = [_nonempty_string(item, f"{where}[]") for item in value]
    else:
        raise ValueError(f"{where} must be an array")
    if not result or len(set(result)) != len(result) or not set(result) <= ROLE_SET:
        raise ValueError(f"{where} must contain unique judge roles")
    return result


def safe_relative_path(value: object, where: str) -> str:
    """Validate a portable path without consulting or escaping the project."""

    raw = _nonempty_string(value, where)
    if "\x00" in raw or "\\" in raw:
        raise ValueError(f"{where} must be a project-relative POSIX path")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{where} must not be absolute or contain traversal")
    return path.as_posix()


def _safe_existing_file(project: Path, relative: str) -> Path | None:
    try:
        resolved = (project / relative).resolve(strict=True)
        resolved.relative_to(project)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _question_number(raw: str) -> int | None:
    if raw.isdigit():
        number = int(raw)
        return number if number > 0 else None
    if raw in _CHINESE_DIGITS:
        return _CHINESE_DIGITS[raw]
    if raw.startswith("十") and len(raw) == 2 and raw[1] in _CHINESE_DIGITS:
        return 10 + _CHINESE_DIGITS[raw[1]]
    if raw.endswith("十") and len(raw) == 2 and raw[0] in _CHINESE_DIGITS:
        return _CHINESE_DIGITS[raw[0]] * 10
    if len(raw) == 3 and raw[1] == "十":
        left = _CHINESE_DIGITS.get(raw[0])
        right = _CHINESE_DIGITS.get(raw[2])
        if left and right:
            return left * 10 + right
    return None


def _question_id_from_text(text: object) -> str | None:
    question_ids = _question_ids_from_text(text)
    return question_ids[0] if question_ids else None


def _question_ids_from_text(text: object) -> list[str]:
    if not isinstance(text, str):
        return []
    result: list[str] = []
    for match in _QUESTION_REFERENCE.finditer(text):
        number = _question_number(match.group("number"))
        question_id = f"Q{number}" if number else None
        if question_id and question_id not in result:
            result.append(question_id)
    return result


def _source_path(locator: object) -> str | None:
    if not isinstance(locator, str):
        return None
    raw = locator.split("::", 1)[0].split("#", 1)[0].strip()
    try:
        return safe_relative_path(raw, "source locator")
    except ValueError:
        return None


def _validate_question(item: object, index: int) -> dict[str, Any]:
    where = f"questions[{index}]"
    if not isinstance(item, dict):
        raise ValueError(f"{where} must be an object")
    question_id = _nonempty_string(item.get("id"), f"{where}.id")
    statement = _nonempty_string(item.get("statement"), f"{where}.statement")
    source = item.get("source")
    if not isinstance(source, dict):
        raise ValueError(f"{where}.source must be an object")
    source_path = safe_relative_path(source.get("path"), f"{where}.source.path")
    line = source.get("line")
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        raise ValueError(f"{where}.source.line must be a positive integer")
    return {
        "id": question_id,
        "statement": statement,
        "source": {"path": source_path, "line": line},
        "required": item.get("required", True) is not False,
        "required_roles": _roles(item.get("required_roles"), f"{where}.required_roles"),
    }


def _validate_claim(item: object, index: int, kind: str) -> dict[str, Any]:
    where = f"{kind}[{index}]"
    if not isinstance(item, dict):
        raise ValueError(f"{where} must be an object")
    claim_id = _nonempty_string(item.get("id"), f"{where}.id")
    question_ids = item.get("question_ids", [])
    if not isinstance(question_ids, list) or any(
        not isinstance(value, str) or not value.strip() for value in question_ids
    ):
        raise ValueError(f"{where}.question_ids must be an array of nonempty strings")
    if len(set(question_ids)) != len(question_ids):
        raise ValueError(f"{where}.question_ids must be unique")
    required_roles = _roles(item.get("required_roles"), f"{where}.required_roles")
    artifacts = item.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError(f"{where}.artifacts must be an array")
    normalized_artifacts: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for artifact_index, artifact in enumerate(artifacts):
        artifact_where = f"{where}.artifacts[{artifact_index}]"
        if not isinstance(artifact, dict):
            raise ValueError(f"{artifact_where} must be an object")
        path = safe_relative_path(artifact.get("path"), f"{artifact_where}.path")
        artifact_roles = _roles(
            artifact.get("roles"), artifact_where + ".roles", default=required_roles
        )
        if not set(artifact_roles) <= set(required_roles):
            raise ValueError(f"{artifact_where}.roles must be a subset of required_roles")
        key = (path, tuple(artifact_roles))
        if key in seen:
            raise ValueError(f"duplicate artifact registration at {artifact_where}")
        seen.add(key)
        normalized_artifacts.append({"path": path, "roles": artifact_roles})
    return {
        "id": claim_id,
        "kind": (
            "delivery"
            if kind == "delivery_requirements"
            else str(item.get("kind") or "claim")
        ),
        "statement": _nonempty_string(item.get("statement"), f"{where}.statement"),
        "question_ids": list(question_ids),
        "required": item.get("required", True) is not False,
        "required_roles": required_roles,
        "artifacts": normalized_artifacts,
    }


def load_declared_registry(project: Path) -> dict[str, Any] | None:
    project = project.resolve()
    path = project / "claim_registry.json"
    if not path.exists():
        return None
    resolved = _safe_existing_file(project, "claim_registry.json")
    if resolved is None:
        raise ValueError("claim_registry.json must be a regular file inside the project")
    data = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("claim registry must be a JSON object")
    if data.get("contract_version") != REGISTRY_CONTRACT_VERSION:
        raise ValueError(f"claim registry contract_version must be {REGISTRY_CONTRACT_VERSION}")
    raw_questions = data.get("questions")
    raw_claims = data.get("claims")
    raw_delivery = data.get("delivery_requirements", [])
    if not isinstance(raw_questions, list) or not isinstance(raw_claims, list):
        raise ValueError("claim registry questions and claims must be arrays")
    if not isinstance(raw_delivery, list):
        raise ValueError("claim registry delivery_requirements must be an array")
    questions = [_validate_question(item, index) for index, item in enumerate(raw_questions)]
    claims = [_validate_claim(item, index, "claims") for index, item in enumerate(raw_claims)]
    delivery = [
        _validate_claim(item, index, "delivery_requirements")
        for index, item in enumerate(raw_delivery)
    ]
    question_ids = [item["id"] for item in questions]
    if not question_ids:
        raise ValueError("claim registry must contain at least one question")
    if len(set(question_ids)) != len(question_ids):
        raise ValueError("claim registry question ids must be unique")
    claim_ids = [item["id"] for item in claims + delivery]
    if len(set(claim_ids)) != len(claim_ids):
        raise ValueError("claim and delivery requirement ids must be globally unique")
    known_questions = set(question_ids)
    for question in questions:
        source_path = question["source"]["path"]
        source_file = _safe_existing_file(project, source_path)
        if source_file is None:
            raise ValueError(
                f"question {question['id']} source must be a regular file inside the project"
            )
        line_count = sum(
            1
            for _ in source_file.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        )
        if question["source"]["line"] > line_count:
            raise ValueError(f"question {question['id']} source line is out of range")
    unknown = sorted(
        {
            question_id
            for claim in claims + delivery
            for question_id in claim["question_ids"]
            if question_id not in known_questions
        }
    )
    if unknown:
        raise ValueError("claim registry references unknown questions: " + ", ".join(unknown))
    _, detected_questions = _problem_questions(project)
    detected_ids = {item["id"] for item in detected_questions}
    if detected_ids:
        declared_required_ids = {
            item["id"] for item in questions if item.get("required", True)
        }
        if declared_required_ids != detected_ids:
            missing = sorted(detected_ids - declared_required_ids)
            extra = sorted(declared_required_ids - detected_ids)
            details = []
            if missing:
                details.append("missing=" + ",".join(missing))
            if extra:
                details.append("extra=" + ",".join(extra))
            raise ValueError(
                "claim registry questions do not match problem headings ("
                + "; ".join(details)
                + ")"
            )
    deliverables_path = _safe_existing_file(project, "problem/deliverables.json")
    if deliverables_path is not None:
        deliverables_data = json.loads(deliverables_path.read_text(encoding="utf-8"))
        if not isinstance(deliverables_data, dict):
            raise ValueError("problem/deliverables.json must be an object")
        attachments = deliverables_data.get("attachments", [])
        strategy_tables = deliverables_data.get("strategy_tables", [])
        if not isinstance(attachments, list) or not isinstance(strategy_tables, list):
            raise ValueError("problem deliverable arrays are invalid")
        expected_delivery_count = len(attachments) + len(strategy_tables)
        required_delivery_count = sum(
            item.get("required", True) for item in delivery
        )
        if required_delivery_count != expected_delivery_count:
            raise ValueError(
                "claim registry delivery requirements do not match "
                f"problem/deliverables.json (expected {expected_delivery_count}, "
                f"got {required_delivery_count})"
            )
    return {
        "contract_version": REGISTRY_CONTRACT_VERSION,
        "source": {
            "mode": "declared",
            "path": "claim_registry.json",
            "declared_required": True,
            "declared_missing": False,
        },
        "questions": questions,
        "claims": claims,
        "delivery_requirements": delivery,
        "diagnostics": [],
    }


def _problem_questions(project: Path) -> tuple[str | None, list[dict[str, Any]]]:
    for relative in ("problem/problem_brief.md", "problem/source.md"):
        resolved = _safe_existing_file(project, relative)
        if resolved is None:
            continue
        questions: list[dict[str, Any]] = []
        for line_number, line in enumerate(
            resolved.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
        ):
            match = _QUESTION_HEADING.match(line.strip())
            if not match:
                continue
            number = _question_number(match.group("number"))
            if number is None:
                continue
            questions.append(
                {
                    "id": f"Q{number}",
                    "statement": re.sub(r"^#{2,6}\s*", "", line).strip(),
                    "source": {"path": relative, "line": line_number},
                    "required": True,
                    "required_roles": list(ROLES),
                }
            )
        if questions:
            deduplicated = {item["id"]: item for item in questions}
            return relative, list(deduplicated.values())
    return None, []


def _final_paper_path(project: Path, base_name: str) -> str | None:
    for relative in (f"{base_name}_paper.tex", "paper/paper.tex"):
        if _safe_existing_file(project, relative) is not None:
            return relative
    return None


def _derived_quality_claims(
    project: Path, known_questions: set[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    path = _safe_existing_file(project, "quality_contract.json")
    if path is None:
        return [], ["quality_contract.json is missing or unsafe"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"quality_contract.json is invalid: {exc}"]
    raw_claims = data.get("claims") if isinstance(data, dict) else None
    if not isinstance(raw_claims, list):
        return [], ["quality_contract.json claims are missing"]
    claims: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    for index, raw in enumerate(raw_claims):
        if not isinstance(raw, dict):
            diagnostics.append(f"quality claim at index {index} is not an object")
            continue
        claim_id = raw.get("id")
        statement = raw.get("statement")
        if (
            not isinstance(claim_id, str)
            or not claim_id.strip()
            or not isinstance(statement, str)
            or not statement.strip()
        ):
            diagnostics.append(f"quality claim at index {index} lacks id or statement")
            continue
        explicit_question_ids = raw.get("question_ids")
        if isinstance(explicit_question_ids, list) and all(
            isinstance(item, str) for item in explicit_question_ids
        ):
            question_ids = [item for item in explicit_question_ids if item in known_questions]
        else:
            inferred_ids = _question_ids_from_text(raw.get("source"))
            if not inferred_ids:
                match = _CLAIM_ID_QUESTION.match(claim_id)
                inferred_ids = [f"Q{int(match.group('number'))}"] if match else []
            question_ids = [item for item in inferred_ids if item in known_questions]
        artifacts: list[dict[str, Any]] = []
        source = _source_path(raw.get("source"))
        if source:
            artifacts.append({"path": source, "roles": ["math"]})
        implementation_locators = (
            raw.get("implementation", [])
            if isinstance(raw.get("implementation"), list)
            else []
        )
        for locator in implementation_locators:
            implementation = _source_path(locator)
            if implementation and not any(item["path"] == implementation for item in artifacts):
                artifacts.append({"path": implementation, "roles": ["math", "execution"]})
        verification = _safe_existing_file(
            project, "quality_contract_verification.latest.txt"
        )
        if verification is not None:
            artifacts.append(
                {
                    "path": "quality_contract_verification.latest.txt",
                    "roles": ["execution"],
                }
            )
        claims.append(
            {
                "id": claim_id.strip(),
                "kind": "quality_contract",
                "statement": statement.strip(),
                "question_ids": question_ids,
                "required": raw.get("severity", "advisory") == "hard",
                "required_roles": ["math", "execution"],
                "artifacts": artifacts,
            }
        )
    return claims, diagnostics


def _derived_delivery_requirements(
    project: Path, final_paper: str | None, known_questions: set[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    path = _safe_existing_file(project, "problem/deliverables.json")
    if path is None:
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"problem/deliverables.json is invalid: {exc}"]
    if not isinstance(data, dict):
        return [], ["problem/deliverables.json must be an object"]
    report = (
        "deliverables_verification.latest.txt"
        if _safe_existing_file(project, "deliverables_verification.latest.txt") is not None
        else None
    )
    requirements: list[dict[str, Any]] = []
    for index, raw in enumerate(data.get("attachments", [])):
        if not isinstance(raw, dict):
            continue
        question = _question_id_from_text(raw.get("problem"))
        artifacts = [{"path": report, "roles": ["execution"]}] if report else []
        requirements.append(
            {
                "id": f"DELIVERY_ATTACHMENT_{index + 1}",
                "kind": "delivery",
                "statement": str(
                    raw.get("description") or raw.get("file") or "required attachment"
                ),
                "question_ids": [question] if question in known_questions else [],
                "required": True,
                "required_roles": ["execution"],
                "artifacts": artifacts,
            }
        )
    for index, raw in enumerate(data.get("strategy_tables", [])):
        if not isinstance(raw, dict):
            continue
        question = _question_id_from_text(raw.get("problem"))
        artifacts = [{"path": final_paper, "roles": ["paper"]}] if final_paper else []
        requirements.append(
            {
                "id": f"DELIVERY_TABLE_{index + 1}",
                "kind": "delivery",
                "statement": str(raw.get("description") or "required result table"),
                "question_ids": [question] if question in known_questions else [],
                "required": True,
                "required_roles": ["paper"],
                "artifacts": artifacts,
            }
        )
    return requirements, []


def derive_registry(project: Path, base_name: str) -> dict[str, Any]:
    problem_path, questions = _problem_questions(project)
    final_paper = _final_paper_path(project, base_name)
    known_questions = {item["id"] for item in questions}
    claims, diagnostics = _derived_quality_claims(project, known_questions)
    # A derived response claim only asserts that the problem statement and the
    # complete paper are visible to the paper reviewer.  It is intentionally
    # not used as mathematical or execution evidence.
    if problem_path and final_paper:
        for question in questions:
            claims.append(
                {
                    "id": f"DERIVED_{question['id']}_PAPER_RESPONSE",
                    "kind": "derived_question_response",
                    "statement": f"The final paper contains the response to {question['id']}.",
                    "question_ids": [question["id"]],
                    "required": True,
                    "required_roles": ["paper"],
                    "artifacts": [
                        {"path": problem_path, "roles": ["paper"]},
                        {"path": final_paper, "roles": ["paper"]},
                    ],
                }
            )
    delivery, delivery_diagnostics = _derived_delivery_requirements(
        project, final_paper, known_questions
    )
    diagnostics.extend(delivery_diagnostics)
    quality_version: object = None
    quality_path = _safe_existing_file(project, "quality_contract.json")
    if quality_path is not None:
        try:
            quality_data = json.loads(quality_path.read_text(encoding="utf-8"))
            quality_version = (
                quality_data.get("version") if isinstance(quality_data, dict) else None
            )
        except (OSError, json.JSONDecodeError):
            pass
    declared_required = quality_version in {2, 3, 4}
    if declared_required:
        diagnostics.append(
            f"quality contract v{quality_version} requires a declared claim_registry.json"
        )
    mode = "derived" if questions else "unavailable"
    if not questions:
        diagnostics.append("no machine-recognizable problem question headings found")
    return {
        "contract_version": REGISTRY_CONTRACT_VERSION,
        "source": {
            "mode": mode,
            "path": problem_path,
            "declared_required": declared_required,
            "declared_missing": declared_required,
            "limitations": (
                []
                if mode == "declared"
                else [
                    "derived registry proves artifact visibility, not semantic answer correctness"
                ]
            ),
        },
        "questions": questions,
        "claims": claims,
        "delivery_requirements": delivery,
        "diagnostics": diagnostics,
    }


def build_claim_registry(project: Path, base_name: str | None = None) -> dict[str, Any]:
    project = project.resolve()
    if not project.is_dir():
        raise FileNotFoundError(f"project directory not found: {project}")
    declared = load_declared_registry(project)
    return (
        declared
        if declared is not None
        else derive_registry(project, base_name or project.name)
    )


def artifact_paths_for_role(registry: dict[str, Any], role: str) -> list[str]:
    if role not in ROLE_SET:
        raise ValueError(f"unknown judge role: {role}")
    paths: list[str] = []
    if registry.get("source", {}).get("mode") == "declared":
        paths.append("claim_registry.json")
    for claim in registry.get("claims", []) + registry.get("delivery_requirements", []):
        if not claim.get("required", True) or role not in claim.get("required_roles", []):
            continue
        for artifact in claim.get("artifacts", []):
            if role in artifact.get("roles", []) and artifact["path"] not in paths:
                paths.append(artifact["path"])
    return paths


def coverage_requirements(registry: dict[str, Any], role: str) -> list[dict[str, Any]]:
    """Translate registry coverage into the packet's existing hard gate shape."""

    if role not in ROLE_SET:
        raise ValueError(f"unknown judge role: {role}")
    source = registry.get("source", {})
    if source.get("mode") == "unavailable" and source.get("declared_missing") is not True:
        return []
    # Derived registries are migration diagnostics only: inferred requirements
    # are still emitted so packet manifests disclose missing question/claim
    # evidence, but the eligibility calculation below deliberately remains
    # false unless the registry is explicitly declared.  This keeps the
    # diagnostic surface honest without allowing a heuristic registry to act
    # as a production hard gate.
    requirements: list[dict[str, Any]] = []
    if registry.get("source", {}).get("mode") == "declared":
        requirements.append(
            {
                "id": "claim_registry",
                "description": "declared question and claim registry",
                "required_status": "included",
                "paths": ["claim_registry.json"],
            }
        )
    elif registry.get("source", {}).get("declared_missing") is True:
        requirements.append(
            {
                "id": "claim_registry",
                "description": "quality contract v2+ requires declared claim_registry.json",
                "required_status": "included",
                "paths": [],
            }
        )
    all_claims = registry.get("claims", []) + registry.get("delivery_requirements", [])
    applicable = [
        claim
        for claim in all_claims
        if claim.get("required", True) and role in claim.get("required_roles", [])
    ]
    applicable_by_question = {
        question["id"]: [
            claim for claim in applicable if question["id"] in claim.get("question_ids", [])
        ]
        for question in registry.get("questions", [])
        if question.get("required", True) and role in question.get("required_roles", [])
    }
    for question_id, claims in applicable_by_question.items():
        if not claims:
            requirements.append(
                {
                    "id": f"question:{question_id}:registered_claim",
                    "description": f"{question_id} has at least one registered claim for {role}",
                    "required_status": "included",
                    "paths": [],
                }
            )
    for claim in applicable:
        paths = [
            artifact["path"]
            for artifact in claim.get("artifacts", [])
            if role in artifact.get("roles", [])
        ]
        requirements.append(
            {
                "id": f"claim:{claim['id']}",
                "description": claim["statement"],
                "required_status": "included",
                "paths": paths,
            }
        )
        if claim.get("kind") != "delivery" and not claim.get("question_ids"):
            requirements.append(
                {
                    "id": f"claim:{claim['id']}:question_assignment",
                    "description": f"claim {claim['id']} is assigned to a known question",
                    "required_status": "included",
                    "paths": [],
                }
            )
    return requirements


def evaluate_claim_coverage(
    registry: dict[str, Any], role: str, files: list[dict[str, Any]]
) -> dict[str, Any]:
    by_path = {str(item.get("path")): item for item in files}
    requirements = coverage_requirements(registry, role)
    requirement_state: dict[str, dict[str, Any]] = {}
    for requirement in requirements:
        paths = [str(path) for path in requirement["paths"]]
        satisfied = [
            path for path in paths if by_path.get(path, {}).get("status") == "included"
        ]
        requirement_state[requirement["id"]] = {
            "paths": paths,
            "satisfied_paths": satisfied,
            "satisfied": bool(paths) and len(paths) == len(satisfied),
        }
    all_claims = registry.get("claims", []) + registry.get("delivery_requirements", [])
    claims: list[dict[str, Any]] = []
    for claim in all_claims:
        if not claim.get("required", True) or role not in claim.get("required_roles", []):
            continue
        # Derived registries are intentionally not converted into hard packet
        # requirements (``coverage_requirements`` returns an empty list for
        # them), but their inferred artifact visibility is still useful as
        # diagnostic evidence.  Compute that state directly instead of
        # treating the absence of a hard requirement as proof that every
        # derived claim is missing.  The overall status remains
        # ``DERIVED_ONLY`` and ``eligible`` stays false below, so this never
        # silently promotes inferred coverage to a delivery gate.
        state = requirement_state.get(f"claim:{claim['id']}")
        if state is None:
            paths = [
                str(artifact["path"])
                for artifact in claim.get("artifacts", [])
                if role in artifact.get("roles", [])
            ]
            satisfied_paths = [
                path for path in paths if by_path.get(path, {}).get("status") == "included"
            ]
            state = {
                "paths": paths,
                "satisfied_paths": satisfied_paths,
                "satisfied": bool(paths) and len(paths) == len(satisfied_paths),
            }
        assigned = bool(claim.get("question_ids")) or claim.get("kind") == "delivery"
        covered = state.get("satisfied") is True and assigned
        failure_reason = None
        if not assigned:
            failure_reason = "claim_not_assigned_to_question"
        elif not state.get("paths"):
            failure_reason = "claim_has_no_registered_artifacts_for_role"
        elif not state.get("satisfied"):
            failure_reason = "claim_artifacts_not_fully_included"
        claims.append(
            {
                "id": claim["id"],
                "kind": claim.get("kind", "claim"),
                "statement": claim["statement"],
                "question_ids": claim.get("question_ids", []),
                "artifact_paths": state.get("paths", []),
                "satisfied_paths": state.get("satisfied_paths", []),
                "status": "COVERED" if covered else "MISSING",
                **({"failure_reason": failure_reason} if failure_reason else {}),
            }
        )
    by_claim_id = {item["id"]: item for item in claims}
    questions: list[dict[str, Any]] = []
    for question in registry.get("questions", []):
        if not question.get("required", True) or role not in question.get("required_roles", []):
            continue
        claim_ids = [
            claim["id"]
            for claim in all_claims
            if claim.get("required", True)
            and role in claim.get("required_roles", [])
            and question["id"] in claim.get("question_ids", [])
        ]
        missing = [
            claim_id
            for claim_id in claim_ids
            if by_claim_id.get(claim_id, {}).get("status") != "COVERED"
        ]
        covered = bool(claim_ids) and not missing
        questions.append(
            {
                "id": question["id"],
                "statement": question["statement"],
                "source": question["source"],
                "claim_ids": claim_ids,
                "covered_claim_ids": [item for item in claim_ids if item not in missing],
                "missing_claim_ids": missing,
                "status": "COVERED" if covered else "MISSING",
                **(
                    {"failure_reason": "no_registered_claims_for_role"}
                    if not claim_ids
                    else {}
                ),
            }
        )
    mode = registry.get("source", {}).get("mode", "unavailable")
    declared_missing = registry.get("source", {}).get("declared_missing") is True
    complete = (
        mode != "unavailable"
        and not declared_missing
        and all(item["status"] == "COVERED" for item in claims + questions)
    )
    if declared_missing:
        coverage_status = "INCOMPLETE"
    elif mode == "unavailable":
        coverage_status = "UNAVAILABLE"
    elif not complete:
        coverage_status = "INCOMPLETE"
    elif mode == "declared":
        coverage_status = "COMPLETE"
    else:
        coverage_status = "DERIVED_ONLY"
    return {
        "contract_version": COVERAGE_CONTRACT_VERSION,
        "registry_source": registry.get("source"),
        "status": coverage_status,
        "eligible": complete and mode == "declared",
        "questions": questions,
        "claims": claims,
        "missing_question_ids": [item["id"] for item in questions if item["status"] == "MISSING"],
        "missing_claim_ids": [item["id"] for item in claims if item["status"] == "MISSING"],
        "unassigned_claim_ids": [
            item["id"]
            for item in claims
            if item.get("kind") != "delivery" and not item.get("question_ids")
        ],
        "diagnostics": registry.get("diagnostics", []),
    }
