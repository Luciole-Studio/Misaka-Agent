from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

import hermes_lcm.command as command_mod
from hermes_lcm.command import handle_lcm_command
from hermes_lcm.config import LCMConfig
from hermes_lcm.vector_store import VectorStore


class FakeProvider:
    provider_id = "ollama"
    model_id = "model-a"

    def __init__(self, *, dim: int = 2):
        self.dim = dim
        self.calls: list[list[str]] = []
        self.last_skipped_documents: list[int] = []

    def embed_documents(self, texts):
        current = list(texts)
        self.calls.append(current)
        self.last_skipped_documents = []
        return [[float(index + 1), 1.0] for index, _text in enumerate(current)]


@pytest.fixture(autouse=True)
def deterministic_token_count(monkeypatch):
    # len-based token count keeps chunk-size math simple and deterministic.
    monkeypatch.setattr(command_mod, "count_tokens", lambda text: len(str(text)))
    import hermes_lcm.chunking as chunking
    monkeypatch.setattr(chunking, "count_tokens", lambda text: len(str(text)))


def _engine(tmp_path, *, enabled: bool = True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "backfill.db"
    config = LCMConfig(
        database_path=str(db_path),
        embeddings_enabled=enabled,
        embedding_provider="ollama",
        embedding_model="model-a",
    )
    return SimpleNamespace(_config=config, _store=SimpleNamespace(db_path=db_path))


def _seed_messages(engine, rows, *, register: bool = True):
    conn = sqlite3.connect(engine._store.db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            store_id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            source TEXT DEFAULT '',
            role TEXT NOT NULL,
            content TEXT,
            timestamp REAL NOT NULL
        )
        """
    )
    conn.executemany(
        "INSERT INTO messages(store_id, session_id, source, role, content, timestamp) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    if register:
        store = VectorStore(engine._store.db_path, config=engine._config)
        try:
            store.register_profile("model-a", "ollama", 2, task="chunk")
        finally:
            store.close()


def _chunk_meta_ids(engine) -> list[str]:
    conn = sqlite3.connect(engine._store.db_path)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lcm_chunk_meta'"
        ).fetchone()
        if exists is None:
            return []
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT chunk_id FROM lcm_chunk_meta ORDER BY chunk_id"
            ).fetchall()
        ]
    finally:
        conn.close()


def _user_msgs(n, *, start=1):
    return [
        (i, "sess-a", "history", "user", "u" * 60, float(i))
        for i in range(start, start + n)
    ]


class _CrashOnCommitConnection:
    """Proxies a sqlite3 connection but raises on ``commit`` -- emulating a batch
    publish whose transaction never lands (process killed before commit)."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def commit(self):
        raise RuntimeError("crash before chunk batch commit")


def test_chunk_crash_on_commit_labels_local_error_and_counts_failed(monkeypatch, tmp_path):
    """FIX-4 (chunk path): a commit crash during the chunk batch publish is a
    LOCAL storage failure -- every dispatched chunk row is counted as failed and
    labeled local_error (not provider_error), instead of being silently omitted
    from the run's failed count while mislabeled as a provider error."""
    engine = _engine(tmp_path)  # ollama (local) -> exempt from raw-text gate
    _seed_messages(engine, _user_msgs(2))
    provider = FakeProvider()
    monkeypatch.setattr(command_mod, "resolve_provider", lambda _c, **_k: provider)

    class CrashStore(VectorStore):
        def publish_chunk_embedding_batch_under_lease(self, rows, **kwargs):
            real = self._conn
            self._conn = _CrashOnCommitConnection(real)
            try:
                return super().publish_chunk_embedding_batch_under_lease(rows, **kwargs)
            finally:
                self._conn = real

    monkeypatch.setattr(command_mod, "VectorStore", CrashStore)
    out = handle_lcm_command("embed backfill --corpus chunks --apply", engine)

    # Nothing half-published; the crash is labeled a local storage failure and
    # every dispatched chunk row is counted as failed (not under-counted).
    assert _chunk_meta_ids(engine) == []
    assert "embedded: 0" in out
    assert "failed: 0" not in out
    assert "reason=local_error:" in out
    assert "reason=provider_error:" not in out


class TestChunkRetryUncertainSpans:
    def test_rebuild_chunk_document_returns_real_span(self, tmp_path):
        from hermes_lcm.chunking import chunk_message

        engine = _engine(tmp_path)
        content = "some substantial user output token " * 40
        _seed_messages(engine, [(1, "sess-a", "history", "user", content, 1.0)])
        expected = chunk_message(1, "user", content, policy="conversational")[0]

        conn = sqlite3.connect(engine._store.db_path)
        try:
            rebuilt = command_mod._rebuild_chunk_document(
                conn, expected.chunk_id, "conversational"
            )
        finally:
            conn.close()

        assert rebuilt is not None
        text, tokens, char_start, char_end = rebuilt
        # The real char span is carried, not the old (0, 0) placeholder.
        assert (char_start, char_end) == (expected.char_start, expected.char_end)
        assert char_end > char_start

    def test_authorized_uncertain_rows_persist_real_span_not_zero(self, tmp_path):
        from hermes_lcm.chunking import chunk_message

        engine = _engine(tmp_path)
        content = "another long verbatim user payload body " * 30
        _seed_messages(engine, [(1, "sess-a", "history", "user", content, 1.0)])
        expected = chunk_message(1, "user", content, policy="conversational")[0]

        # _chunk_authorized_uncertain_rows only string-matches identity_hash (no
        # profile join), so any stable value the inflight row shares works here.
        identity = "test-chunk-identity"

        conn = sqlite3.connect(engine._store.db_path)
        try:
            from hermes_lcm import db_bootstrap

            db_bootstrap.ensure_embedding_tables(conn)
            db_bootstrap.ensure_chunk_tables(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lcm_embedding_backfill_inflight (
                    embedded_id TEXT, identity_hash TEXT, state TEXT,
                    updated_at REAL,
                    PRIMARY KEY(embedded_id, identity_hash)
                )
                """
            )
            conn.execute(
                "INSERT INTO lcm_embedding_backfill_inflight"
                "(embedded_id, identity_hash, state, updated_at) VALUES(?, ?, 'uncertain', 1.0)",
                (expected.chunk_id, identity),
            )
            conn.commit()
            count, documents, meta = command_mod._chunk_authorized_uncertain_rows(
                conn, identity, "conversational", 10
            )
        finally:
            conn.close()

        assert count == 1
        _sid, _idx, char_start, char_end = meta[expected.chunk_id]
        assert (char_start, char_end) == (expected.char_start, expected.char_end)
        assert char_end > char_start

    def test_authorized_uncertain_rows_group_per_message_despite_interleave(
        self, tmp_path
    ):
        """FIX 3: uncertain rows are SELECTed by (updated_at, embedded_id), which
        interleaves store_ids. Since ``group_by_store_id`` only merges adjacent
        equal store_ids, an interleaved order would collapse every retry chunk
        into a singleton group and defeat C2 contextualization. The retry path
        must stably re-sort documents by (store_id, chunk_index) so each message's
        chunks stay contiguous and group into one contextualization document."""
        from hermes_lcm.chunking import chunk_message, group_by_store_id
        from hermes_lcm import db_bootstrap

        engine = _engine(tmp_path)
        # Many sentence boundaries so each message splits into several ~600-token
        # chunks (the chunker splits at sentence boundaries, not raw length).
        content = " ".join(
            f"This is verbatim payload sentence {i} about kanban dashboards."
            for i in range(400)
        )
        _seed_messages(
            engine,
            [
                (1, "sess-a", "history", "user", content, 1.0),
                (2, "sess-a", "history", "user", content, 2.0),
            ],
        )
        chunks_1 = chunk_message(1, "user", content, policy="conversational")
        chunks_2 = chunk_message(2, "user", content, policy="conversational")
        assert len(chunks_1) >= 2 and len(chunks_2) >= 2  # multi-chunk messages

        identity = "test-chunk-identity"
        conn = sqlite3.connect(engine._store.db_path)
        try:
            db_bootstrap.ensure_embedding_tables(conn)
            db_bootstrap.ensure_chunk_tables(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lcm_embedding_backfill_inflight (
                    embedded_id TEXT, identity_hash TEXT, state TEXT,
                    updated_at REAL,
                    PRIMARY KEY(embedded_id, identity_hash)
                )
                """
            )
            # Interleave updated_at so the SQL order alternates message 1 / 2.
            interleaved = []
            for i in range(max(len(chunks_1), len(chunks_2))):
                if i < len(chunks_1):
                    interleaved.append(chunks_1[i].chunk_id)
                if i < len(chunks_2):
                    interleaved.append(chunks_2[i].chunk_id)
            for ordinal, chunk_id in enumerate(interleaved):
                conn.execute(
                    "INSERT INTO lcm_embedding_backfill_inflight"
                    "(embedded_id, identity_hash, state, updated_at) "
                    "VALUES(?, ?, 'uncertain', ?)",
                    (chunk_id, identity, float(ordinal)),
                )
            conn.commit()
            count, documents, meta = command_mod._chunk_authorized_uncertain_rows(
                conn, identity, "conversational", 100
            )
        finally:
            conn.close()

        assert count == len(chunks_1) + len(chunks_2)
        store_ids = [meta[chunk_id][0] for chunk_id, _text, _tokens in documents]
        groups = group_by_store_id(store_ids)
        # Exactly one group per message -- NOT one singleton per chunk.
        assert len(groups) == 2
        assert sorted(len(g) for g in groups) == sorted(
            [len(chunks_1), len(chunks_2)]
        )
        # Within a message, chunk_index order is preserved.
        for group in groups:
            indexes = [meta[documents[pos][0]][1] for pos in group]
            assert indexes == sorted(indexes)


class TestChunkRawTextConsentGate:
    def _voyage_engine(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        db_path = tmp_path / "backfill.db"
        config = LCMConfig(
            database_path=str(db_path),
            embeddings_enabled=True,
            embedding_provider="voyage",
            embedding_model="voyage-3",
        )
        engine = SimpleNamespace(_config=config, _store=SimpleNamespace(db_path=db_path))
        _seed_messages(engine, _user_msgs(2), register=False)
        store = VectorStore(db_path, config=config)
        try:
            store.register_profile("voyage-3", "voyage", 2, task="chunk")
        finally:
            store.close()
        return engine

    def test_cloud_apply_refused_without_confirm_flag(self, monkeypatch, tmp_path):
        engine = self._voyage_engine(tmp_path)
        provider = FakeProvider()
        monkeypatch.setattr(command_mod, "resolve_provider", lambda _c, **_k: provider)

        out = handle_lcm_command("embed backfill --corpus chunks --apply", engine)

        assert "status: refused" in out
        assert "RAW, VERBATIM" in out
        assert "--confirm-raw-text" in out
        # Nothing was sent to the cloud provider and nothing was written.
        assert provider.calls == []
        assert _chunk_meta_ids(engine) == []

    def test_cloud_apply_proceeds_with_confirm_flag(self, monkeypatch, tmp_path):
        engine = self._voyage_engine(tmp_path)
        provider = FakeProvider()
        # Match the registered voyage chunk profile so the apply gets past the
        # provider/profile consistency check and actually embeds.
        provider.provider_id = "voyage"
        provider.model_id = "voyage-3"
        monkeypatch.setattr(command_mod, "resolve_provider", lambda _c, **_k: provider)

        out = handle_lcm_command(
            "embed backfill --corpus chunks --apply --confirm-raw-text", engine
        )

        assert "status: refused" not in out
        assert _chunk_meta_ids(engine)  # chunks were embedded

    def test_local_provider_exempt_from_gate(self, monkeypatch, tmp_path):
        engine = _engine(tmp_path)  # ollama (local)
        _seed_messages(engine, _user_msgs(2))
        provider = FakeProvider()
        monkeypatch.setattr(command_mod, "resolve_provider", lambda _c, **_k: provider)

        out = handle_lcm_command("embed backfill --corpus chunks --apply", engine)

        assert "status: refused" not in out
        assert _chunk_meta_ids(engine)

    def test_confirm_flag_rejected_for_summary_corpus(self, tmp_path):
        engine = _engine(tmp_path)
        out = handle_lcm_command("embed backfill --confirm-raw-text", engine)
        assert "only applies to the chunk corpus" in out


class TestBothCorpusNextHint:
    def test_both_dry_run_emits_single_coherent_next_hint(self, monkeypatch, tmp_path):
        engine = _engine(tmp_path)
        _seed_messages(engine, _user_msgs(2))
        provider = FakeProvider()
        monkeypatch.setattr(command_mod, "resolve_provider", lambda _c, **_k: provider)

        out = handle_lcm_command("embed backfill --corpus both", engine)

        # Exactly one next-hint, and it names the actual `--corpus both` command.
        assert out.count("next: run") == 1
        assert "next: run `/lcm embed backfill --corpus both --apply`" in out
        # The two contradictory per-corpus hints are gone.
        assert "run `/lcm embed backfill --apply`" not in out
        assert "run `/lcm embed backfill --corpus chunks --apply`" not in out
        assert provider.calls == []


class TestChunkDryRun:
    def test_reports_pending_without_writes(self, monkeypatch, tmp_path):
        engine = _engine(tmp_path)
        _seed_messages(engine, _user_msgs(3))
        provider = FakeProvider()
        monkeypatch.setattr(command_mod, "resolve_provider", lambda _c, **_k: provider)
        before = engine._store.db_path.read_bytes()

        result = handle_lcm_command("embed backfill --corpus chunks", engine)

        assert "corpus: chunks" in result
        assert "policy: conversational" in result
        assert "status: dry-run" in result
        assert "pending: 3" in result
        assert provider.calls == []
        assert _chunk_meta_ids(engine) == []
        assert engine._store.db_path.read_bytes() == before

    def test_estimates_without_registered_profile(self, tmp_path):
        engine = _engine(tmp_path)
        _seed_messages(engine, _user_msgs(2), register=False)
        result = handle_lcm_command("embed backfill --corpus chunks", engine)
        assert "status: dry-run" in result
        assert "pending: 2" in result

    def test_dry_run_display_honors_explicit_context_model(self, tmp_path):
        # H3: an explicit voyage context model is the chunk-model intent and the
        # dry-run display must equal it (single resolution path) rather than
        # forcing the voyage-context-4 mapping — this is what apply would use.
        engine = _engine(tmp_path)
        engine._config.embedding_provider = "voyage"
        engine._config.embedding_model = "voyage-context-3"
        _seed_messages(engine, _user_msgs(1), register=False)
        result = handle_lcm_command("embed backfill --corpus chunks", engine)
        assert "model: voyage-context-3" in result

    def test_dry_run_display_maps_plain_voyage_to_context_default(self, tmp_path):
        # A plain (non-context) voyage model has no explicit chunk-model intent,
        # so the voyage-context-4 mapping applies.
        engine = _engine(tmp_path)
        engine._config.embedding_provider = "voyage"
        engine._config.embedding_model = "voyage-3"
        _seed_messages(engine, _user_msgs(1), register=False)
        result = handle_lcm_command("embed backfill --corpus chunks", engine)
        assert "model: voyage-context-4" in result

    def test_policy_heads_includes_tool_heads(self, tmp_path):
        engine = _engine(tmp_path)
        rows = _user_msgs(1) + [(2, "sess-a", "history", "tool", "t" * 60, 2.0)]
        _seed_messages(engine, rows)
        conv = handle_lcm_command("embed backfill --corpus chunks --policy conversational", engine)
        heads = handle_lcm_command("embed backfill --corpus chunks --policy heads", engine)
        assert "pending: 1" in conv  # tool skipped under conversational
        assert "pending: 2" in heads  # tool head added under heads

    def test_disabled_is_refused(self, tmp_path):
        engine = _engine(tmp_path, enabled=False)
        _seed_messages(engine, _user_msgs(1), register=False)
        result = handle_lcm_command("embed backfill --corpus chunks", engine)
        assert "status: refused" in result

    def test_context_estimate_uses_grouped_caps_not_flat_27k(self):
        """FIX 4: the contextualized estimate groups chunks per message, skips a
        chunk only above the 32K per-chunk context cap (NOT the flat 27K per-doc
        cap), and counts requests via the context planner -- so the cost/consent
        preview matches the grouped apply path."""
        from hermes_lcm.embedding_provider import _VOYAGE_CONTEXT_MAX_CHUNK_TOKENS

        # message 1: two small chunks; message 2: one 30K chunk (27K<t<=32K -> now
        # BILLABLE, was wrongly excluded by the old 27K cap) + one 40K chunk
        # (>32K -> genuinely skipped by apply).
        documents = [
            ("1:0", "a", 10),
            ("1:1", "b", 20),
            ("2:0", "c", 30_000),
            ("2:1", "d", _VOYAGE_CONTEXT_MAX_CHUNK_TOKENS + 8_000),
        ]
        est_tokens, est_cost_tokens, est_requests = command_mod._chunk_context_estimates(
            documents
        )
        assert est_tokens == 10 + 20 + 30_000 + (_VOYAGE_CONTEXT_MAX_CHUNK_TOKENS + 8_000)
        # Billable = every chunk at/under the 32K per-chunk cap (the 30K chunk is
        # charged; the >32K chunk is skipped). The old flat estimate wrongly
        # dropped the 30K chunk at 27K.
        assert est_cost_tokens == 10 + 20 + 30_000
        # Two messages -> two grouped documents packed into a single context
        # request (well under the request budgets).
        assert est_requests == 1


class TestChunkApply:
    def test_records_meta_and_is_idempotent(self, monkeypatch, tmp_path):
        engine = _engine(tmp_path)
        _seed_messages(engine, _user_msgs(3))
        provider = FakeProvider()
        monkeypatch.setattr(command_mod, "resolve_provider", lambda _c, **_k: provider)

        first = handle_lcm_command("embed backfill --corpus chunks --apply", engine)
        assert "embedded: 3" in first
        assert _chunk_meta_ids(engine) == ["1:0", "2:0", "3:0"]

        second = handle_lcm_command("embed backfill --corpus chunks --apply", engine)
        assert "pending: 0" in second
        assert "embedded: 0" in second
        assert _chunk_meta_ids(engine) == ["1:0", "2:0", "3:0"]

    def test_prescreen_revision_identity_applies(self, monkeypatch, tmp_path):
        engine = _engine(tmp_path)
        engine._config.embedding_binary_prescreen = True
        _seed_messages(engine, _user_msgs(1))
        provider = FakeProvider()
        monkeypatch.setattr(command_mod, "resolve_provider", lambda _c, **_k: provider)

        result = handle_lcm_command("embed backfill --corpus chunks --apply", engine)

        assert "status: complete" in result
        assert _chunk_meta_ids(engine) == ["1:0"]

    def test_provider_skips_leave_run_partial(self, monkeypatch, tmp_path):
        engine = _engine(tmp_path)
        _seed_messages(engine, _user_msgs(3))
        provider = FakeProvider()

        def skip_middle(texts):
            provider.calls.append(list(texts))
            provider.last_skipped_documents = [1]
            return [[1.0, 1.0], [2.0, 1.0]]

        provider.embed_documents = skip_middle
        monkeypatch.setattr(command_mod, "resolve_provider", lambda _c, **_k: provider)

        result = handle_lcm_command("embed backfill --corpus chunks --apply", engine)

        assert "status: partial" in result
        assert "skipped_overcap: 1" in result
        assert "remaining: 1" in result

    def test_lease_and_uncertain_retry(self, monkeypatch, tmp_path):
        engine = _engine(tmp_path)
        _seed_messages(engine, _user_msgs(2))
        provider = FakeProvider()
        monkeypatch.setattr(command_mod, "resolve_provider", lambda _c, **_k: provider)

        # Force a durable uncertain marker for chunk "1:0".
        store = VectorStore(engine._store.db_path, config=engine._config)
        try:
            store.ensure_chunk_schema()
            conn = store.connection
            command_mod._ensure_inflight_table(conn)
            identity = str(conn.execute(
                "SELECT identity_hash FROM lcm_embedding_profile WHERE task='chunk' AND active=1"
            ).fetchone()[0])
            conn.execute(
                "INSERT INTO lcm_embedding_backfill_inflight("
                "embedded_id, identity_hash, lease_id, generation, claimed_at, "
                "state, request_id, updated_at, last_error) "
                "VALUES('1:0', ?, 'prior', 1, 1, 'uncertain', 'prior-req', 1, 'unknown')",
                (identity,),
            )
            conn.commit()
        finally:
            store.close()

        # Ordinary apply must NOT auto-retry the uncertain chunk.
        ordinary = handle_lcm_command("embed backfill --corpus chunks --apply", engine)
        assert "1:0" not in _chunk_meta_ids(engine)
        assert "2:0" in _chunk_meta_ids(engine)
        assert "embedded: 1" in ordinary

        # Explicit authorization re-embeds only the uncertain chunk.
        retry = handle_lcm_command(
            "embed backfill --corpus chunks --apply --retry-uncertain", engine
        )
        assert "1:0" in _chunk_meta_ids(engine)
        assert "embedded: 1" in retry


class _ContextFakeTransport:
    """Records contextualized requests and returns a nested per-doc response."""

    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        inputs = kwargs["payload"]["inputs"]
        data = []
        for outer, group in enumerate(inputs):
            inner = [
                {"index": i, "embedding": [float(outer + 1), float(i + 1), 1.0]}
                for i in range(len(group))
            ]
            data.append({"index": outer, "data": inner})
        from hermes_lcm.embedding_provider import HttpResponse

        return HttpResponse(
            status=200, headers={},
            body=json.dumps({"data": data, "usage": {"total_tokens": 10}}).encode(),
        )


def _two_chunk_msg(store_id, session="sess-a"):
    # With len-based token counting (~600-token window target), ~900 chars of
    # sentence-delimited text splits into two chunks.
    sentence = "this is a sufficiently long sentence with several words. "
    content = sentence * 16
    return (store_id, session, "history", "user", content, float(store_id))


class TestChunkContextualizedGrouping:
    def _voyage_context_engine(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        db_path = tmp_path / "backfill.db"
        config = LCMConfig(
            database_path=str(db_path),
            embeddings_enabled=True,
            embedding_provider="voyage",
            embedding_model="voyage-context-3",
        )
        engine = SimpleNamespace(
            _config=config, _store=SimpleNamespace(db_path=db_path)
        )
        # Two multi-chunk messages so grouping is observable on the wire.
        _seed_messages(
            engine, [_two_chunk_msg(1), _two_chunk_msg(2)], register=False
        )
        store = VectorStore(db_path, config=config)
        try:
            store.register_profile("voyage-context-3", "voyage", 3, task="chunk")
        finally:
            store.close()
        return engine

    def test_message_chunks_grouped_into_one_inputs_list(self, monkeypatch, tmp_path):
        import hermes_lcm.embedding_provider as provider_mod

        monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
        engine = self._voyage_context_engine(tmp_path)
        transport = _ContextFakeTransport()
        provider = provider_mod.VoyageProvider(
            "voyage-context-3", transport=transport
        )
        monkeypatch.setattr(command_mod, "resolve_provider", lambda _c, **_k: provider)

        out = handle_lcm_command(
            "embed backfill --corpus chunks --apply --confirm-raw-text", engine
        )

        assert "status: refused" not in out
        # The context endpoint saw one inner list PER MESSAGE (cross-chunk
        # contextualization), not one single-chunk list per chunk.
        assert transport.calls
        inputs = transport.calls[0]["payload"]["inputs"]
        assert all(len(inner) >= 2 for inner in inputs), inputs
        # Every chunk was still published as its own independent row.
        meta_ids = _chunk_meta_ids(engine)
        assert len(meta_ids) == sum(len(inner) for inner in inputs)
        # Each row keyed store_id:chunk_index -> two messages, chunk 0 and 1 each.
        assert set(meta_ids) == {"1:0", "1:1", "2:0", "2:1"}

    def test_dry_run_estimated_batches_match_apply_requests(self, monkeypatch, tmp_path):
        """FIX 4: the dry-run 'estimated_batches' equals the number of context
        requests the grouped apply path actually dispatches, so the preview
        matches apply's request shape."""
        import hermes_lcm.embedding_provider as provider_mod

        monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
        engine = self._voyage_context_engine(tmp_path)

        # Dry-run first (no writes): capture the estimated request count.
        dry = handle_lcm_command("embed backfill --corpus chunks", engine)
        estimated_batches = int(
            next(
                line.split(":", 1)[1].strip()
                for line in dry.splitlines()
                if line.startswith("estimated_batches:")
            )
        )
        assert _chunk_meta_ids(engine) == []  # dry-run wrote nothing

        # Apply with a request-recording transport: count real dispatches.
        transport = _ContextFakeTransport()
        provider = provider_mod.VoyageProvider("voyage-context-3", transport=transport)
        monkeypatch.setattr(command_mod, "resolve_provider", lambda _c, **_k: provider)
        out = handle_lcm_command(
            "embed backfill --corpus chunks --apply --confirm-raw-text", engine
        )
        assert "status: refused" not in out
        assert estimated_batches == len(transport.calls)


class TestProviderChunkDocumentBatchesFlatPath:
    def test_non_context_provider_keeps_flat_per_chunk_path(self):
        provider = FakeProvider()  # no supports_contextualized_grouping attr
        batch = [("1:0", "a", 1), ("1:1", "b", 1), ("2:0", "c", 1)]
        chunk_meta = {
            "1:0": (1, 0, 0, 1), "1:1": (1, 1, 0, 1), "2:0": (2, 0, 0, 1),
        }
        dispatched: list[tuple[int, ...]] = []
        batches = list(
            command_mod._provider_chunk_document_batches(
                provider, batch, chunk_meta,
                before_dispatch=lambda idx: dispatched.append(idx),
            )
        )
        # Flat path: all chunks embedded as independent documents in one call.
        assert provider.calls == [["a", "b", "c"]]
        assert len(batches) == 1
        assert batches[0].indexes == (0, 1, 2)
        assert dispatched == [(0, 1, 2)]
