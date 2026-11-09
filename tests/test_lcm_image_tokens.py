"""LCM prices an image at the per-image cost Hermes' own estimator uses, not by its base64 (U11, R44).

2026-10-28, the Last Order of my-research: one 670 KB page image read with ``doc_page_image``
counted 475,853 tokens in ``count_message_tokens``; the preflight check compacted her 1M window
at 328k real tokens, under the 500k threshold."""
from misaka.extensions.misaka_lcm.native.image_token_cost import (
    DEFAULT_IMAGE_TOKEN_COST,
    image_cost_context,
)
from misaka.extensions.misaka_lcm.vendor.config import LCMConfig
from misaka.extensions.misaka_lcm.vendor.engine import LCMEngine
from misaka.extensions.misaka_lcm.vendor.message_content import normalize_content_value
from misaka.extensions.misaka_lcm.vendor.tokens import (
    count_message_tokens,
    count_messages_tokens,
    count_tokens,
)

PAGE = "iVBORw0KGgo" + "AbCdEfGh" * 83_810          # the size of that page image's base64


def page_result(data=PAGE):
    return {"role": "tool", "content": [
        {"type": "text", "text": "page 382 of 監獄學"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}},
    ]}


def test_an_image_costs_the_per_image_price():
    text_only = count_message_tokens({"role": "tool", "content": [{"type": "text", "text": "page 382 of 監獄學"}]})
    with image_cost_context(DEFAULT_IMAGE_TOKEN_COST):               # what a model not yet calibrated pays
        priced = count_message_tokens(page_result())
        assert count_message_tokens(page_result("aA==")) == priced      # its size says nothing
    assert text_only + DEFAULT_IMAGE_TOKEN_COST <= priced < text_only + DEFAULT_IMAGE_TOKEN_COST + 20


def test_the_price_follows_what_the_host_learned_for_the_model():
    with image_cost_context(1100):
        low = count_message_tokens(page_result())
    with image_cost_context(4000):
        high = count_message_tokens(page_result())
    assert high - low == 2900


def test_content_without_images_counts_as_upstream_does():
    for content in ("plain 文字", [{"type": "text", "text": "part 一"}, {"type": "text", "text": "part 二"}],
                    [{"type": "tool_result", "content": "x"}]):
        assert count_message_tokens({"role": "user", "content": content}) == \
            4 + count_tokens(normalize_content_value(content))


def test_a_large_image_in_the_backlog_no_longer_triggers_compaction(tmp_path):
    engine = LCMEngine(config=LCMConfig(database_path=str(tmp_path / "lcm.db"), fresh_tail_count=4,
                                        leaf_chunk_tokens=100))
    engine._session_id = "s"
    engine.context_length = 200_000
    engine.threshold_tokens = 100_000
    tail = [{"role": "assistant", "content": "read it"}, {"role": "user", "content": "next"},
            {"role": "assistant", "content": "done"}, {"role": "user", "content": "thanks"}]
    old = [{"role": "system", "content": "system"}, {"role": "user", "content": "read page 382"}]
    try:
        as_text = old + [{"role": "tool", "content": "page 382 " + PAGE}] + tail
        assert engine.should_compress_preflight(as_text)              # the same bytes as text are over
        engine.shutdown()
        engine = LCMEngine(config=LCMConfig(database_path=str(tmp_path / "lcm2.db"), fresh_tail_count=4,
                                            leaf_chunk_tokens=100))
        engine._session_id = "s"
        engine.context_length = 200_000
        engine.threshold_tokens = 100_000
        with_image = old + [page_result()] + tail
        assert count_messages_tokens(with_image) < 5_000
        assert not engine.should_compress_preflight(with_image)
    finally:
        engine.shutdown()
