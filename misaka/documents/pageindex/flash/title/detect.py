"""Document title search over early pages."""

from __future__ import annotations

from ..model import (
    alignment_code,
    heading_score,
)
from ..tokens import (
    is_word_token,
    tokenize_block,
)
from .scoring import (
    TitleCandidate,
    is_cover_like_page,
    is_title_candidate_block,
    score_title_candidate,
)

# --------------------------------------------------------------------------- #
# Title detection state.
# --------------------------------------------------------------------------- #


class TitleSearchState:
    """Title-detection state: document, visited blocks, and current best candidate."""

    __slots__ = ("primary_slot", "secondary_slot", "tertiary_slot")

    def __init__(self, doc):
        self.tertiary_slot = doc
        self.primary_slot: set = set()
        self.secondary_slot: TitleCandidate | None = None


# --------------------------------------------------------------------------- #
# Title-detection driver.
# --------------------------------------------------------------------------- #


def detect_title(doc) -> TitleCandidate | None:
    """Iterate early pages, score title-like block groups, and return the best candidate."""
    state = TitleSearchState(doc)
    has_seen_da = False                                  # "broke into body" flag

    for page in doc.primary_slot:
        # Special branch: landscape cover document
        if (
            doc.secondary_slot.style_slot > len(doc.primary_slot) / 2
            and page.page_index <= 1
            and page.bounds.bbox_width() > page.bounds.bbox_height()
            and page.primary_slot.secondary_slot < 500
        ):
            for idx, block in enumerate(page.secondary_slot):
                if (
                    is_title_candidate_block(block) and id(block) not in state.primary_slot
                    and heading_score(block) > page.primary_slot.primary_slot - 0.1
                ):
                    score_title_candidate(state, page, idx)
            break

        if (
            is_cover_like_page(doc, page)
            or (page.page_index <= 1 and len(doc.primary_slot) >= 10 and page.primary_slot.secondary_slot < 0.8 * doc.secondary_slot.secondary_slot)
        ):
            # Cover / front-matter page
            for idx, block in enumerate(page.secondary_slot):
                if not is_title_candidate_block(block) or id(block) in state.primary_slot:
                    continue
                score = heading_score(block)
                if (
                    (score > doc.secondary_slot.primary_slot + 0.1 and score > page.primary_slot.primary_slot + 0.1)
                    or (score > doc.secondary_slot.primary_slot + 2 and score > page.primary_slot.primary_slot - 0.1)
                    or (score > doc.secondary_slot.primary_slot - 0.1 and score > page.primary_slot.primary_slot - 0.1 and block.isolated_centered)
                    or (score > doc.secondary_slot.primary_slot - 0.1 and score > page.primary_slot.primary_slot - 0.1
                        and page.page_index <= 1 and page.primary_slot.secondary_slot < 500)
                ):
                    score_title_candidate(state, page, idx)
        else:
            # Body page: only consider initial blocks until we hit body text
            local_done = False
            for idx, block in enumerate(page.secondary_slot):
                if id(block) in state.primary_slot:
                    continue
                score = heading_score(block)
                # Block clearly larger than body
                size_trigger = (
                    is_title_candidate_block(block) and (
                        (score > doc.secondary_slot.primary_slot + 0.1 and score > page.primary_slot.primary_slot + 0.1)
                        or (score > doc.secondary_slot.primary_slot + 2 and score > page.primary_slot.primary_slot - 0.1)
                        or (block.isolated_centered and score > page.primary_slot.primary_slot - 0.1)
                        or (page.page_index == 1 and score > page.primary_slot.primary_slot + 2)
                    )
                )
                if size_trigger:
                    score_title_candidate(state, page, idx)
                elif block.is_body_paragraph and not block.isolated_centered:
                    # Body-break flag: stop scanning once body text is reached.
                    if not has_seen_da:
                        if (block.bottom_edge() - page.bounds.bottom_edge() < 2 * page.bounds.bbox_height() / 3):
                            has_seen_da = False
                        elif block.line_count() >= 3 and alignment_code(block) == 4:
                            has_seen_da = True
                        else:
                            digit_or_period = 0
                            tokens = tokenize_block(block)
                            for token in tokens:
                                if is_word_token(token) or token.type == 1:
                                    digit_or_period += 1
                            has_seen_da = digit_or_period >= len(tokens) / 3
                        has_seen_da = not has_seen_da
                    if has_seen_da:
                        local_done = True
                        break
                    local_done = True
                    # Once body text is seen, the flag stays sticky so a later
                    # body block on this page breaks immediately.
                    has_seen_da = True
            if local_done:
                break
            if doc.secondary_slot.secondary_slot < 400:
                break
    return state.secondary_slot
