"""Pure task-memory retrieval policy for the MP-3 implementation.

The memory ledger deliberately exposes a correctness-oriented ``search``
operation.  This module adds the small amount of query planning and ranking
needed by a task without taking ownership of persistence or authorization.
Callers provide the ledger search function and, when explicit references or a
knowledge cutoff need to be resolved, small Core-owned callbacks.

The module is intentionally synchronous and side-effect free.  It never opens
SQLite, changes a ledger head, or treats a candidate as a published version.
All returned references are copied from ``MemoryVersion`` values that passed
the dataset/scope/condition checks in this module.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Literal, Protocol, cast

from operant.contracts.b2_1 import (
    CandidateReference,
    MemoryConditions,
    MemoryVersion,
    MemoryVersionRef,
    RecallRequest,
    Scope,
)

RETRIEVAL_STRATEGY_VERSION = "mp3.v1"
MAXIMUM_RECALL_SENSITIVITY = "internal"

_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff]+")
_ASCII_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/\\-]*")
_ASCII_WORD = re.compile(r"[A-Za-z0-9]+")
_WHITESPACE = re.compile(r"\s+")
_IDENTIFIER_PUNCTUATION = frozenset("._:/\\-")

Evidence = Literal[
    "user_asserted",
    "observed",
    "tested",
    "inferred",
    "legacy_unverified",
]


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """A bounded, grouped set of short queries.

    ``groups`` are ordered by retrieval value: exact identifiers, CJK phrases
    and short n-grams, then ordinary words/phrases.  Terms within a group are
    alternatives.  A backend that supports FTS5 can use :attr:`fts_queries`;
    the existing ledger adapter should issue the plain terms in
    :attr:`queries`, because its current ``search`` contract performs a
    bounded substring lookup.
    """

    groups: tuple[tuple[str, ...], ...]
    normalized_query: str
    group_labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.group_labels) not in (0, len(self.groups)):
            raise ValueError("group_labels must match groups")
        if any(not term for group in self.groups for term in group):
            raise ValueError("query groups cannot contain blank terms")

    @property
    def original_query(self) -> str:
        """Compatibility alias for callers that use the unsuffixed name."""

        return self.normalized_query

    @property
    def queries(self) -> tuple[str, ...]:
        """Return unique plain terms in deterministic group order."""

        return _unique(term for group in self.groups for term in group)

    @property
    def flat_terms(self) -> tuple[str, ...]:
        """Alias used by integrations that call plan terms ``flat_terms``."""

        return self.queries

    @property
    def fts_queries(self) -> tuple[str, ...]:
        """Return one safe FTS5 OR query for each group."""

        return tuple(compile_fts_query(group) for group in self.groups)

    @property
    def fts_groups(self) -> tuple[str, ...]:
        """Alias for :attr:`fts_queries`."""

        return self.fts_queries


@dataclass(frozen=True, slots=True)
class RetrievalPolicy:
    """Versioned, bounded candidate strategy.

    ``candidate_limit`` is the per-term ledger read limit.  The request's
    ``max_candidates`` remains the final output limit.  ``diversity_limit``
    caps automatically selected candidates whose primary source is the same;
    explicit references are always preferred, subject to the request limit.
    """

    version: str = RETRIEVAL_STRATEGY_VERSION
    candidate_limit: int = 40
    diversity_limit: int = 3
    max_query_groups: int = 8
    max_terms_per_group: int = 12
    min_score: float = 0.0
    allow_legacy: bool = False

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("retrieval policy version cannot be blank")
        if self.candidate_limit < 1:
            raise ValueError("candidate_limit must be positive")
        if self.diversity_limit < 1:
            raise ValueError("diversity_limit must be positive")
        if self.max_query_groups < 1:
            raise ValueError("max_query_groups must be positive")
        if self.max_terms_per_group < 1:
            raise ValueError("max_terms_per_group must be positive")
        if not isfinite(self.min_score) or not 0.0 <= self.min_score <= 1.0:
            raise ValueError("min_score must be between 0 and 1")

    @property
    def strategy_version(self) -> str:
        """Explicit alias for persistence/telemetry fields."""

        return self.version


@dataclass(frozen=True, slots=True)
class MemoryConditionContext:
    """Current facts used to evaluate a version's applicability conditions.

    A missing fact is different from a stored condition whose value is
    ``None``: the latter means "no restriction", while the former fails
    closed when the memory requires that fact.
    """

    commit_ref: str | None = None
    tree_digest: str | None = None
    file_fingerprints: Mapping[str, str] = field(default_factory=dict)
    environment_digest: str | None = None
    tool_versions: Mapping[str, str] = field(default_factory=dict)
    at: datetime | None = None


# Short aliases make the type useful to integrations without creating another
# public contract model.
ConditionContext = MemoryConditionContext
RetrievalConditions = MemoryConditionContext


class MemorySearch(Protocol):
    """The narrow existing Ledger ``search``/``query`` callable shape."""

    def __call__(
        self,
        dataset_id: str,
        text: str | None = None,
        *,
        scope: Scope | Mapping[str, Any] | None = None,
        limit: int = 100,
    ) -> Sequence[MemoryVersion]: ...


VersionResolver = Callable[[MemoryVersionRef], MemoryVersion | None]
VisibilityCheck = Callable[[MemoryVersion], bool]
CutoffResolver = Callable[[MemoryVersion], str | int | None]


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _normalise_query(query: str) -> str:
    normalized = unicodedata.normalize("NFKC", query)
    return _WHITESPACE.sub(" ", normalized).strip()


def _is_identifier(token: str) -> bool:
    if len(token) < 2:
        return False
    if _IDENTIFIER_PUNCTUATION.intersection(token):
        return True
    if any(character.isdigit() for character in token):
        return True
    has_upper = any(character.isupper() for character in token)
    has_lower = any(character.islower() for character in token)
    return has_upper and has_lower or token.isupper()


def _cjk_terms(run: str, *, limit: int) -> tuple[str, ...]:
    if len(run) <= 1:
        return (run,)
    values = [run]
    # Keep short Chinese words useful for a lexical index without sending the
    # complete task text as one impossible AND expression.  Bigram ordering is
    # stable and a whole phrase gets the strongest rank later.
    values.extend(run[index : index + 2] for index in range(len(run) - 1))
    return _unique(values)[:limit]


def _ordinary_terms(normalized: str, identifiers: set[str], *, limit: int) -> tuple[str, ...]:
    words = [match.group(0) for match in _ASCII_WORD.finditer(normalized)]
    plain_words = [word for word in words if word not in identifiers]
    if not plain_words:
        return ()
    values: list[str] = []
    # Phrases retain the useful relationship in a short query such as
    # ``legacy poetry install``.  Single words remain as recall fallbacks.
    values.extend(
        " ".join(plain_words[index : index + width])
        for width in (3, 2)
        for index in range(max(0, len(plain_words) - width + 1))
    )
    values.extend(plain_words)
    return _unique(values)[:limit]


def build_query_plan(
    query: str,
    *,
    max_query_groups: int = 8,
    max_terms_per_group: int = 12,
) -> QueryPlan:
    """Build a bounded plan that preserves CJK words and exact identifiers.

    The planner intentionally uses only the standard library.  It keeps
    identifiers such as ``PersistentContextComposer``, ``tests/test_api.py``
    and ``MAPLE-42`` intact, while adding CJK full phrases and bigrams for a
    lexical FTS backend.  The output is deterministic and contains no task
    specific fixture terms.
    """

    if max_query_groups < 1 or max_terms_per_group < 1:
        raise ValueError("query plan bounds must be positive")
    normalized = _normalise_query(query)
    if not normalized:
        raise ValueError("query cannot be blank")

    raw_tokens = [match.group(0) for match in _ASCII_TOKEN.finditer(normalized)]
    identifiers = {token for token in raw_tokens if _is_identifier(token)}
    identifier_terms = _unique(token for token in raw_tokens if token in identifiers)
    cjk_terms = _unique(
        term
        for match in _CJK_RUN.finditer(normalized)
        for term in _cjk_terms(match.group(0), limit=max_terms_per_group)
    )
    ordinary_terms = _ordinary_terms(
        normalized,
        identifiers,
        limit=max_terms_per_group,
    )

    groups: list[tuple[str, ...]] = []
    labels: list[str] = []
    for label, terms in (
        ("identifier", identifier_terms),
        ("cjk", cjk_terms),
        ("term", ordinary_terms),
    ):
        bounded = terms[:max_terms_per_group]
        if bounded:
            groups.append(bounded)
            labels.append(label)
        if len(groups) >= max_query_groups:
            break

    return QueryPlan(
        groups=tuple(groups),
        normalized_query=normalized,
        group_labels=tuple(labels),
    )


def compile_fts_query(group: Sequence[str]) -> str:
    """Compile one grouped term sequence into a safe FTS5 OR expression."""

    values = _unique(term.strip() for term in group if term.strip())
    if not values:
        raise ValueError("cannot compile an empty FTS group")
    return " OR ".join('"' + value.replace('"', '""') + '"' for value in values)


def _as_context(
    context: MemoryConditionContext | Mapping[str, Any] | None,
    *,
    now: datetime | None,
    commit_ref: str | None,
    tree_digest: str | None,
    file_fingerprints: Mapping[str, str] | None,
    environment_digest: str | None,
    tool_versions: Mapping[str, str] | None,
) -> MemoryConditionContext:
    if context is None:
        values: Mapping[str, Any] = {}
    elif isinstance(context, MemoryConditionContext):
        values = {
            "commit_ref": context.commit_ref,
            "tree_digest": context.tree_digest,
            "file_fingerprints": context.file_fingerprints,
            "environment_digest": context.environment_digest,
            "tool_versions": context.tool_versions,
            "at": context.at,
        }
    else:
        values = context
    return MemoryConditionContext(
        commit_ref=commit_ref
        if commit_ref is not None
        else cast(str | None, values.get("commit_ref")),
        tree_digest=tree_digest
        if tree_digest is not None
        else cast(str | None, values.get("tree_digest")),
        file_fingerprints=(
            file_fingerprints
            if file_fingerprints is not None
            else cast(Mapping[str, str], values.get("file_fingerprints") or {})
        ),
        environment_digest=(
            environment_digest
            if environment_digest is not None
            else cast(str | None, values.get("environment_digest"))
        ),
        tool_versions=(
            tool_versions
            if tool_versions is not None
            else cast(Mapping[str, str], values.get("tool_versions") or {})
        ),
        at=now if now is not None else cast(datetime | None, values.get("at")),
    )


def conditions_match(
    conditions: MemoryConditions,
    context: MemoryConditionContext | Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
    commit_ref: str | None = None,
    tree_digest: str | None = None,
    file_fingerprints: Mapping[str, str] | None = None,
    environment_digest: str | None = None,
    tool_versions: Mapping[str, str] | None = None,
) -> bool:
    """Return whether conditions apply to the supplied current facts.

    Required contextual fields are fail-closed: an entry requiring a commit,
    tree, file fingerprint, environment or tool version does not match when
    the caller has not supplied that exact fact.  ``valid_until`` is treated
    as an exclusive bound.
    """

    current = _as_context(
        context,
        now=now,
        commit_ref=commit_ref,
        tree_digest=tree_digest,
        file_fingerprints=file_fingerprints,
        environment_digest=environment_digest,
        tool_versions=tool_versions,
    )
    at = current.at or datetime.now(timezone.utc)
    if at.tzinfo is None or at.utcoffset() is None:
        return False
    if at < conditions.valid_from:
        return False
    if conditions.valid_until is not None and at >= conditions.valid_until:
        return False
    if conditions.commit_ref is not None and current.commit_ref != conditions.commit_ref:
        return False
    if conditions.tree_digest is not None and current.tree_digest != conditions.tree_digest:
        return False
    if any(
        current.file_fingerprints.get(path) != digest
        for path, digest in conditions.file_fingerprints.items()
    ) or len(current.file_fingerprints) < len(conditions.file_fingerprints):
        return False
    if (
        conditions.environment_digest is not None
        and current.environment_digest != conditions.environment_digest
    ):
        return False
    return not any(
        current.tool_versions.get(tool) != version
        for tool, version in conditions.tool_versions.items()
    )


def memory_conditions_match(
    version: MemoryVersion,
    context: MemoryConditionContext | Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> bool:
    """Convenience wrapper accepting a complete ``MemoryVersion``."""

    return conditions_match(version.conditions, context, **kwargs)


matches_conditions = conditions_match
condition_matches = conditions_match


def _scope_visible(version: MemoryVersion, request: RecallRequest) -> bool:
    return (
        version.ref.dataset_id == request.context.dataset_id
        and version.scope == request.context.scope
    )


def memory_sensitivity_visible(version: MemoryVersion, maximum: str | None) -> bool:
    if maximum is None:
        return True
    order = {"public": 0, "internal": 1, "sensitive": 2}
    return order.get(version.sensitivity, 99) <= order.get(maximum, -1)


def _cutoff_visible(
    version: MemoryVersion,
    knowledge_cutoff: str,
    cutoff_resolver: CutoffResolver | None,
) -> bool:
    if knowledge_cutoff == "0" or cutoff_resolver is None:
        return knowledge_cutoff == "0" or cutoff_resolver is not None
    raw = cutoff_resolver(version)
    if raw is None:
        return False
    try:
        return int(str(raw)) <= int(knowledge_cutoff)
    except (TypeError, ValueError):
        return False


def _source_key(version: MemoryVersion) -> tuple[str, str]:
    if not version.sources:
        return ("record", version.ref.record_id)
    source = min((item.source_type, item.source_id) for item in version.sources)
    return source


def _content_terms(version: MemoryVersion) -> str:
    return version.content.casefold()


_EVIDENCE_WEIGHT: dict[Evidence, float] = {
    "user_asserted": 1.0,
    "tested": 0.98,
    "observed": 0.9,
    "inferred": 0.68,
    "legacy_unverified": 0.2,
}


@dataclass(frozen=True, slots=True)
class _ScoredVersion:
    version: MemoryVersion
    score: float
    explicit: bool
    source_key: tuple[str, str]
    matched_terms: int
    matched_groups: int


_CONCEPT_CHUNK = re.compile(r"[A-Za-z0-9_./:-]+|[\u3400-\u9fff]+")
_NAMED_SUBJECT = re.compile(r"[A-Z][a-z]{2,}")
_QUERY_STOP_WORDS = frozenset(
    {"find", "show", "search", "how", "what", "the", "请", "查找", "搜索", "如何", "怎么", "关于"}
)


def _score(
    version: MemoryVersion,
    plan: QueryPlan,
    *,
    explicit: bool,
) -> tuple[float, int, int]:
    content = _content_terms(version)
    if not explicit:
        # Compact queries with a named subject describe related concepts. A broad
        # fallback for only one concept (e.g. a project name or a tool) must
        # not turn an unrelated result into a relevant one. Long natural task
        # text still uses the bounded OR plan instead of full-text AND.
        chunks = [
            match.group(0)
            for match in _CONCEPT_CHUNK.finditer(plan.normalized_query)
            if match.group(0).casefold() not in _QUERY_STOP_WORDS
        ]
        if (
            2 <= len(chunks) <= 3
            and len(plan.normalized_query) <= 80
            and any(_NAMED_SUBJECT.fullmatch(chunk) for chunk in chunks)
        ):

            def concept_matches(chunk: str) -> bool:
                if _CJK_RUN.fullmatch(chunk):
                    return any(term.casefold() in content for term in _cjk_terms(chunk, limit=12))
                return chunk.casefold() in content

            if not all(concept_matches(chunk) for chunk in chunks):
                return 0.0, 0, 0
    matched_terms = 0
    matched_groups = 0
    weighted_matches = 0.0
    weighted_total = 0.0
    labels = plan.group_labels or tuple("term" for _ in plan.groups)
    for index, group in enumerate(plan.groups):
        label = labels[index] if index < len(labels) else "term"
        group_weight = 1.0 if label == "identifier" else 0.85 if label == "cjk" else 0.65
        group_matched = False
        best_match_weight = 0.0
        for term in group:
            term_weight = group_weight
            if label == "cjk" and len(term) <= 2:
                term_weight *= 0.45
            if term.casefold() in content:
                matched_terms += 1
                group_matched = True
                best_match_weight = max(best_match_weight, term_weight)
        weighted_matches += best_match_weight
        weighted_total += group_weight
        if group_matched:
            matched_groups += 1
    if explicit:
        return 1.0, matched_terms, matched_groups
    if not weighted_total or not matched_terms:
        return 0.0, matched_terms, matched_groups
    coverage = weighted_matches / weighted_total
    group_coverage = matched_groups / max(1, len(plan.groups))
    evidence = _EVIDENCE_WEIGHT[version.evidence]
    score = 0.68 * coverage + 0.17 * group_coverage + 0.15 * evidence
    return min(1.0, max(0.0, score)), matched_terms, matched_groups


def _is_better(candidate: _ScoredVersion, current: _ScoredVersion) -> bool:
    candidate_key = (
        candidate.explicit,
        candidate.score,
        candidate.matched_groups,
        candidate.matched_terms,
        candidate.version.ref.version,
        candidate.version.ref.content_digest,
    )
    current_key = (
        current.explicit,
        current.score,
        current.matched_groups,
        current.matched_terms,
        current.version.ref.version,
        current.version.ref.content_digest,
    )
    return candidate_key > current_key


def _deduplicate(values: Iterable[_ScoredVersion]) -> list[_ScoredVersion]:
    by_record: dict[tuple[str, str], _ScoredVersion] = {}
    for value in values:
        key = (value.version.ref.dataset_id, value.version.ref.record_id)
        current = by_record.get(key)
        if current is None or _is_better(value, current):
            by_record[key] = value
    return list(by_record.values())


def _ordered(values: Iterable[_ScoredVersion]) -> list[_ScoredVersion]:
    return sorted(
        values,
        key=lambda value: (
            not value.explicit,
            -value.score,
            -value.matched_groups,
            -value.matched_terms,
            -value.version.ref.version,
            value.version.ref.record_id,
            value.version.ref.content_digest,
        ),
    )


def _diverse(
    values: Sequence[_ScoredVersion],
    *,
    limit: int,
    max_per_source: int,
) -> tuple[_ScoredVersion, ...]:
    selected: list[_ScoredVersion] = []
    source_counts: dict[tuple[str, str], int] = {}
    deferred: list[_ScoredVersion] = []
    for value in values:
        source = value.source_key
        if value.explicit or source_counts.get(source, 0) < max_per_source:
            selected.append(value)
            source_counts[source] = source_counts.get(source, 0) + 1
            if len(selected) >= limit:
                return tuple(selected)
        else:
            deferred.append(value)
    # A single source should still be useful when no diverse alternatives
    # exist.  This fallback only fills slots left by the strict first pass.
    for value in deferred:
        selected.append(value)
        if len(selected) >= limit:
            break
    return tuple(selected)


def _resolve_search(search: object) -> MemorySearch:
    if callable(search):
        return cast(MemorySearch, search)
    candidate: object = getattr(search, "search", None)
    if callable(candidate):
        return cast(MemorySearch, candidate)
    candidate = getattr(search, "query", None)
    if callable(candidate):
        return cast(MemorySearch, candidate)
    raise TypeError("search must be a callable or expose search/query")


def retrieve_memories(
    request: RecallRequest,
    search: object,
    *,
    policy: RetrievalPolicy | None = None,
    query_plan: QueryPlan | None = None,
    condition_context: MemoryConditionContext | Mapping[str, Any] | None = None,
    resolve: VersionResolver | None = None,
    version_lookup: VersionResolver | None = None,
    cutoff_resolver: CutoffResolver | None = None,
    visibility: VisibilityCheck | None = None,
    allowed_role_ids: Iterable[str] = (),
    allowed_agent_ids: Iterable[str] = (),
    maximum_sensitivity: str | None = None,
) -> tuple[CandidateReference, ...]:
    """Retrieve and rank memory references for one typed recall request.

    ``search`` is called once per bounded plain term in the query plan using
    the current Ledger signature.  ``resolve`` is optional and is used only
    for explicit references that do not appear in lexical search results.  A
    non-zero ``knowledge_cutoff`` requires ``cutoff_resolver`` so an old
    index cannot silently return an unbounded version.
    """

    active_policy = policy or RetrievalPolicy()
    # Core can share its bounded FTS plan with ranking within this request.
    # This reuses query parsing only; every candidate is still checked below.
    plan = query_plan or build_query_plan(
        request.query,
        max_query_groups=active_policy.max_query_groups,
        max_terms_per_group=active_policy.max_terms_per_group,
    )
    if query_plan is not None and (
        query_plan.normalized_query != _normalise_query(request.query)
        or len(query_plan.groups) > active_policy.max_query_groups
        or any(len(group) > active_policy.max_terms_per_group for group in query_plan.groups)
    ):
        raise ValueError("query plan must match this request and retrieval policy")
    backend = _resolve_search(search)
    role_ids = frozenset(allowed_role_ids)
    agent_ids = frozenset(allowed_agent_ids)
    resolver = resolve or version_lookup
    raw: list[MemoryVersion] = []

    for term in plan.queries:
        values = backend(
            request.context.dataset_id,
            term,
            scope=request.context.scope,
            limit=min(active_policy.candidate_limit, 10_000),
        )
        raw.extend(value for value in values if isinstance(value, MemoryVersion))

    if resolver is not None:
        for reference in request.explicit_refs:
            resolved = resolver(reference)
            if resolved is not None:
                raw.append(resolved)

    scored: list[_ScoredVersion] = []
    explicit_refs = set(request.explicit_refs)
    for version in {value.ref: value for value in raw}.values():
        if not _scope_visible(version, request):
            continue
        if version.evidence == "legacy_unverified" and not active_policy.allow_legacy:
            continue
        if version.role_ids and not role_ids.intersection(version.role_ids):
            continue
        if version.agent_ids and not agent_ids.intersection(version.agent_ids):
            continue
        if not memory_sensitivity_visible(version, maximum_sensitivity):
            continue
        if not _cutoff_visible(version, request.knowledge_cutoff, cutoff_resolver):
            continue
        if not memory_conditions_match(version, condition_context):
            continue
        if visibility is not None and not visibility(version):
            continue
        is_explicit = version.ref in explicit_refs
        score, matched_terms, matched_groups = _score(version, plan, explicit=is_explicit)
        if not is_explicit and (score <= 0 or score < active_policy.min_score):
            continue
        scored.append(
            _ScoredVersion(
                version=version,
                score=score,
                explicit=is_explicit,
                source_key=_source_key(version),
                matched_terms=matched_terms,
                matched_groups=matched_groups,
            )
        )

    chosen = _diverse(
        _ordered(_deduplicate(scored)),
        limit=request.max_candidates,
        max_per_source=active_policy.diversity_limit,
    )
    return tuple(
        CandidateReference(ref=value.version.ref, score=round(value.score, 6)) for value in chosen
    )


# Concise aliases for the two names most likely to be used by Core adapters.
retrieve_candidates = retrieve_memories
retrieve = retrieve_memories
plan_queries = build_query_plan


__all__ = [
    "ConditionContext",
    "CutoffResolver",
    "MemoryConditionContext",
    "MemorySearch",
    "QueryPlan",
    "RetrievalConditions",
    "RetrievalPolicy",
    "RETRIEVAL_STRATEGY_VERSION",
    "build_query_plan",
    "compile_fts_query",
    "condition_matches",
    "conditions_match",
    "memory_conditions_match",
    "matches_conditions",
    "plan_queries",
    "retrieve",
    "retrieve_candidates",
    "retrieve_memories",
]
