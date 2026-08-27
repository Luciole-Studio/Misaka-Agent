"""Bounded, provider-neutral rebuild orchestration for V4 assertions.

The orchestrator receives exact persisted ``SourceSnapshot`` rows from
``AssertionStore.plan_rebuild``. It deliberately knows nothing about models,
providers, embeddings, prompts, or benchmark inputs. Dry-run never invokes an
extractor; apply requires an explicitly supplied pure/injected extractor and
publishes each source through AssertionStore's late source-hash CAS.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json

from .assertion_store import (
    CURRENT_EXTRACTION_VERSION,
    AssertionCandidate,
    AssertionRelationCandidate,
    AssertionSourceStaleError,
    AssertionStore,
    SourceSnapshot,
)


@dataclass(frozen=True)
class AssertionExtraction:
    """Validated candidate batch emitted for one exact source snapshot."""

    assertions: tuple[AssertionCandidate, ...] = ()
    relations: tuple[AssertionRelationCandidate, ...] = ()


AssertionExtractor = Callable[[SourceSnapshot], AssertionExtraction]


@dataclass(frozen=True)
class AssertionRebuildFailure:
    source_store_id: int
    error: str


@dataclass(frozen=True)
class AssertionRebuildResult:
    mode: str
    extraction_version: str
    pending_count: int
    selected_count: int
    processed_count: int
    already_current_count: int
    stale_or_missing_count: int
    failed_count: int
    assertions_written: int
    relations_written: int
    remaining_count: int
    plan_digest: str
    receipt_digest: str
    failures: tuple[AssertionRebuildFailure, ...]


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounded_error(exc: Exception) -> str:
    rendered = f"{type(exc).__name__}: {exc}".replace("\n", " ").strip()
    return rendered[:300]


def rebuild_assertions(
    store: AssertionStore,
    *,
    apply: bool = False,
    extractor: AssertionExtractor | None = None,
    limit: int = 100,
) -> AssertionRebuildResult:
    """Plan or apply one bounded, resumable assertion rebuild batch.

    A dry-run performs only read queries and never calls ``extractor``. Apply
    publishes one source at a time, so completed receipts are durable cursors
    and a later run resumes only sources that remain pending. Candidate
    validation and raw-row compare-and-swap remain owned by ``AssertionStore``.
    """

    if apply and store.read_only:
        raise ValueError("apply requires a writable AssertionStore")
    if apply and extractor is None:
        raise ValueError("apply requires an explicit assertion extractor")

    plan = store.plan_rebuild(
        extraction_version=CURRENT_EXTRACTION_VERSION,
        limit=limit,
    )
    plan_digest = _digest([
        {
            "source_store_id": source.store_id,
            "source_content_sha256": source.content_sha256,
        }
        for source in plan.selected_sources
    ])
    if not apply:
        return AssertionRebuildResult(
            mode="dry-run",
            extraction_version=CURRENT_EXTRACTION_VERSION,
            pending_count=plan.pending_count,
            selected_count=len(plan.selected_sources),
            processed_count=0,
            already_current_count=0,
            stale_or_missing_count=0,
            failed_count=0,
            assertions_written=0,
            relations_written=0,
            remaining_count=plan.pending_count,
            plan_digest=plan_digest,
            receipt_digest=_digest([]),
            failures=(),
        )

    processed = 0
    already_current = 0
    stale_or_missing = 0
    assertions_written = 0
    relations_written = 0
    failures: list[AssertionRebuildFailure] = []
    receipts: list[dict[str, object]] = []
    assert extractor is not None

    for source in plan.selected_sources:
        try:
            extraction = extractor(source)
            if not isinstance(extraction, AssertionExtraction):
                raise TypeError("assertion extractor must return AssertionExtraction")
            published = store.publish_source(
                source,
                extraction.assertions,
                relations=extraction.relations,
                extraction_version=CURRENT_EXTRACTION_VERSION,
            )
            processed += 1
            already_current += int(published.already_current)
            assertions_written += published.assertions_written
            relations_written += published.relations_written
            receipts.append({
                "source_store_id": published.source_store_id,
                "source_content_sha256": published.source_content_sha256,
                "candidate_digest": published.candidate_digest,
            })
        except (AssertionSourceStaleError, KeyError) as exc:
            stale_or_missing += 1
            failures.append(AssertionRebuildFailure(source.store_id, _bounded_error(exc)))
        except Exception as exc:
            failures.append(AssertionRebuildFailure(source.store_id, _bounded_error(exc)))

    remaining = store.plan_rebuild(
        extraction_version=CURRENT_EXTRACTION_VERSION,
        limit=1,
    ).pending_count
    receipts.sort(key=lambda item: int(item["source_store_id"]))
    return AssertionRebuildResult(
        mode="apply",
        extraction_version=CURRENT_EXTRACTION_VERSION,
        pending_count=plan.pending_count,
        selected_count=len(plan.selected_sources),
        processed_count=processed,
        already_current_count=already_current,
        stale_or_missing_count=stale_or_missing,
        failed_count=len(failures) - stale_or_missing,
        assertions_written=assertions_written,
        relations_written=relations_written,
        remaining_count=remaining,
        plan_digest=plan_digest,
        receipt_digest=_digest(receipts),
        failures=tuple(failures),
    )
