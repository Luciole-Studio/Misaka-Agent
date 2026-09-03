"""Collect verified task artifacts into the canonical document store."""

import json
import logging
import os

from misaka.config import CFG
from misaka.core.documents import index as corpus

logger = logging.getLogger(__name__)


def ingest_artifacts(con, task, artifacts=None):
    """Ingest verified task artifacts, rejecting empty or escaping paths."""

    workspace = task["workspace"] or ""
    if artifacts is None:
        try:
            with open(os.path.join(CFG["tasks_root"], task["id"], "report.json"),
                      encoding="utf-8") as handle:
                artifacts = json.load(handle).get("artifacts", [])
        except (OSError, ValueError):
            return []
    root = os.path.realpath(workspace) if workspace else ""
    collected = []
    for relative in artifacts:
        relative = str(relative)
        path = os.path.join(root, relative)
        if not root or os.path.isabs(relative) or not corpus.under(path, root) or not os.path.isfile(path):
            continue
        try:
            doc_id, _pages = corpus.ingest(path, title=f"[{task['id']}] {relative}",
                                           task_id=task["id"])
        except ValueError as error:
            # Every ValueError corpus.ingest raises is a real refusal: no extractor for the
            # suffix, a scanned PDF with no ocrmypdf, bytes that decode under no encoding, an
            # EPUB that is not a readable book. Dropping it without a word is how a card can
            # deliver three files, land none of them in the corpus, and leave no trace anywhere.
            # The caller turns the shortfall into a board event; this line names the reason.
            logger.warning("Card %s deliverable %s did not enter the corpus: %s",
                           task["id"], relative, error)
            continue
        collected.append((doc_id, relative))
    return collected


__all__ = ["ingest_artifacts"]
