"""Shared resolution of complete packet evidence and content aliases.

An alias names one direct canonical chunk, never a second copy of that chunk.
Grounding still verifies the actual canonical context bytes and exact quote.
"""
from __future__ import annotations

import re
try:
    from scripts.document_evidence_view import SUFFIXES as DOCUMENT_SUFFIXES, complete_binding as document_complete
except ModuleNotFoundError:
    from document_evidence_view import SUFFIXES as DOCUMENT_SUFFIXES, complete_binding as document_complete
try:
    from scripts.numpy_evidence_view import SUFFIXES as NUMPY_SUFFIXES, complete_binding
except ModuleNotFoundError:  # direct script execution
    from numpy_evidence_view import SUFFIXES as NUMPY_SUFFIXES, complete_binding

ALIAS_CONTRACT = "judge-packet-alias-v1"
# v2: a requirement may also carry ``attested_outputs`` (path + receipt digest),
# satisfied by the manifest digest rather than by included content.
COMPLETENESS_CONTRACT_VERSION = "judge-packet-completeness-v2"
#: v1 manifests carry no attested outputs and evaluate identically under v2.
#: The one definition the packet builder, the pre-dispatch check and the
#: aggregator share, so the gates cannot disagree.
COMPLETENESS_CONTRACT_VERSIONS = frozenset({"judge-packet-completeness-v1", COMPLETENESS_CONTRACT_VERSION})


def supported_completeness_version(value) -> bool:
    """Whether ``value`` names a supported completeness contract.

    A malformed value (a list, a dict) is unsupported, never a ``TypeError``.
    """

    return isinstance(value, str) and value in COMPLETENESS_CONTRACT_VERSIONS
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class PacketEvidence:
    def __init__(self, files):
        self.by_path = {}
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise ValueError("invalid packet file identity")
            path = item["path"]
            if not path or path in self.by_path:
                raise ValueError(f"duplicate or empty packet path: {path}")
            self.by_path[path] = item
        for path, item in self.by_path.items():
            if item.get("status") != "alias":
                continue
            canonical = self.by_path.get(item.get("alias_of"))
            if (canonical is None or canonical is item
                    or canonical.get("status") not in {"included", "truncated"}):
                raise ValueError(f"alias must name a direct active canonical file: {path}")
            digest = item.get("sha256")
            if (not isinstance(digest, str) or not _SHA256.fullmatch(digest)
                    or digest != canonical.get("sha256")
                    or type(item.get("size")) is not int
                    or item["size"] != canonical.get("size")
                    or item["size"] < 0):
                raise ValueError(f"alias source content differs from canonical file: {path}")
            chunk = item.get("alias_chunk_id")
            if (not isinstance(chunk, str) or not _SHA256.fullmatch(chunk)
                    or chunk != canonical.get("chunk_id")
                    or item.get("included_bytes") != 0):
                raise ValueError(f"alias chunk binding is invalid: {path}")
            aliases = canonical.get("aliases")
            if not isinstance(aliases, list) or aliases.count(path) != 1:
                raise ValueError(f"alias is not registered by its canonical file: {path}")
            if canonical["status"] == "included":
                if "binary_review" in canonical:
                    complete = complete_binding(canonical) and item.get("binary_review") == canonical["binary_review"]
                elif "document_review" in canonical:
                    complete = document_complete(canonical) and item.get("document_review") == canonical["document_review"]
                else:
                    complete = (canonical.get("included_sha256") == digest
                                and canonical.get("included_bytes") == item["size"])
                if not complete:
                    raise ValueError(f"alias canonical content is not complete: {path}")
        for path, item in self.by_path.items():
            aliases = item.get("aliases", [])
            if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
                raise ValueError(f"invalid canonical aliases: {path}")
            if len(set(aliases)) != len(aliases) or any(
                self.by_path.get(alias, {}).get("status") != "alias"
                or self.by_path[alias].get("alias_of") != path for alias in aliases
            ):
                raise ValueError(f"canonical aliases do not match manifest entries: {path}")

    def resolve(self, path):
        item = self.by_path.get(path)
        if item is not None and item.get("status") == "alias":
            return self.by_path[item["alias_of"]]
        return item

    def complete(self, path):
        item = self.resolve(path)
        if item is not None and item.get("content_location") == "asset":
            relative = item.get("asset_path")
            mode = item.get("asset_quote_mode")
            complete_asset = (
                item.get("status") == "included"
                and isinstance(relative, str) and relative.startswith("assets/")
                and len(relative.split("/")) == 2 and "\\" not in relative
                and relative.split("/")[1] not in {"", ".", ".."}
                and type(item.get("size")) is int and item["size"] >= 0
                and item.get("asset_size") == item["size"]
                and isinstance(item.get("sha256"), str) and bool(_SHA256.fullmatch(item["sha256"]))
                and item.get("asset_sha256") == item["sha256"]
                and isinstance(item.get("included_sha256"), str)
                and bool(_SHA256.fullmatch(item["included_sha256"]))
                and type(item.get("included_bytes")) is int and item["included_bytes"] >= 0
            )
            if mode == "text":
                return complete_asset and item["included_sha256"] == item["sha256"] and item["included_bytes"] == item["size"]
            return complete_asset and mode == "descriptor"
        if item is not None and ("document_review" in item or path.lower().endswith(tuple(DOCUMENT_SUFFIXES))):
            return item.get("status") == "included" and document_complete(item)
        return (item is not None and item.get("status") == "included"
                and (("binary_review" not in item and not path.lower().endswith(tuple(NUMPY_SUFFIXES)))
                     or complete_binding(item)))

    def attested(self, path, sha256):
        """A receipt-attested output: listed in the packet with the receipt's bytes.

        Its content need not be in the context; the solver completion receipt
        binds those bytes, and the manifest entry must carry the same digest.
        """
        item = self.by_path.get(path)
        return (item is not None and isinstance(sha256, str) and bool(_SHA256.fullmatch(sha256))
                and item.get("sha256") == sha256)


def evaluate_requirement(evidence, requirement):
    """Return ``(satisfied_paths, satisfied_attested_paths, satisfied)``.

    ``paths`` must be fully included. ``attested_outputs`` (completeness v2) are
    satisfied by an exact receipt digest instead of their content.
    """
    paths = requirement.get("paths")
    attested = requirement.get("attested_outputs", [])
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        raise ValueError("invalid completeness paths")
    if not isinstance(attested, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("path"), str)
        or not isinstance(item.get("sha256"), str) for item in attested
    ):
        raise ValueError("invalid attested outputs")
    satisfied_paths = [path for path in paths if evidence.complete(path)]
    satisfied_attested = [item["path"] for item in attested
                          if evidence.attested(item["path"], item["sha256"])]
    satisfied = (bool(paths) and len(satisfied_paths) == len(paths)
                 and len(satisfied_attested) == len(attested)
                 and not requirement.get("binding_error"))
    return satisfied_paths, satisfied_attested, satisfied
