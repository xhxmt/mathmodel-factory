from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal


RenderType = Literal[
    "notice",
    "key_value",
    "method_cards",
    "dag",
    "markdown",
    "artifact_link",
    "table",
]
BlockType = Literal["status", "summary", "collection", "graph", "document", "data"]
ACTION_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
BLOCK_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")
RENDER_TYPES = {
    "notice",
    "key_value",
    "method_cards",
    "dag",
    "markdown",
    "artifact_link",
    "table",
}
BLOCK_TYPES = {"status", "summary", "collection", "graph", "document", "data"}


def _bounded_string(value: Any, field_name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} exceeds {maximum} characters")
    return normalized


@dataclass(frozen=True)
class ContentAction:
    id: str
    label: str
    payload: dict[str, Any] = field(default_factory=dict)
    style: Literal["primary", "secondary", "danger"] = "secondary"

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _bounded_string(self.id, "ContentAction.id", 80))
        object.__setattr__(self, "label", _bounded_string(self.label, "ContentAction.label", 120))
        if not ACTION_ID_RE.fullmatch(self.id):
            raise ValueError("ContentAction.id has an invalid format")
        if self.style not in {"primary", "secondary", "danger"}:
            raise ValueError("ContentAction.style is invalid")
        if not isinstance(self.payload, dict):
            raise ValueError("ContentAction.payload must be an object")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "payload": self.payload,
            "style": self.style,
        }


@dataclass(frozen=True)
class ContentBlock:
    id: str
    type: BlockType
    label: str
    render_type: RenderType
    content: Any = None
    children: list["ContentBlock"] = field(default_factory=list)
    actions: list[ContentAction] = field(default_factory=list)
    data_key: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _bounded_string(self.id, "ContentBlock.id", 120))
        object.__setattr__(self, "label", _bounded_string(self.label, "ContentBlock.label", 200))
        if not BLOCK_ID_RE.fullmatch(self.id):
            raise ValueError("ContentBlock.id has an invalid format")
        if self.type not in BLOCK_TYPES:
            raise ValueError("ContentBlock.type is invalid")
        if self.render_type not in RENDER_TYPES:
            raise ValueError("ContentBlock.render_type is invalid")
        if not isinstance(self.children, list) or not all(
            isinstance(child, ContentBlock) for child in self.children
        ):
            raise ValueError("ContentBlock.children must contain ContentBlock values")
        if not isinstance(self.actions, list) or not all(
            isinstance(action, ContentAction) for action in self.actions
        ):
            raise ValueError("ContentBlock.actions must contain ContentAction values")
        if self.data_key is not None:
            object.__setattr__(
                self, "data_key", _bounded_string(self.data_key, "ContentBlock.data_key", 120)
            )
        if not isinstance(self.meta, dict):
            raise ValueError("ContentBlock.meta must be an object")
        forbidden = {
            key for key in self.meta if str(key).lower() in {"token", "secret", "password"}
        }
        if forbidden:
            raise ValueError("ContentBlock.meta cannot contain secret-bearing keys")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "render_type": self.render_type,
            "content": self.content,
            "children": [child.to_dict() for child in self.children],
            "actions": [action.to_dict() for action in self.actions],
            "data_key": self.data_key,
            "meta": self.meta,
        }


@dataclass(frozen=True)
class NodeOutput:
    node_id: str
    title: str
    blocks: list[ContentBlock] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: Literal["node-output-v1"] = "node-output-v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _bounded_string(self.node_id, "NodeOutput.node_id", 120))
        object.__setattr__(self, "title", _bounded_string(self.title, "NodeOutput.title", 200))
        if self.schema_version != "node-output-v1":
            raise ValueError("NodeOutput.schema_version must be node-output-v1")
        if not isinstance(self.blocks, list) or not all(
            isinstance(block, ContentBlock) for block in self.blocks
        ):
            raise ValueError("NodeOutput.blocks must contain ContentBlock values")
        if not isinstance(self.metadata, dict):
            raise ValueError("NodeOutput.metadata must be an object")

    def to_ui_dict(self) -> dict[str, Any]:
        """Return the full typed payload consumed by the frontend registry."""

        return {
            "schema_version": self.schema_version,
            "node_id": self.node_id,
            "title": self.title,
            "blocks": [block.to_dict() for block in self.blocks],
            "metadata": self.metadata,
        }

    def to_llm_context(self, *, max_chars: int = 12_000) -> str:
        """Return an allow-listed, bounded representation for model context.

        UI actions and metadata are intentionally omitted so server-only or
        interaction data never leaks into a later model prompt.
        """

        safe_blocks = [_block_llm_payload(block) for block in self.blocks]
        text = json.dumps(
            {"node_id": self.node_id, "title": self.title, "blocks": safe_blocks},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(text) <= max_chars:
            return text
        marker = "…[内容已按模型上下文上限截断]"
        return text[: max(0, max_chars - len(marker))] + marker


def _block_llm_payload(block: ContentBlock) -> dict[str, Any]:
    return {
        "id": block.id,
        "type": block.type,
        "label": block.label,
        "content": block.content,
        "children": [_block_llm_payload(child) for child in block.children],
    }
