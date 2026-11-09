"""An image stays in the session file; LCM stores a marker where it was.

Upstream writes an inline image to a payload file beside the database, because LCM is the only
record its host has. misaka's record is the session file, so that was a second copy of every image:
5.1 GB of `my-research`'s 5.4 GB cache (2026-11-06), read back by none of 111 real LCM results. The
model is unaffected, since its context is rebuilt from the session (`ingest.Replay`), and the
summariser never saw an image: it got the placeholder.

The marker keeps two things the payload did without saying so. Both fail silently.

* Every comparison of a message with its row has to see the marker, not the image (`without` in
  `_message_replay_identity`). Compaction maps messages to rows by content: a row it cannot match
  drops out of what its summaries cover, 8% of the entries of 45 real sessions. Reconciliation
  matches the same way, and a session end then searches for minutes.
* It has to stay opaque to `fence`, which distrusts what it cannot see through. A plain note would
  take the fence off every summary over an image row, while an agent's transcription of that image
  is in the summary. Hence `Externalized ` and `; ref=`, in a form upstream does not take for a
  payload it could load.
"""
from __future__ import annotations

import hashlib


def _marker(block: dict) -> dict:
    url = str((block.get("image_url") or {}).get("url") or "")
    head, separator, data = url.partition(";base64,")
    if block.get("type") != "image_url" or not separator or not head.startswith("data:"):
        return block
    digest = hashlib.sha256(data.encode()).hexdigest()[:16]
    return {"type": "text", "text": f"[Externalized image: {head[5:]}, {len(data)} chars, sha256 {digest}; "
                                    "held by the session file, not LCM; ref=session-file]"}


def without(message):
    """`message` with each inline image as its marker, or `message` itself if it holds none."""
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return message
    blocks = [_marker(block) if isinstance(block, dict) else block for block in content]
    if all(new is old for new, old in zip(blocks, content)):
        return message
    return {**message, "content": blocks}


def install() -> None:
    """Replace images where every write to the store passes. Upstream's list form calls this one by
    module global; `store` imported its own name for it."""
    from ..vendor import ingest_protection, store
    original = ingest_protection.protect_message_for_ingest
    if getattr(original, "misaka_images", False):
        return

    def protect(message, *args, **kwargs):
        return original(without(message), *args, **kwargs)

    protect.misaka_images = True
    ingest_protection.protect_message_for_ingest = protect
    store.protect_message_for_ingest = protect
