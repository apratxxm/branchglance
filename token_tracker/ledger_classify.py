"""Work-kind classification for ledger threads.

Order (first match wins):
1. user override (stored per thread, applied at rollup time)
2. segment tag: a user message line exactly `kind: <word>` (per job segment)
3. specific evidence: task words in the job slug, agent type, exec review, read-only sandbox
4. generic worker names in the slug (terra/haiku/sonnet -> build, sol -> review)
5. top-level interactive session -> lead
6. other

Each result records the rule id, its evidence source and inferred=True. Only the job
slug is kept: callers classify titles and prompts in memory and never store them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

CLASSIFICATION_VERSION = 1
KINDS = ("build", "review", "check", "research", "chore", "design", "explore", "lead", "other")
DEFAULT_KIND_REGISTRY = {
    "build": "Implementation and creation",
    "review": "Review and audit",
    "check": "Testing and verification",
    "research": "Research and investigation",
    "chore": "Maintenance and routine work",
    "lead": "Coordination and decisions",
    "design": "Design work",
    "explore": "Exploration",
}
TAG_KINDS = frozenset(DEFAULT_KIND_REGISTRY)
_KIND_WORD = re.compile(r"[a-z]{1,32}")
_TAG_LINE = re.compile(r"kind: ([a-z]{1,32})")
_REVIEW_TITLE = re.compile(r"\breview\b")

# A recognised job slug is <project>-<worker>-<task>, with a known generic worker name.
SLUG_PATTERN = re.compile(r"(?<![a-z0-9-])([a-z0-9]+-(?:terra|haiku|sonnet|sol)-[a-z0-9][a-z0-9-]{0,60})(?![a-z0-9-])")
WORKER_KIND = {"terra": "build", "haiku": "build", "sonnet": "build", "sol": "review"}
REVIEW_WORDS = frozenset({"review", "reviews", "audit"})
CHECK_WORDS = frozenset({"check", "checks", "verify", "test", "tests"})
AGENT_KIND = {"review": "review", "check": "check", "test": "check", "research": "research", "explore": "research"}


def load_kind_registry(path: str | Path) -> dict[str, str]:
    """Read kind definitions; a missing or invalid registry recognizes no tags.

    Only bounded lower-case words can become kinds. The reserved fallback ``other``
    is not a registrable tag. This function has no cache and performs no writes.
    """
    registry: dict[str, str] = {}
    try:
        entries = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return registry
    if not isinstance(entries, list):
        return registry
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kind, meaning = entry.get("kind"), entry.get("meaning")
        if (isinstance(kind, str) and _KIND_WORD.fullmatch(kind) and kind != "other"
                and isinstance(meaning, str) and meaning.strip()):
            registry[kind] = meaning
    return registry


def recognised_slug(text: Any) -> str | None:
    """The first job slug in text. Only the slug is returned; the text is discarded."""
    if not isinstance(text, str) or not text:
        return None
    match = SLUG_PATTERN.search(text[:4000].lower())
    return match.group(1)[:120] if match else None


def kind_tag(text: Any) -> str | None:
    """The segment kind from a user message, or None.

    Only a line that is exactly `kind: <word>` counts, outside fenced code blocks and not
    quoted (leading `>`). The text is parsed in memory and never stored.
    """
    if not isinstance(text, str) or not text:
        return None
    fenced = False
    for raw in text.splitlines()[:200]:
        line = raw.rstrip("\r")
        if line.strip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or line.startswith(">"):
            continue
        match = _TAG_LINE.fullmatch(line)
        if match:
            return match.group(1)
    return None


def title_has_review(text: Any) -> bool:
    """Whether a prompt says review. Only this flag is kept, never the text."""
    return isinstance(text, str) and bool(_REVIEW_TITLE.search(text.lower()))


def _result(kind: str, rule: str, source: str) -> dict[str, Any]:
    return {"kind": kind, "rule": rule, "source": source, "inferred": rule != "override",
            "classificationVersion": CLASSIFICATION_VERSION}


def classify(*, slug: str | None = None, agent_type: str | None = None, exec_review: bool = False,
             read_only: bool = False, interactive: bool = False, override: str | None = None,
             tag: str | None = None, review_title: bool = False,
             registry: dict[str, str] | None = None) -> dict[str, Any]:
    kinds = DEFAULT_KIND_REGISTRY if registry is None else registry
    if override == "other" or override in kinds:
        return _result(override, "override", "user")
    if isinstance(tag, str) and _KIND_WORD.fullmatch(tag):
        if tag == "other":
            return _result("other", "tag", "tag")
        if tag in kinds:
            return _result(tag, "tag", "tag")
        result = _result("other", "tag", "tag")
        result["rawKind"] = tag
        return result
    worker = None
    if slug:
        parts = slug.split("-")
        worker = parts[1] if len(parts) > 1 else None
        tokens = set(parts[2:])
        if tokens & REVIEW_WORDS:
            return _result("review", "task-review", "slug")
        if tokens & CHECK_WORDS:
            return _result("check", "task-check", "slug")
    if agent_type:
        for token in re.split(r"[-_]", agent_type.lower()):
            if token in AGENT_KIND:
                return _result(AGENT_KIND[token], "agent-type", "agent_type")
    if exec_review:
        return _result("review", "exec-review", "exec")
    if read_only:
        if review_title:
            return _result("review", "sandbox-review", "sandbox")
        return _result("research", "sandbox-read-only", "sandbox")
    if worker in WORKER_KIND:
        return _result(WORKER_KIND[worker], "worker-generic", "worker")
    if interactive:
        return _result("lead", "top-level-interactive", "top_level")
    return _result("other", "unclassified", "none")
