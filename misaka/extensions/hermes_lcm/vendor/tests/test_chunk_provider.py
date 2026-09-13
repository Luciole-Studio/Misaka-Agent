from __future__ import annotations

import json

import pytest

import hermes_lcm.embedding_provider as provider_mod
from hermes_lcm.embedding_provider import (
    HttpResponse,
    VoyageError,
    VoyageProvider,
    _plan_contextualized_requests,
    default_chunk_model,
    embed_contextualized,
)

_CONTEXT_URL = "https://api.voyageai.com/v1/contextualizedembeddings"
_FLAT_URL = "https://api.voyageai.com/v1/embeddings"


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _context_response(group_sizes, *, usage=100, order=None):
    """Build a nested contextualized response.

    Each embedding encodes its own ``(outer_index, inner_index)`` so a caller
    can assert order preservation even when the wire order is shuffled via
    ``order`` (a permutation of the outer-document objects).
    """
    data = []
    for outer_index, count in enumerate(group_sizes):
        inner = [
            {"index": i, "embedding": [float(outer_index), float(i), 0.0], "text": "x"}
            for i in range(count)
        ]
        # Shuffle the inner list too, to prove inner sorting by index.
        inner = list(reversed(inner))
        data.append({"index": outer_index, "data": inner})
    if order is not None:
        data = [data[i] for i in order]
    body = {"data": data, "model": "voyage-context-3", "usage": {"total_tokens": usage}}
    return HttpResponse(status=200, headers={}, body=json.dumps(body).encode())


class FlatProvider:
    """A provider exposing only the flat embed_documents contract."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def embed_documents(self, texts):
        self.calls.append(list(texts))
        # Deterministic 1-d vector per document.
        return [[float(len(text))] for text in texts]


class ContextualProvider(FlatProvider):
    def embed_contextualized(self, chunks_by_doc):
        return [[[1.0] for _ in chunks] for chunks in chunks_by_doc]


class TestDefaultChunkModel:
    def test_voyage_maps_to_context_4(self):
        assert default_chunk_model("voyage", "voyage-3") == "voyage-context-4"
        assert default_chunk_model("VoyageAI", "x") == "voyage-context-4"

    def test_local_providers_reuse_configured_model(self):
        assert default_chunk_model("ollama", "nomic-embed") == "nomic-embed"
        assert default_chunk_model("fastembed", "bge-small") == "bge-small"

    def test_explicit_context_model_wins_over_mapping(self):
        # An explicit voyage context model is the chunk-model intent and wins;
        # a plain voyage model still maps to the voyage-context-4 default.
        assert default_chunk_model("voyage", "voyage-context-3") == "voyage-context-3"
        assert default_chunk_model("voyage", "voyage-context-4") == "voyage-context-4"
        assert default_chunk_model("voyage", "voyage-3") == "voyage-context-4"


class TestEmbedContextualized:
    def test_plain_fallback_regroups_by_document(self):
        provider = FlatProvider()
        result = embed_contextualized(provider, [["a", "bb"], ["ccc"]])
        # One vector list per document, aligned to that doc's chunks.
        assert len(result) == 2
        assert len(result[0]) == 2 and len(result[1]) == 1
        assert result[0][0] == [1.0] and result[0][1] == [2.0]
        assert result[1][0] == [3.0]
        # Flattened into a single embed_documents call.
        assert provider.calls == [["a", "bb", "ccc"]]

    def test_uses_provider_contextualized_when_available(self):
        provider = ContextualProvider()
        result = embed_contextualized(provider, [["a"], ["b", "c"]])
        assert result == [[[1.0]], [[1.0], [1.0]]]
        # The flat path was not used.
        assert provider.calls == []

    def test_empty_input(self):
        assert embed_contextualized(FlatProvider(), []) == []


class TestVoyageContextualizedWireShape:
    def test_embed_contextualized_request_body_shape(self, monkeypatch):
        monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
        transport = FakeTransport(_context_response([2, 1]))
        provider = VoyageProvider("voyage-context-3", transport=transport)

        provider.embed_contextualized([["a", "bb"], ["ccc"]])

        assert len(transport.calls) == 1
        call = transport.calls[0]
        assert call["url"] == _CONTEXT_URL
        payload = call["payload"]
        # inputs is a list of lists (one inner list per document); no flat "input".
        assert payload["inputs"] == [["a", "bb"], ["ccc"]]
        assert payload["input_type"] == "document"
        assert "input" not in payload

    def test_embed_contextualized_parses_nested_and_preserves_order(self, monkeypatch):
        monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
        # Wire order shuffles the outer documents; parsing must restore index order.
        transport = FakeTransport(_context_response([2, 1, 3], order=[2, 0, 1]))
        provider = VoyageProvider("voyage-context-3", transport=transport)

        result = provider.embed_contextualized([["a", "b"], ["c"], ["d", "e", "f"]])

        # Nested shape mirrors the mixed batch sizes (2, 1, 3).
        assert [len(group) for group in result] == [2, 1, 3]
        # Each embedding encodes (outer_index, inner_index): order is preserved.
        assert result[0] == [[0.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
        assert result[1] == [[1.0, 0.0, 0.0]]
        assert result[2] == [[2.0, 0.0, 0.0], [2.0, 1.0, 0.0], [2.0, 2.0, 0.0]]
        assert provider.last_usage_tokens == 100

    def test_context_model_documents_never_hit_flat_endpoint(self, monkeypatch):
        # 400-on-flat regression guard: a context model routed through the
        # document batch path must POST to the contextualized endpoint only.
        monkeypatch.setattr(provider_mod, "count_tokens", lambda _t: 1)
        monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
        transport = FakeTransport(_context_response([1, 1]))
        provider = VoyageProvider("voyage-context-4", transport=transport)

        vectors = provider.embed_documents(["doc-a", "doc-b"])

        assert len(vectors) == 2
        urls = [call["url"] for call in transport.calls]
        assert urls == [_CONTEXT_URL]
        assert _FLAT_URL not in urls
        assert transport.calls[0]["payload"]["inputs"] == [["doc-a"], ["doc-b"]]

    def test_context_model_query_uses_context_endpoint(self, monkeypatch):
        monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
        transport = FakeTransport(_context_response([1]))
        provider = VoyageProvider("voyage-context-3", transport=transport)

        provider.embed_query("what is the answer")

        call = transport.calls[0]
        assert call["url"] == _CONTEXT_URL
        assert call["payload"]["inputs"] == [["what is the answer"]]
        assert call["payload"]["input_type"] == "query"

    def test_flat_request_refuses_context_model(self, monkeypatch):
        # The flat payload path structurally refuses a context model, so the
        # 400-producing flat request can never be emitted.
        monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
        provider = VoyageProvider("voyage-context-3", transport=FakeTransport())
        with pytest.raises(VoyageError) as excinfo:
            provider._request(("x",), input_type="document")
        assert excinfo.value.kind == "bad_request"

    def test_non_context_model_keeps_flat_endpoint(self, monkeypatch):
        monkeypatch.setattr(provider_mod, "count_tokens", lambda _t: 1)
        monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
        flat_body = {"data": [{"index": 0, "embedding": [1.0, 2.0, 3.0]}]}
        transport = FakeTransport(
            HttpResponse(status=200, headers={}, body=json.dumps(flat_body).encode())
        )
        provider = VoyageProvider("voyage-3", transport=transport)

        provider.embed_documents(["only-doc"])

        assert transport.calls[0]["url"] == _FLAT_URL
        assert transport.calls[0]["payload"]["input"] == ["only-doc"]


class TestPlanContextualizedRequests:
    def test_small_docs_pack_into_one_request(self):
        plan = _plan_contextualized_requests(
            [[10, 20], [30]],
            doc_token_budget=1000,
            request_token_budget=1000,
            request_chunk_budget=100,
            max_inputs=100,
        )
        # One request, two sub-documents (one per source document), unsplit.
        assert plan == [[[(0, 0), (0, 1)], [(1, 0)]]]

    def test_oversize_document_is_split_into_subdocuments(self):
        # Doc 0 sums to 250 tokens over a 100-token doc budget -> contiguous split.
        plan = _plan_contextualized_requests(
            [[60, 60, 60, 70]],
            doc_token_budget=100,
            request_token_budget=100000,
            request_chunk_budget=100000,
            max_inputs=100,
        )
        subdocs = plan[0]
        # Each sub-document stays within the doc budget and preserves chunk order.
        assert subdocs == [[(0, 0)], [(0, 1)], [(0, 2)], [(0, 3)]]

    def test_request_token_budget_forces_new_request(self):
        plan = _plan_contextualized_requests(
            [[80], [80]],
            doc_token_budget=1000,
            request_token_budget=100,
            request_chunk_budget=1000,
            max_inputs=1000,
        )
        assert len(plan) == 2

    def test_max_inputs_forces_new_request(self):
        plan = _plan_contextualized_requests(
            [[1], [1], [1]],
            doc_token_budget=1000,
            request_token_budget=1000,
            request_chunk_budget=1000,
            max_inputs=2,
        )
        assert [len(req) for req in plan] == [2, 1]

    def test_empty(self):
        assert _plan_contextualized_requests(
            [], doc_token_budget=1, request_token_budget=1,
            request_chunk_budget=1, max_inputs=1,
        ) == []


class TestSupportsContextualizedGrouping:
    def test_true_for_context_model(self):
        provider = VoyageProvider("voyage-context-4", transport=FakeTransport())
        assert provider.supports_contextualized_grouping is True

    def test_false_for_plain_model(self):
        provider = VoyageProvider("voyage-3", transport=FakeTransport())
        assert provider.supports_contextualized_grouping is False


class TestEmbedChunkGroupBatches:
    def test_groups_a_message_into_one_inputs_list(self, monkeypatch):
        monkeypatch.setattr(provider_mod, "count_tokens", lambda _t: 1)
        monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
        transport = FakeTransport(_context_response([2, 1]))
        provider = VoyageProvider("voyage-context-3", transport=transport)

        # Two documents: message A has chunks at flat indexes 0,1; message B at 2.
        groups = [[(0, "a0"), (1, "a1")], [(2, "b0")]]
        dispatched: list[tuple[int, ...]] = []
        batches = list(
            provider.embed_chunk_group_batches(
                groups, before_dispatch=lambda idx: dispatched.append(idx)
            )
        )

        # One network request; inputs grouped one inner list per message.
        assert len(transport.calls) == 1
        payload = transport.calls[0]["payload"]
        assert payload["inputs"] == [["a0", "a1"], ["b0"]]
        assert "input" not in payload
        # before_dispatch marked exactly this request's flat chunk indexes.
        assert dispatched == [(0, 1, 2)]
        # One accepted batch carrying per-chunk flat indexes + vectors.
        assert len(batches) == 1
        assert batches[0].indexes == (0, 1, 2)
        assert len(batches[0].vectors) == 3

    def test_per_chunk_vectors_preserve_order(self, monkeypatch):
        monkeypatch.setattr(provider_mod, "count_tokens", lambda _t: 1)
        monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
        # Response encodes (outer_index, inner_index) into each embedding; the
        # transport shuffles wire order, so correct mapping proves order-preservation.
        transport = FakeTransport(_context_response([2, 1], order=[1, 0]))
        provider = VoyageProvider("voyage-context-3", transport=transport)

        groups = [[(5, "a0"), (6, "a1")], [(7, "b0")]]
        (batch,) = list(provider.embed_chunk_group_batches(groups))

        vectors_by_index = dict(zip(batch.indexes, batch.vectors))
        # doc 0 chunk 0 -> (0,0); doc 0 chunk 1 -> (0,1); doc 1 chunk 0 -> (1,0).
        assert vectors_by_index[5] == (0.0, 0.0, 0.0)
        assert vectors_by_index[6] == (0.0, 1.0, 0.0)
        assert vectors_by_index[7] == (1.0, 0.0, 0.0)

    def test_oversize_document_splits_into_two_requests(self, monkeypatch):
        # Force a tiny per-doc/request budget so a 2-chunk message splits.
        monkeypatch.setattr(provider_mod, "count_tokens", lambda _t: 1000)
        monkeypatch.setattr(
            provider_mod, "_VOYAGE_CONTEXT_DOCUMENT_TOKEN_BUDGET", 1000
        )
        monkeypatch.setattr(
            provider_mod, "_VOYAGE_CONTEXT_REQUEST_TOKEN_BUDGET", 1000
        )
        monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
        transport = FakeTransport(
            _context_response([1]), _context_response([1])
        )
        provider = VoyageProvider("voyage-context-3", transport=transport)

        groups = [[(0, "big0"), (1, "big1")]]
        batches = list(provider.embed_chunk_group_batches(groups))

        # Two requests, each one single-chunk sub-document.
        assert len(transport.calls) == 2
        assert transport.calls[0]["payload"]["inputs"] == [["big0"]]
        assert transport.calls[1]["payload"]["inputs"] == [["big1"]]
        # Every chunk still resolved to its own flat index.
        resolved = sorted(i for b in batches for i in b.indexes)
        assert resolved == [0, 1]

    def test_oversize_single_chunk_is_skipped(self, monkeypatch):
        # A single chunk above the per-chunk cap is non-embeddable -> skipped.
        big = provider_mod._VOYAGE_CONTEXT_MAX_CHUNK_TOKENS + 1
        monkeypatch.setattr(
            provider_mod, "count_tokens",
            lambda t: big if t == "huge" else 1,
        )
        monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
        transport = FakeTransport(_context_response([1]))
        provider = VoyageProvider("voyage-context-3", transport=transport)

        groups = [[(0, "huge"), (1, "ok")]]
        (batch,) = list(provider.embed_chunk_group_batches(groups))

        assert provider.last_skipped_documents == [0]
        assert batch.indexes == (1,)
        assert transport.calls[0]["payload"]["inputs"] == [["ok"]]
