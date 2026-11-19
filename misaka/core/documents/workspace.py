"""Collect verified task artifacts into the canonical document store."""

import logging
import os

from misaka.core.documents import index as corpus

logger = logging.getLogger(__name__)

HANDED_OVER_FOLDERS = frozenset({"sources", "reports"})
ORIGINAL_SUFFIXES = frozenset({".pdf", ".djvu", ".epub", ".doc", ".docx", ".docm", ".ppt", ".pptx", ".pptm",
                               ".xls", ".xlsx", ".xlsm"})


def belongs_in_corpus(task, path):
    """Whether a file a card left joins the corpus by itself. A file outside the card's folder, or
    of a card without one, is not the folder's to judge."""
    output_dir = dict(task).get("output_dir")
    if not output_dir or not corpus.under(path, output_dir):
        return True
    place = os.path.relpath(os.path.realpath(path), os.path.realpath(output_dir)).split(os.sep)
    return (len(place) == 1 or place[0] in HANDED_OVER_FOLDERS
            or os.path.splitext(path)[1].lower() in ORIGINAL_SUFFIXES)


def ingest_artifacts(con, task, artifacts=None):
    """Ingest verified task artifacts, rejecting empty or escaping paths."""

    workspace = task["workspace"] or ""
    root = os.path.realpath(workspace) if workspace else ""
    collected = []
    for relative in artifacts or ():
        relative = str(relative)
        path = os.path.join(root, relative)
        if not root or os.path.isabs(relative) or not corpus.under(path, root) or not os.path.isfile(path):
            continue
        if os.path.splitext(path)[1].lower() not in corpus.READABLE_SUFFIXES:
            logger.debug("Card %s deliverable %s is a format the corpus does not read", task["id"], relative)
            continue
        if not belongs_in_corpus(task, path):
            continue
        try:
            doc_id, _pages = corpus.ingest(path, title=f"[{task['id']}] {relative}",
                                           task_id=task["id"], workspace=root)
        except ValueError as error:
            logger.warning("Card %s deliverable %s did not enter the corpus: %s",
                           task["id"], relative, error)
            continue
        collected.append((doc_id, relative))
    return collected


__all__ = ["belongs_in_corpus", "ingest_artifacts"]
