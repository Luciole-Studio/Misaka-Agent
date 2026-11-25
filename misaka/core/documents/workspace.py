"""Resolve project source files and collect verified task artifacts into the document store."""

import logging
import os
import stat

from misaka.core.documents import index as corpus

logger = logging.getLogger(__name__)

SOURCE_MANIFEST = "SOURCES.md"
SOURCE_DIRECTORY = "sources"
HANDED_OVER_FOLDERS = frozenset({SOURCE_DIRECTORY, "reports"})
ORIGINAL_SUFFIXES = frozenset({".pdf", ".djvu", ".epub", ".doc", ".docx", ".docm", ".ppt", ".pptx", ".pptm",
                               ".xls", ".xlsx", ".xlsm"})


def project_file(path, workspace):
    """Return the real path of a readable regular project file, excluding file symlinks and Git metadata."""
    from misaka.core.web.evidence import check_material_read

    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        real = os.path.realpath(path)
        root = os.path.realpath(workspace)
        if not corpus.under(real, root) or os.path.relpath(real, root).split(os.sep)[0].casefold() == ".git":
            return None
        check_material_read(real)
    except (OSError, ValueError):
        return None
    return real


def document_file(metadata, workspace):
    """Choose the project's copy of a document in the corpus's provenance order."""
    candidates = [metadata.get("orig_path"), *(metadata.get("paths") or [])]
    return next((real for path in candidates if path and (real := project_file(path, workspace))), None)


def doc_source(doc_id, workspace):
    return document_file(corpus._meta(doc_id, workspace=workspace) or {}, workspace)


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
    from misaka.core.network.worker import scoped_artifacts

    artifacts = scoped_artifacts(con, task, artifacts)
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


__all__ = ["SOURCE_DIRECTORY", "SOURCE_MANIFEST", "belongs_in_corpus", "doc_source", "document_file",
           "ingest_artifacts", "project_file"]
