#!/usr/bin/env python3
"""High-confidence secret checks for frozen evidence payload bytes.

Findings intentionally contain only an archive path and a stable rule name.
Matched values, offsets, line contents, and value prefixes are never returned.
"""

from __future__ import annotations

import argparse
import base64
import ast
from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import Path
import re
import shlex
import sys
import tokenize
import warnings
from typing import Mapping, Sequence


@dataclass(frozen=True)
class SecretFinding:
    path: str
    rule: str


_SIGNATURE_RULES = (
    ("private_key_header", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("google_api_key", re.compile(rb"AIza[0-9A-Za-z_-]{30,}")),
    ("github_token", re.compile(rb"gh[pousr]_[0-9A-Za-z]{30,}")),
    ("aws_access_key", re.compile(rb"(?:AKIA|ASIA)[0-9A-Z]{16}")),
    ("openai_api_key", re.compile(rb"sk-[0-9A-Za-z_-]{20,}")),
    ("slack_token", re.compile(rb"xox[baprs]-[0-9A-Za-z-]{20,}")),
)
_JWT_CANDIDATE = re.compile(
    rb"(?<![A-Za-z0-9_-])"
    rb"([A-Za-z0-9_-]{2,})\.([A-Za-z0-9_-]{2,})\.([A-Za-z0-9_-]*)"
    rb"(?![A-Za-z0-9_-])"
)
_BEARER_VALUE = re.compile(
    rb"(?:[\"']authorization[\"']|authorization)[ \t]*:[ \t]*"
    rb"(?:[\"'][ \t]*)?bearer[ \t]+(?:[\"'][ \t]*)?([^\s\"']+)",
    re.IGNORECASE,
)
_DYNAMIC_BEARER_VALUES = (
    re.compile(rb"^\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)$"),
    re.compile(rb"^\{[A-Za-z_][A-Za-z0-9_]*\}$"),
    re.compile(rb"^%\([A-Za-z_][A-Za-z0-9_]*\)s$"),
    re.compile(rb"^<[^<>\s]+>$"),
    re.compile(rb"^(?:TOKEN|REDACTED|YOUR[_-]?(?:TOKEN|API[_-]?KEY))$", re.IGNORECASE),
)


def _decode_base64url_json(segment: bytes) -> object:
    if len(segment) % 4 == 1:
        raise ValueError("invalid base64url length")
    padded = segment + b"=" * ((4 - len(segment) % 4) % 4)
    decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    return json.loads(decoded.decode("utf-8"))


def _contains_validated_compact_jwt(value: bytes) -> bool:
    for candidate in _JWT_CANDIDATE.finditer(value):
        try:
            header = _decode_base64url_json(candidate.group(1))
            payload = _decode_base64url_json(candidate.group(2))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            continue
        if (
            isinstance(header, dict)
            and isinstance(payload, dict)
            and isinstance(header.get("alg"), str)
            and bool(header["alg"])
        ):
            return True
    return False


def _contains_hardcoded_bearer(
    value: bytes,
    *,
    allow_printf_placeholder: bool = False,
) -> bool:
    for candidate in _BEARER_VALUE.finditer(value):
        supplied = candidate.group(1)
        tail = value[candidate.start(1) :]
        command = _shell_command_substitution(tail)
        if command is not None:
            return True
        if allow_printf_placeholder and re.fullmatch(rb"%s(?:\\[nrt])?", supplied):
            remainder = value[candidate.end(1) :]
            if remainder[:1] in {b'"', b"'"}:
                remainder = remainder[1:]
            if not remainder or remainder[:1] in b" \t\r\n;)|":
                continue
        if any(pattern.fullmatch(supplied) for pattern in _DYNAMIC_BEARER_VALUES):
            remainder = value[candidate.end(1) :]
            if remainder[:1] in {b'"', b"'"}:
                remainder = remainder[1:]
            if remainder and remainder[:1] not in b" \t\r\n;)|":
                return True
            continue
        else:
            return True
    return False


def _shell_command_substitution(value: bytes) -> tuple[bytes, int] | None:
    if not value.startswith(b"$("):
        return None
    depth = 0
    index = 0
    while index < len(value):
        if value[index : index + 2] == b"$(":
            depth += 1
            index += 2
            continue
        if value[index : index + 1] == b")":
            depth -= 1
            if depth == 0:
                return value[2:index], index + 1
        index += 1
    return None


_SHELL_ASSIGNMENT = re.compile(
    rb"(?m)^[ \t]*(?:(?:export|readonly|local|declare(?:[ \t]+-[A-Za-z]+)?)[ \t]+)?"
    rb"([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*"
    rb"([^\r\n]+?)[ \t]*$"
)

_SHELL_DYNAMIC = object()
_SHELL_CONFLICT = object()


def _shell_static(value: bytes) -> tuple[str, bytes]:
    return ("static", value)


def _literal_shell_bearer_is_hardcoded(value: bytes) -> bool:
    # Only unmistakable documentation placeholders remain safe once shell
    # parsing has proved the value is literal.  A quoted '$TOKEN' is data, not
    # a runtime expansion.
    return not any(
        pattern.fullmatch(value) for pattern in _DYNAMIC_BEARER_VALUES[3:]
    )


def _shell_variable_name(value: bytes) -> bytes | None:
    match = re.fullmatch(
        rb"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))",
        value,
    )
    return None if match is None else (match.group(1) or match.group(2))


def _is_shell_source(path: str, value: bytes) -> bool:
    lowered = path.lower()
    if lowered.endswith(
        (
            ".sh",
            ".bash",
            ".zsh",
            ".ksh",
            ".dash",
            ".ash",
            ".mksh",
            ".yash",
            ".csh",
            ".tcsh",
            ".fish",
            ".bats",
            ".command",
        )
    ):
        return True
    basename = lowered.rsplit("/", 1)[-1]
    if basename in {
        "sh",
        "bash",
        "zsh",
        "ksh",
        "dash",
        "ash",
        "mksh",
        "yash",
        "csh",
        "tcsh",
        "fish",
        ".profile",
        ".bashrc",
        ".bash_profile",
        ".zshrc",
        ".kshrc",
        ".ashrc",
    }:
        return True
    first_line = value.splitlines()[0] if value.splitlines() else b""
    return bool(
        re.fullmatch(
            rb"#![ \t]*(?:/usr/bin/env(?:[ \t]+-S)?[ \t]+)?(?:/[^ \t]*/)?(?:[A-Za-z0-9_.+-]*sh|fish)(?:[ \t]+.*)?",
            first_line,
        )
    )


_AUTHORIZATION_WORD = b"Author" + b"ization"
_BEARER_WORD = b"Bear" + b"er"
_RUNTIME_FD_BEARER_LITERAL = _AUTHORIZATION_WORD + b": " + _BEARER_WORD + b" %s"
_APPROVED_RUNTIME_FD_PATHS = frozenset(
    {
        "scripts/gcp_solver_client.sh",
        "paper_factory/scripts/gcp_solver_client.sh",
    }
)
_APPROVED_RUNTIME_FD_SHA256 = (
    "20593f23782ee9c053a2e7c40f4fad2f36fe868a37e20a0410751b12e679cf82"
)
_SAFE_RUNTIME_FD_BEARER = re.compile(
    rb"3<[ \t]*<\([ \t]*printf[ \t]+(['\"])"
    + _RUNTIME_FD_BEARER_LITERAL
    + rb"\\n\1[ \t]+"
    rb"\"\$([A-Za-z_][A-Za-z0-9_]*)\"[ \t]*\)"
)


def _shell_has_only_safe_runtime_fd_bearer(path: str, value: bytes) -> bool:
    if (
        path not in _APPROVED_RUNTIME_FD_PATHS
        or hashlib.sha256(value).hexdigest() != _APPROVED_RUNTIME_FD_SHA256
        or not _is_shell_source(path, value)
    ):
        return False
    literal_count = value.count(_RUNTIME_FD_BEARER_LITERAL)
    if literal_count == 0:
        return False
    matches = tuple(_SAFE_RUNTIME_FD_BEARER.finditer(value))
    if len(matches) != literal_count:
        return False
    if value.count(b"-H @/dev/fd/3") < literal_count:
        return False
    for match in matches:
        variable = re.escape(match.group(2))
        assignment = re.compile(
            rb"(?m)^[ \t]*"
            + variable
            + rb"=\"\$\(get_identity_token\)\"[ \t]*$"
        )
        if assignment.search(value) is None:
            return False
    return True


def _shell_safe_unexported_aliases(value: bytes) -> frozenset[bytes]:
    aliases: set[bytes] = set()
    assignment = re.compile(
        rb"(?m)^[ \t]*([A-Za-z_][A-Za-z0-9_]*)=\"\$(?:\{)?"
        rb"([A-Za-z_][A-Za-z0-9_]*)(?:\})?\"[ \t]*$"
    )
    for match in assignment.finditer(value):
        target, source = match.groups()
        target_pattern = re.escape(target)
        source_pattern = re.escape(source)
        unexported = re.search(
            rb"(?m)^[ \t]*export[ \t]+-n[ \t]+" + target_pattern + rb"[ \t]*$",
            value,
        )
        removed_source = re.search(
            rb"(?m)^[ \t]*unset[ \t]+" + source_pattern + rb"[ \t]*$",
            value,
        )
        if unexported is not None and removed_source is not None:
            aliases.add(target)
    return frozenset(aliases)


def _shell_static_assignment_rules(path: str, value: bytes) -> tuple[str, ...]:
    if not _is_shell_source(path, value):
        return ()
    parsed: list[tuple[bytes, bytes, bool]] = []
    for assignment in _SHELL_ASSIGNMENT.finditer(value):
        name = assignment.group(1)
        rhs = assignment.group(2).strip()
        single_quoted = len(rhs) >= 2 and rhs[:1] == rhs[-1:] == b"'"
        double_quoted = len(rhs) >= 2 and rhs[:1] == rhs[-1:] == b'"'
        supplied = rhs[1:-1] if single_quoted or double_quoted else rhs
        parsed.append((name, supplied, single_quoted))

    assigned_names = {name for name, _, _ in parsed}
    states: dict[bytes, object] = {}
    for _round in range(max(1, 2 * len(assigned_names) + 1)):
        changed = False
        for name, supplied, single_quoted in parsed:
            if single_quoted:
                # Shell does not expand anything inside single quotes.  Keep an
                # explicit static tag so '$TOKEN' cannot be mistaken for a
                # machine-proven dynamic reference downstream.
                candidate: object | None = _shell_static(supplied)
            else:
                dependency = _shell_variable_name(supplied)
                if dependency is not None:
                    if dependency in states:
                        candidate = states[dependency]
                    elif dependency in assigned_names:
                        candidate = None
                    else:
                        candidate = _SHELL_DYNAMIC
                elif b"$" in supplied or b"`" in supplied or b"\\" in supplied:
                    candidate = _SHELL_CONFLICT
                else:
                    candidate = _shell_static(supplied)
            if candidate is None:
                continue
            previous = states.get(name)
            if previous is _SHELL_CONFLICT:
                continue
            if previous is None:
                states[name] = candidate
                changed = True
            elif previous != candidate:
                states[name] = _SHELL_CONFLICT
                changed = True
        if not changed:
            break
    else:
        raise RuntimeError("shell secret-scan dataflow did not converge")

    for candidate in _BEARER_VALUE.finditer(value):
        supplied = candidate.group(1)
        name = _shell_variable_name(supplied)
        if name is None:
            continue
        state = states.get(name, _SHELL_DYNAMIC)
        if state is _SHELL_CONFLICT:
            return ("hardcoded_authorization_bearer",)
        if (
            type(state) is tuple
            and len(state) == 2
            and state[0] == "static"
            and type(state[1]) is bytes
            and _literal_shell_bearer_is_hardcoded(state[1])
        ):
            return ("hardcoded_authorization_bearer",)
    return ()


_SHELL_TOKEN_ASSIGNMENT = re.compile(
    rb"([A-Za-z_][A-Za-z0-9_]*)=(.*)",
    re.DOTALL,
)
_SHELL_TOKEN_AUGMENT_OR_ARRAY = re.compile(
    rb"([A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]\r\n]+\])?(?:\+=|\[[^\]\r\n]+\]=)(.*)",
    re.DOTALL,
)
_SHELL_TOKEN_BEARER = re.compile(
    rb"authorization[ \t]*:[ \t]*bearer[ \t]+([^\s;|&)]+)",
    re.IGNORECASE,
)
_SHELL_WORD_VARIABLE = re.compile(
    rb"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))"
)


def _shell_expand_known_word(
    word: bytes,
    states: Mapping[bytes, object],
) -> tuple[bytes, bool, int]:
    parts: list[bytes] = []
    offset = 0
    conflict = False
    count = 0
    for match in _SHELL_WORD_VARIABLE.finditer(word):
        parts.append(word[offset : match.start()])
        name = match.group(1) or match.group(2)
        state = states.get(name, _SHELL_DYNAMIC)
        if type(state) is tuple and len(state) == 2 and state[0] == "static":
            parts.append(state[1])
        elif state is _SHELL_CONFLICT:
            parts.append(b"<UNKNOWN>")
            conflict = True
        else:
            parts.append(b"<DYNAMIC>")
        offset = match.end()
        count += 1
    parts.append(word[offset:])
    return b"".join(parts), conflict, count


def _shell_assignment_target(
    supplied: bytes,
    states: Mapping[bytes, object],
) -> bytes | None:
    if re.fullmatch(rb"[A-Za-z_][A-Za-z0-9_]*", supplied):
        return supplied
    reference = _shell_variable_name(supplied)
    if reference is None:
        return None
    state = states.get(reference)
    if (
        type(state) is tuple
        and len(state) == 2
        and state[0] == "static"
        and re.fullmatch(rb"[A-Za-z_][A-Za-z0-9_]*", state[1])
    ):
        return state[1]
    return None


def _shell_word_rules(path: str, value: bytes) -> tuple[str, ...]:
    """Conservatively inspect normalized shell words without executing them."""

    if not _is_shell_source(path, value):
        return ()
    try:
        source = value.decode("utf-8", errors="strict")
        lexer = shlex.shlex(source, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        words = tuple(token.encode("utf-8", errors="strict") for token in lexer)
    except (UnicodeDecodeError, UnicodeEncodeError, ValueError):
        folded = value.lower()
        if b"bearer" in folded and (
            b"authorization" in folded
            or (b"author" in folded and b"ization" in folded)
        ):
            return ("hardcoded_authorization_bearer",)
        return ()

    safe_runtime_fd_bearer = _shell_has_only_safe_runtime_fd_bearer(path, value)
    safe_unexported_aliases = _shell_safe_unexported_aliases(value)
    states: dict[bytes, object] = {}
    for word_index, word in enumerate(words):
        mutation = _SHELL_TOKEN_AUGMENT_OR_ARRAY.fullmatch(word)
        if mutation is not None:
            states[mutation.group(1)] = _SHELL_CONFLICT
            continue
        if word == b"printf" and word_index + 2 < len(words) and words[word_index + 1] == b"-v":
            target = words[word_index + 2]
            if re.fullmatch(rb"[A-Za-z_][A-Za-z0-9_]*", target):
                states[target] = _SHELL_CONFLICT
        assignment = _SHELL_TOKEN_ASSIGNMENT.fullmatch(word)
        if assignment is None:
            continue
        name, supplied = assignment.groups()
        if not supplied:
            candidate: object = _SHELL_CONFLICT
        elif _shell_variable_name(supplied) is not None:
            # Quote removal makes a single-quoted literal indistinguishable
            # from an expansion.  Treat aliases as unknown; direct external
            # variables at the header sink remain the only safe shell form.
            candidate = (
                _SHELL_DYNAMIC
                if name in safe_unexported_aliases
                else _SHELL_CONFLICT
            )
        elif b"$" in supplied or b"`" in supplied or b"\\" in supplied:
            candidate = _SHELL_CONFLICT
        else:
            candidate = _shell_static(supplied)
        previous = states.get(name)
        if previous is None:
            states[name] = candidate
        elif previous != candidate:
            states[name] = _SHELL_CONFLICT

    for word_index, word in enumerate(words):
        if word == b"printf" and word_index + 2 < len(words) and words[word_index + 1] == b"-v":
            target = _shell_assignment_target(words[word_index + 2], states)
            if target is not None:
                states[target] = _SHELL_CONFLICT
        if word in {b"declare", b"typeset", b"readonly", b"local"}:
            cursor = word_index + 1
            while cursor < len(words) and words[cursor].startswith(b"-"):
                cursor += 1
            if cursor < len(words):
                declaration = words[cursor]
                indirect = re.fullmatch(
                    rb"(\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*))(?:\+?=).+",
                    declaration,
                )
                if indirect is not None:
                    target = _shell_assignment_target(indirect.group(1), states)
                    if target is not None:
                        states[target] = _SHELL_CONFLICT
        if word in {b"read", b"readarray", b"mapfile"}:
            cursor = word_index + 1
            targets: list[bytes] = []
            here_string = False
            while cursor < len(words) and words[cursor] not in {b";", b"&", b"|", b"(" , b")"}:
                candidate = words[cursor]
                if candidate == b"<<<" or candidate.startswith(b"<<<"):
                    here_string = True
                    break
                if not candidate.startswith(b"-"):
                    target = _shell_assignment_target(candidate, states)
                    if target is not None:
                        targets.append(target)
                cursor += 1
            if here_string:
                for target in targets:
                    states[target] = _SHELL_CONFLICT

    for word_index, word in enumerate(words):
        header_arguments: list[bytes] = []
        if word in {b"-H", b"--header"} and word_index + 1 < len(words):
            header_arguments.append(words[word_index + 1])
        elif word.startswith(b"--header="):
            header_arguments.append(word.split(b"=", 1)[1])
        elif word.startswith(b"-H") and len(word) > 2:
            header_arguments.append(word[2:])
        for header in header_arguments:
            if header == b"@-":
                continue
            expanded, conflict, reference_count = _shell_expand_known_word(
                header,
                states,
            )
            if reference_count == 0:
                continue
            if _shell_variable_name(header) is not None and not conflict:
                state = states.get(_shell_variable_name(header), _SHELL_DYNAMIC)
                if state is _SHELL_DYNAMIC:
                    continue
            if conflict or _contains_hardcoded_bearer(expanded):
                return ("hardcoded_authorization_bearer",)

    for word_index, word in enumerate(words):
        for sink in _SHELL_TOKEN_BEARER.finditer(word):
            supplied = sink.group(1)
            variable = _shell_variable_name(supplied)
            if variable is not None:
                state = states.get(variable, _SHELL_DYNAMIC)
                if state is _SHELL_DYNAMIC:
                    continue
                return ("hardcoded_authorization_bearer",)
            normalized_placeholder = supplied[:-1] if supplied[-1:] in {b'"', b"'"} else supplied
            if safe_runtime_fd_bearer and re.fullmatch(
                rb"%s(?:\\n|n)?",
                normalized_placeholder,
            ):
                continue
            if any(pattern.fullmatch(supplied) for pattern in _DYNAMIC_BEARER_VALUES[3:]):
                continue
            return ("hardcoded_authorization_bearer",)
    return ()


def _python_constant_bytes(node: ast.AST) -> bytes | None:
    if isinstance(node, ast.Constant):
        if type(node.value) is str:
            try:
                return node.value.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                return None
        if type(node.value) is bytes:
            return node.value
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _python_constant_bytes(node.left)
        right = _python_constant_bytes(node.right)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.JoinedStr):
        values: list[bytes] = []
        for item in node.values:
            if not isinstance(item, ast.Constant) or type(item.value) is not str:
                return None
            try:
                values.append(item.value.encode("utf-8", errors="strict"))
            except UnicodeEncodeError:
                return None
        return b"".join(values)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and not node.keywords
        and len(node.args) == 1
        and isinstance(node.args[0], (ast.Tuple, ast.List))
    ):
        separator = _python_constant_bytes(node.func.value)
        parts = tuple(_python_constant_bytes(item) for item in node.args[0].elts)
        if separator is not None and all(part is not None for part in parts):
            return separator.join(part for part in parts if part is not None)
    return None


def _python_static_value(
    node: ast.AST,
    constants: Mapping[str, object],
) -> bytes | None:
    direct = _python_constant_bytes(node)
    if direct is not None:
        return direct
    if isinstance(node, ast.Name):
        value = constants.get(node.id)
        return value if type(value) is bytes else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _python_static_value(node.left, constants)
        right = _python_static_value(node.right, constants)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.JoinedStr):
        parts: list[bytes] = []
        for item in node.values:
            if isinstance(item, ast.Constant):
                value = _python_constant_bytes(item)
            elif (
                isinstance(item, ast.FormattedValue)
                and isinstance(item.value, ast.Name)
                and item.format_spec is None
            ):
                candidate = constants.get(item.value.id)
                value = candidate if type(candidate) is bytes else None
            else:
                value = None
            if value is None:
                return None
            parts.append(value)
        return b"".join(parts)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and not node.keywords
        and len(node.args) == 1
        and isinstance(node.args[0], (ast.Tuple, ast.List))
    ):
        separator = _python_static_value(node.func.value, constants)
        parts = tuple(_python_static_value(item, constants) for item in node.args[0].elts)
        if separator is not None and all(part is not None for part in parts):
            return separator.join(part for part in parts if part is not None)
    return None


def _static_bearer_value_is_hardcoded(value: bytes) -> bool:
    match = re.fullmatch(rb"[ \t]*bearer[ \t]+([^\s]+)[ \t]*", value, re.I)
    # This function is used only after Python AST/dataflow has proved that the
    # whole Authorization value is static executable code.  Documentation
    # placeholders are safe in raw prose, but are not a dynamic credential
    # source when they flow through an executable header sink.
    return match is not None


_PYTHON_SCOPES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _python_scope_nodes(scope: ast.AST) -> tuple[ast.AST, ...]:
    values: list[ast.AST] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _PYTHON_SCOPES):
                continue
            values.append(child)
            visit(child)

    visit(scope)
    return tuple(values)


def _python_direct_nested_scopes(scope: ast.AST) -> tuple[ast.AST, ...]:
    values: list[ast.AST] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _PYTHON_SCOPES):
                values.append(child)
            else:
                visit(child)

    visit(scope)
    return tuple(values)


def _python_parameter_names(scope: ast.AST) -> set[str]:
    if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        return set()
    arguments = scope.args
    return {
        item.arg
        for item in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
            *((arguments.vararg,) if arguments.vararg is not None else ()),
            *((arguments.kwarg,) if arguments.kwarg is not None else ()),
        )
    }


def _python_scope_has_trusted_os_binding(scope: ast.AST, inherited: bool) -> bool:
    nodes = _python_scope_nodes(scope)
    exact_import = any(
        isinstance(node, ast.Import)
        and any(alias.name == "os" and alias.asname in {None, "os"} for alias in node.names)
        for node in nodes
    )
    rebound = "os" in _python_parameter_names(scope)
    rebound = rebound or any(
        isinstance(node, ast.Name)
        and node.id == "os"
        and isinstance(node.ctx, (ast.Store, ast.Del))
        for node in nodes
    )
    rebound = rebound or any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name == "os"
        for node in _python_direct_nested_scopes(scope)
    )
    rebound = rebound or any(
        isinstance(node, (ast.Import, ast.ImportFrom))
        and any(
            (alias.asname or alias.name.split(".", 1)[0]) == "os"
            and not (
                isinstance(node, ast.Import)
                and alias.name == "os"
                and alias.asname in {None, "os"}
            )
            for alias in node.names
        )
        for node in nodes
    )
    rebound = rebound or any(
        isinstance(node, (ast.Attribute, ast.Subscript))
        and isinstance(node.ctx, (ast.Store, ast.Del))
        and any(isinstance(item, ast.Name) and item.id == "os" for item in ast.walk(node))
        for node in nodes
    )
    rebound = rebound or any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"setattr", "delattr"}
        and bool(node.args)
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "os"
        for node in nodes
    )
    return bool((inherited or exact_import) and not rebound)


def _python_environment_read(node: ast.AST, trusted_os: bool) -> bool:
    if not trusted_os:
        return False
    environ = (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "os"
        and node.value.attr == "environ"
        and isinstance(node.slice, ast.Constant)
        and type(node.slice.value) is str
    )
    environ_get = (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Attribute)
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "os"
        and node.func.value.attr == "environ"
        and not node.keywords
        and len(node.args) in {1, 2}
        and (
            len(node.args) == 1
            or (
                isinstance(node.args[1], ast.Constant)
                and node.args[1].value in {None, ""}
            )
        )
    )
    return bool(environ or environ_get)


def _python_unsafe_parameters(
    scope: ast.AST,
    tree: ast.AST,
    constants: Mapping[str, object],
) -> frozenset[str]:
    if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        return frozenset()
    arguments = scope.args
    positional = (*arguments.posonlyargs, *arguments.args)
    unsafe: set[str] = {
        argument.arg
        for argument in positional[len(positional) - len(arguments.defaults) :]
    }
    unsafe.update(
        argument.arg
        for argument, default in zip(
            arguments.kwonlyargs,
            arguments.kw_defaults,
            strict=True,
        )
        if default is not None
    )
    if isinstance(scope, ast.Lambda):
        return frozenset(unsafe)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        direct_name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else None
        )
        if direct_name != scope.name:
            continue
        for argument, supplied in zip(positional, node.args):
            if _python_static_value(supplied, constants) is not None:
                unsafe.add(argument.arg)
        keyword_values = {
            keyword.arg: keyword.value
            for keyword in node.keywords
            if keyword.arg is not None
        }
        for argument in (*positional, *arguments.kwonlyargs):
            supplied = keyword_values.get(argument.arg)
            if supplied is not None and _python_static_value(supplied, constants) is not None:
                unsafe.add(argument.arg)
    return frozenset(unsafe)


def _python_dynamic_authorization_is_safe(
    node: ast.AST,
    safe_runtime_names: frozenset[str],
) -> bool:
    if isinstance(node, ast.Name):
        return node.id in safe_runtime_names
    if isinstance(node, ast.JoinedStr) and len(node.values) == 2:
        prefix, supplied = node.values
        return bool(
            isinstance(prefix, ast.Constant)
            and type(prefix.value) is str
            and re.fullmatch(r"[ \t]*Bearer[ \t]+", prefix.value, re.I)
            and isinstance(supplied, ast.FormattedValue)
            and supplied.conversion == -1
            and supplied.format_spec is None
            and isinstance(supplied.value, ast.Name)
            and supplied.value.id in safe_runtime_names
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        prefix = _python_constant_bytes(node.left)
        return bool(
            prefix is not None
            and re.fullmatch(rb"[ \t]*Bearer[ \t]+", prefix, re.I)
            and isinstance(node.right, ast.Name)
            and node.right.id in safe_runtime_names
        )
    return False


def _python_has_bearer_shape(node: ast.AST) -> bool:
    return any(
        (literal := _python_constant_bytes(item)) is not None
        and re.search(rb"(?:^|[ \t])bearer[ \t]+", literal, re.I) is not None
        for item in ast.walk(node)
    )


def _python_authorization_key(value: bytes | None) -> bool:
    return value is not None and value.lower() == b"authorization"


def _python_authorization_scope_rules(
    scope: ast.AST,
    inherited_constants: Mapping[str, bytes],
    inherited_conflicts: frozenset[str],
    inherited_pending: frozenset[str],
    *,
    trusted_os: bool,
    unsafe_parameters: frozenset[str],
) -> tuple[set[str], dict[str, bytes], frozenset[str], frozenset[str]]:
    conflict = object()
    nodes = _python_scope_nodes(scope)
    assignments = [
        node
        for node in nodes
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    assignment_names = {
        target.id
        for assignment in assignments
        for target in (
            assignment.targets
            if isinstance(assignment, ast.Assign)
            else (assignment.target,)
        )
        if isinstance(target, ast.Name)
    }
    unsafe_mutated_names = {
        node.target.id
        for node in nodes
        if isinstance(node, (ast.AugAssign, ast.NamedExpr))
        and isinstance(node.target, ast.Name)
    }
    assignment_counts: dict[str, int] = {}
    for assignment in assignments:
        targets = (
            assignment.targets
            if isinstance(assignment, ast.Assign)
            else (assignment.target,)
        )
        for target in targets:
            if isinstance(target, ast.Name):
                assignment_counts[target.id] = assignment_counts.get(target.id, 0) + 1
    parameters = _python_parameter_names(scope)
    safe_environment_names = {
        target.id
        for assignment in assignments
        if assignment.value is not None
        and _python_environment_read(assignment.value, trusted_os)
        for target in (
            assignment.targets
            if isinstance(assignment, ast.Assign)
            else (assignment.target,)
        )
        if isinstance(target, ast.Name)
        and assignment_counts.get(target.id) == 1
    }
    safe_runtime_names = frozenset(
        (parameters - unsafe_parameters - assignment_names - unsafe_mutated_names)
        | (safe_environment_names - unsafe_mutated_names)
    )
    shadows = assignment_names | parameters | unsafe_mutated_names
    constants: dict[str, object] = {
        name: value
        for name, value in inherited_constants.items()
        if name not in shadows
    }
    conflict_names = set(inherited_conflicts - shadows)
    conflict_names.update(unsafe_mutated_names)
    pending_static_names = {
        target.id
        for assignment in assignments
        if isinstance(
            assignment.value,
            (ast.Constant, ast.Name, ast.BinOp, ast.JoinedStr),
        )
        for target in (
            assignment.targets
            if isinstance(assignment, ast.Assign)
            else (assignment.target,)
        )
        if isinstance(target, ast.Name)
    }
    pending_static_names.update(inherited_pending - shadows)
    for _round in range(max(1, 2 * len(assignment_names) + 1)):
        changed = False
        for assignment in assignments:
            if assignment.value is None:
                continue
            resolved = _python_static_value(assignment.value, constants)
            if resolved is None:
                continue
            targets = (
                assignment.targets
                if isinstance(assignment, ast.Assign)
                else (assignment.target,)
            )
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                previous = constants.get(target.id)
                if previous is conflict:
                    continue
                if previous is None:
                    constants[target.id] = resolved
                    changed = True
                elif previous != resolved:
                    constants[target.id] = conflict
                    conflict_names.add(target.id)
                    changed = True
        if not changed:
            break
    else:
        raise RuntimeError("python secret-scan dataflow did not converge")

    authorization_values: list[tuple[ast.AST, bool]] = []
    header_container_calls = {
        id(assignment.value)
        for assignment in nodes
        if isinstance(assignment, (ast.Assign, ast.AnnAssign))
        and assignment.value is not None
        and isinstance(assignment.value, ast.Call)
        for target in (
            assignment.targets
            if isinstance(assignment, ast.Assign)
            else (assignment.target,)
        )
        if (
            isinstance(target, ast.Name)
            and "header" in target.id.lower()
        )
        or (
            isinstance(target, ast.Attribute)
            and "header" in target.attr.lower()
        )
    }

    def add_authorization_value(key: ast.AST | None, supplied: ast.AST) -> bool:
        key_value = _python_static_value(key, constants) if key is not None else None
        if not _python_authorization_key(key_value):
            return False
        authorization_values.append((supplied, key_value == b"Authorization"))
        return True

    def add_iterable_pairs(candidate: ast.AST) -> bool:
        if not isinstance(candidate, (ast.Tuple, ast.List, ast.Set)):
            return False
        recognized = False
        for item in candidate.elts:
            if not isinstance(item, (ast.Tuple, ast.List)) or len(item.elts) != 2:
                continue
            recognized = add_authorization_value(item.elts[0], item.elts[1]) or recognized
        return recognized

    for node in nodes:
        if isinstance(node, ast.Dict):
            for key, supplied in zip(node.keys, node.values, strict=True):
                add_authorization_value(key, supplied)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_header"
            and len(node.args) >= 2
        ):
            add_authorization_value(node.args[0], node.args[1])
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "dict"
        ):
            for candidate in node.args:
                add_iterable_pairs(candidate)
            if (
                id(node) in header_container_calls
                and node.args
                and any(isinstance(candidate, ast.Call) for candidate in node.args)
            ):
                authorization_values.append((node.args[0], True))
            authorization_values.extend(
                (keyword.value, keyword.arg == "Authorization")
                for keyword in node.keywords
                if keyword.arg is not None
                and keyword.arg.lower() == "authorization"
            )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
        ):
            recognized = any(add_iterable_pairs(candidate) for candidate in node.args)
            authorization_values.extend(
                (keyword.value, keyword.arg == "Authorization")
                for keyword in node.keywords
                if keyword.arg is not None
                and keyword.arg.lower() == "authorization"
            )
            if (
                not recognized
                and node.args
                and isinstance(node.func.value, ast.Name)
                and "header" in node.func.value.id.lower()
                and any(isinstance(candidate, ast.Call) for candidate in node.args)
            ):
                authorization_values.append((node.args[0], True))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setdefault"
            and len(node.args) >= 2
        ):
            add_authorization_value(node.args[0], node.args[1])
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript):
                    add_authorization_value(target.slice, node.value)

    rules: set[str] = set()
    for supplied, canonical_key in authorization_values:
        resolved = _python_static_value(supplied, constants)
        if resolved is None:
            if _python_dynamic_authorization_is_safe(supplied, safe_runtime_names):
                continue
            # Authorization values are security-sensitive sinks.  Statically
            # ambiguous names and every unproved value fail closed; only the
            # small exact runtime forms above are safe.
            if canonical_key or _python_has_bearer_shape(supplied):
                rules.add("hardcoded_authorization_bearer")
            continue
        if _static_bearer_value_is_hardcoded(resolved):
            rules.add("hardcoded_authorization_bearer")
        if _contains_validated_compact_jwt(resolved):
            rules.add("validated_compact_jwt")
    unique_constants = {
        name: value for name, value in constants.items() if type(value) is bytes
    }
    return (
        rules,
        unique_constants,
        frozenset(conflict_names),
        frozenset(pending_static_names),
    )


def _python_authorization_rules(tree: ast.AST) -> tuple[str, ...]:
    rules: set[str] = set()

    def analyze(
        scope: ast.AST,
        inherited_constants: Mapping[str, bytes],
        inherited_conflicts: frozenset[str],
        inherited_pending: frozenset[str],
        inherited_trusted_os: bool,
    ) -> None:
        trusted_os = _python_scope_has_trusted_os_binding(
            scope,
            inherited_trusted_os,
        )
        unsafe_parameters = _python_unsafe_parameters(
            scope,
            tree,
            inherited_constants,
        )
        scope_rules, constants, conflicts, pending = _python_authorization_scope_rules(
            scope,
            inherited_constants,
            inherited_conflicts,
            inherited_pending,
            trusted_os=trusted_os,
            unsafe_parameters=unsafe_parameters,
        )
        rules.update(scope_rules)
        for nested in _python_direct_nested_scopes(scope):
            analyze(nested, constants, conflicts, pending, trusted_os)

    analyze(tree, {}, frozenset(), frozenset(), False)
    return tuple(sorted(rules))


_PYTHON_LITERAL_PREFIX = re.compile(
    rb"(?i)(?<![A-Za-z0-9_])(?:br|rb|fr|rf|[rubf])(?=[\"'])"
)
_RAW_FALLBACK_SEPARATORS = re.compile(rb"[\x00-\x20\"'+\\,;()\[\]{}]")
_RAW_FALLBACK_BEARER = re.compile(
    rb"authorization:?bearer[A-Za-z0-9_./+%$<>=-]{4,}",
    re.IGNORECASE,
)
_RAW_FALLBACK_PRIVATE_KEY = re.compile(
    rb"-----BEGIN[A-Z]*PRIVATEKEY-----",
)


def _python_raw_fallback_rules(value: bytes) -> tuple[str, ...]:
    """Scan a conservative byte projection when Python cannot be parsed.

    The projection removes only source-level literal boundaries and common
    expression separators.  It never evaluates or executes the input.  This
    deliberately joins adjacent literal fragments so a syntax error, invalid
    encoding, or split string cannot hide a high-confidence credential shape.
    """

    without_prefixes = _PYTHON_LITERAL_PREFIX.sub(b"", value)
    projected = _RAW_FALLBACK_SEPARATORS.sub(b"", without_prefixes)
    rules: set[str] = set()
    for rule, pattern in _SIGNATURE_RULES:
        if pattern.search(projected):
            rules.add(rule)
    if _RAW_FALLBACK_PRIVATE_KEY.search(projected):
        rules.add("private_key_header")
    if _RAW_FALLBACK_BEARER.search(projected):
        rules.add("hardcoded_authorization_bearer")
    if _contains_validated_compact_jwt(projected):
        rules.add("validated_compact_jwt")
    return tuple(sorted(rules))


def _python_parse_failure_rules(rule: str, value: bytes) -> tuple[str, ...]:
    rules = {rule}
    try:
        rules.update(_python_raw_fallback_rules(value))
    except Exception:
        # The finding is intentionally stable and redacted.  A scanner defect
        # remains a rejecting, auditable result without exposing input bytes or
        # exception details in evidence logs.
        rules.add("secret_scanner_internal_error")
    return tuple(sorted(rules))


def _python_static_literal_rules(path: str, value: bytes) -> tuple[str, ...]:
    if not path.lower().endswith((".py", ".pyw", ".pyi")):
        return ()
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(value).readline)
    except (SyntaxError, UnicodeDecodeError, LookupError):
        return _python_parse_failure_rules("python_encoding_error", value)
    try:
        source = value.decode(encoding, errors="strict")
    except (UnicodeDecodeError, LookupError):
        return _python_parse_failure_rules("python_decode_error", value)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(source)
    except SyntaxError:
        return _python_parse_failure_rules("python_syntax_error", value)
    rules: set[str] = set()
    for node in ast.walk(tree):
        literal = _python_constant_bytes(node)
        if literal is None:
            continue
        if _contains_validated_compact_jwt(literal):
            rules.add("validated_compact_jwt")
        if _contains_hardcoded_bearer(literal):
            rules.add("hardcoded_authorization_bearer")
    rules.update(_python_authorization_rules(tree))
    return tuple(sorted(rules))


def _signature_rules(value: bytes) -> tuple[str, ...]:
    rules: list[str] = []
    for rule, pattern in _SIGNATURE_RULES:
        if pattern.search(value):
            rules.append(rule)
    return tuple(rules)


def scan_bytes(path: str, value: bytes) -> tuple[SecretFinding, ...]:
    rules: set[str] = set()
    scanners = (
        lambda: _signature_rules(value),
        lambda: (
            ("validated_compact_jwt",)
            if _contains_validated_compact_jwt(value)
            else ()
        ),
        lambda: (
            ("hardcoded_authorization_bearer",)
            if _contains_hardcoded_bearer(
                value,
                allow_printf_placeholder=_shell_has_only_safe_runtime_fd_bearer(
                    path,
                    value,
                ),
            )
            else ()
        ),
        lambda: _python_static_literal_rules(path, value),
        lambda: _shell_static_assignment_rules(path, value),
        lambda: _shell_word_rules(path, value),
    )
    for scanner in scanners:
        try:
            rules.update(scanner())
        except Exception:
            # Scanner faults are themselves rejecting findings.  Do not print
            # the exception or source bytes: the stable rule is sufficient for
            # the audit ledger and cannot leak a credential through diagnostics.
            rules.add("secret_scanner_internal_error")
    return tuple(SecretFinding(path=path, rule=rule) for rule in sorted(rules))


def scan_frozen_payload(
    frozen: Mapping[str, bytes],
) -> tuple[SecretFinding, ...]:
    return tuple(
        finding
        for path, value in sorted(frozen.items())
        for finding in scan_bytes(path, value)
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="payload files to scan")
    args = parser.parse_args(argv)

    findings: list[SecretFinding] = []
    read_errors: list[SecretFinding] = []
    for supplied in args.paths:
        try:
            value = Path(supplied).read_bytes()
        except (OSError, ValueError):
            read_errors.append(SecretFinding(path=supplied, rule="payload_read_error"))
            continue
        findings.extend(scan_bytes(supplied, value))

    for finding in sorted(findings, key=lambda item: (item.path, item.rule)):
        print(
            json.dumps(
                {"path": finding.path, "rule": finding.rule},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    for finding in sorted(read_errors, key=lambda item: (item.path, item.rule)):
        print(
            json.dumps(
                {"path": finding.path, "rule": finding.rule},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
    if read_errors:
        return 2
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
