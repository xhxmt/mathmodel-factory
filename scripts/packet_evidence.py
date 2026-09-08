"""Shared resolution of complete packet evidence and content aliases.

An alias names one direct canonical chunk, never a second copy of that chunk.
Grounding still verifies the actual canonical context bytes and exact quote.
"""
from __future__ import annotations

import re
try:
    from scripts.numpy_evidence_view import SUFFIXES as NUMPY_SUFFIXES, complete_binding
except ModuleNotFoundError:  # direct script execution
    from numpy_evidence_view import SUFFIXES as NUMPY_SUFFIXES, complete_binding

ALIAS_CONTRACT = "judge-packet-alias-v1"
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
        return (item is not None and item.get("status") == "included"
                and (("binary_review" not in item and not path.lower().endswith(tuple(NUMPY_SUFFIXES)))
                     or complete_binding(item)))
