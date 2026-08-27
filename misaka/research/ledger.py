"""Evidence-backed findings produced by research tasks.

Sisters record findings while doing the work. A finding rests on one of two things: a file the card
registered as its own artifact, or a document in the corpus. For the first this module checks that
the path is a registered artifact, that a fetched page still hashes to the digest it was saved with,
and that the quote appears in it; for the second it hands the quotation to the corpus' own verbatim
verifier and stores the anchor that comes back. It never scores credibility or merges similar claims.
"""
from __future__ import annotations

import hashlib
import json

from misaka.documents import index as corpus
from misaka.documents.index import normalize_for_quote_match
from misaka.research import runs

CLAIM_TYPES = {"fact", "inference", "interpretation", "normative"}
MAX_FINDINGS = 32
MAX_TEXT = 4000
MAX_QUOTE = 2000


def _norm(value):
    """Identity normalization: what makes two submissions of the same finding one row.

    Deliberately *not* ``normalize_for_quote_match``. That one folds width, ligatures and line-break
    hyphens so a real quotation is not rejected over its typography; folding them here would change
    every finding id ever minted, and an id is a name, not a comparison.
    """
    return "".join(str(value or "").split())


def _named(value):
    """The value if it names something, else ``None``: a blank string is an unfilled key."""
    return None if isinstance(value, str) and not value.strip() else value


def _id(run_id, task_id, text):
    key = f"{run_id}\0{task_id}\0{_norm(text).casefold()}".encode()
    return "f_" + hashlib.sha256(key).hexdigest()[:16]


def findings(con, run_id, *, branch_id=None, task_id=None, limit=80):
    q, args = "SELECT * FROM research_findings WHERE run_id=?", [run_id]
    if branch_id is not None:
        q += " AND branch_id=?"
        args.append(branch_id)
    if task_id is not None:
        q += " AND task_id=?"
        args.append(task_id)
    return con.execute(q + " ORDER BY created_at,id LIMIT ?", [*args, int(limit)]).fetchall()


def claims(con, finding_id):
    return con.execute(
        "SELECT * FROM research_claims WHERE finding_id=? ORDER BY created_at,id",
        (finding_id,),
    ).fetchall()


def _artifact_map(con, run_id, task_id):
    out = {}
    for row in runs.artifacts(con, run_id):
        if row["task_id"] != task_id or row["kind"] != "task_output":
            continue
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except ValueError:
            metadata = {}
        source_file = metadata.get("source_file")
        if isinstance(source_file, str):
            out[source_file] = row
    return out


# How far into a file a provenance block may still close. A fetched page carries its block at the
# very top and closes it within a few lines; a block that never closes is not a header at all.
_PROVENANCE_LINES = 40
_HEX = frozenset("0123456789abcdef")


def _page_unmodified(text):
    """Whether a saved page's body still hashes to the ``text_sha256`` its own header records.

    ``None`` means the artifact carries no such digest -- a Sister's own markdown, a page saved
    before the stamp existed, a header whose value is not a digest -- and those ingest exactly as
    they did before this check existed. Only ``False`` is an accusation.

    The shape read here is the stable part of what ``web_fetch._save_page`` writes: ``---`` on line
    one, ``key: value`` scalars, a closing ``---``, then the text that was hashed. It is parsed
    rather than imported on purpose -- the evidence end of the run must not depend on a tool module
    -- so it stays deliberately tolerant about everything except the digest itself.
    """
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return None
    digest, body = "", None
    for index, line in enumerate(lines[1:_PROVENANCE_LINES], start=1):
        if line.strip() == "---":
            body = "\n".join(lines[index + 1:])
            break                # past the header is the page's own text, whatever it looks like
        key, separator, value = line.partition(":")
        if separator and not digest and key.strip().lower() == "text_sha256":
            digest = value.strip().strip("\"'").lower()
    if body is None or len(digest) != 64 or not set(digest) <= _HEX:
        return None
    # The writer wraps the text it hashed in the header's own blank line and a trailing newline;
    # that wrapping is the file's formatting, not the page. ``unwrapped`` undoes exactly it -- one
    # newline at each end, never more, because a plain-text fetch is saved with its own trailing
    # newline inside the digest and trimming the body would call every untouched .txt page
    # modified. The other two candidates only absorb a change to that wrapping; an edit *inside*
    # the text fails all three.
    unwrapped = body.removeprefix("\n").removesuffix("\n")
    return any(hashlib.sha256(candidate.encode()).hexdigest() == digest
               for candidate in {body, unwrapped, body.strip("\n")})


def _artifact_evidence(artifacts, texts, intact, source_file, quote):
    """Check a quotation against a file the card registered; return the claim's columns, or why not.

    ``texts`` and ``intact`` cache one read and one digest per artifact, however many findings quote
    the same file. The reasons are returned rather than raised: each one is deterministic, so the
    card is told what was wrong instead of being asked to try again.
    """
    artifact = artifacts.get(source_file) if isinstance(source_file, str) else None
    if artifact is None:
        return "source_file is not a registered task artifact"
    if not quote or len(quote) > MAX_QUOTE:
        return "has an invalid quotation"
    if artifact["id"] not in texts:
        try:
            content = runs.artifact_text(artifact)
        except (OSError, ValueError):
            content = None
        texts[artifact["id"]] = content
        intact[artifact["id"]] = None if content is None else _page_unmodified(content)
    if texts[artifact["id"]] is None:
        return "source artifact is not readable"
    # Registration hashes whatever is on disk when the card completes, so the artifact sha cannot
    # see an edit made between the fetch and the citation: a page's own fetch-time digest is the
    # only thing that can. Checked before the quotation, because a quote found in a rewritten page
    # is exactly the case this is about.
    if intact[artifact["id"]] is False:
        return ("source page was modified after it was fetched: its text no longer matches the "
                "text_sha256 in its own provenance header")
    # Matching is loosened, storage is not: the same normalizer the corpus verifier uses, so a word
    # the extractor hyphenated across a line break and full-width punctuation do not make a real
    # sentence "not exist" -- the most expensive kind of false accusation this module can make.
    if normalize_for_quote_match(quote) not in normalize_for_quote_match(texts[artifact["id"]]):
        return "quotation was not found in the source artifact"
    return {"artifact_id": artifact["id"], "source_file": source_file,
            "evidence_sha": artifact["sha256"]}


def _doc_evidence(run, doc_id, page, quote):
    """Check a quotation against a corpus document; return the claim's columns, or why not.

    A book or a PDF is not a task artifact and never will be: ``_register_task_artifacts`` skips
    binary originals, because the corpus is what holds them. The corpus' own verifier is the
    authority here, and what it returns -- the page it actually matched on and a hash binding
    doc/page/offset/quote -- is what gets stored, never what the card asserted. So the locator is a
    total function of the verified text: nobody can be sent to a page the sentence is not on.

    Nothing model-supplied is echoed into a reason unless it has already been validated as a
    document id or a page number: these reasons travel back into a prompt.
    """
    if not isinstance(doc_id, str) or not corpus.DOC_ID_RE.fullmatch(doc_id.strip()):
        return "doc_id is not a document id"
    doc_id = doc_id.strip()
    if not quote or len(quote) > MAX_QUOTE:
        return "has an invalid quotation"
    wanted = None
    if page is not None and str(page).strip():
        number = str(page).strip()
        if not number.isdigit() or int(number) < 1:
            return f"the page given with this quotation is not a page number in document {doc_id}"
        wanted = int(number)
    root = next((candidate for candidate in runs.evidence_roots(run)
                 if corpus.resolve_doc(doc_id, workspace=candidate)), None)
    if root is None:
        return (f"document {doc_id} is not indexed from this run's own material; a run cites what "
                "it worked on, so index the file with doc_add from this run before quoting it")
    try:
        found = corpus.verify_quote(doc_id, quote, page=wanted, workspace=root)
        # A page slip is not a forgery, and telling a Sister her real quotation does not exist is
        # how she learns to stop citing sources. Only looked up once the claimed page has failed.
        moved = None if found or wanted is None else corpus.verify_quote(doc_id, quote, workspace=root)
    except (OSError, ValueError) as error:   # a document deleted or replaced under a running card
        return f"document {doc_id} could not be read: {' '.join(str(error).split())[:120]}"
    if found is None:
        if moved is not None:
            return (f"quotation was not found on page {wanted} of document {doc_id}; "
                    f"it is on page {moved['page']} -- cite the page it is on")
        return f"quotation was not found in document {doc_id}"
    return {"artifact_id": None, "source_file": f"doc:{doc_id}#p{found['page']}",
            "evidence_sha": found["claim_hash"]}


def ingest_report(con, run, task, report):
    """Persist the valid findings from a task report and return a short summary.

    A finding names its evidence one of two ways, never both: ``source_file`` + ``quote`` for a file
    the card registered, or ``doc_id`` + ``page`` + ``quote`` for a document in the corpus.

    ``findings`` may be absent only in pre-schema reports (check_report enforces the array
    at submission for current research cards). Invalid entries
    are dropped with a reason rather than retried: every check here is
    deterministic, so a retry would fail the same way.
    """
    raw = report.get("findings")
    if raw is None:
        if int(report.get("schema_version") or 0) >= 1:
            return {"findings": 0, "claims": 0,
                    "dropped": ["findings missing from a current-schema report"]}
        raw = []
    if not isinstance(raw, list):
        return {"findings": 0, "claims": 0, "dropped": ["findings must be an array"]}
    artifacts = _artifact_map(con, run["id"], task["id"])
    link = con.execute(
        "SELECT branch_id FROM research_run_tasks WHERE run_id=? AND task_id=?",
        (run["id"], task["id"]),
    ).fetchone()
    branch_id = link["branch_id"] if link else None
    made, made_claims, dropped, artifact_texts = 0, 0, [], {}
    page_intact = {}                 # artifact id -> _page_unmodified verdict, one hash per file
    for index, item in enumerate(raw[:MAX_FINDINGS]):
        if not isinstance(item, dict):
            dropped.append(f"finding[{index}] is not an object")
            continue
        text = str(item.get("text") or "").strip()
        # A key filled in with nothing names nothing: models routinely emit every key of a schema
        # they were shown, and an empty ``doc_id`` beside a real ``source_file`` is that, not a
        # claim resting on two sources at once.
        source_file = _named(item.get("source_file"))
        doc_id = _named(item.get("doc_id"))
        quote = str(item.get("quote") or "").strip()
        claim_type = str(item.get("claim_type") or "fact").strip()
        if len(text) < 8 or len(text) > MAX_TEXT:
            dropped.append(f"finding[{index}] has an invalid text length")
            continue
        if claim_type not in CLAIM_TYPES:
            dropped.append(f"finding[{index}] has an invalid claim_type")
            continue
        if doc_id is not None and source_file is not None:
            dropped.append(f"finding[{index}] names both a source_file and a doc_id; a claim rests "
                           "on one of them, and which one decides what is verified")
            continue
        # Two shapes, one ledger row: a file this card registered, or a document in the corpus.
        evidence = (_doc_evidence(run, doc_id, item.get("page"), quote) if doc_id is not None
                    else _artifact_evidence(artifacts, artifact_texts, page_intact, source_file, quote))
        if isinstance(evidence, str):
            dropped.append(f"finding[{index}] {evidence}")
            continue
        fid = _id(run["id"], task["id"], text)
        before = con.total_changes
        con.execute(
            "INSERT OR IGNORE INTO research_findings "
            "(id,run_id,branch_id,task_id,text,claim_type,created_at) VALUES (?,?,?,?,?,?,strftime('%s','now'))",
            (fid, run["id"], branch_id, task["id"], text, claim_type),
        )
        made += int(con.total_changes > before)
        before = con.total_changes
        # ``UNIQUE(finding_id,artifact_id,quote)`` cannot dedupe a doc claim: its artifact_id is
        # NULL, and in SQLite one NULL is never equal to another, so a card that settles twice (a
        # re-run bumps its generation) would file the same quotation again. The guard states the
        # identity the constraint means to state; OR IGNORE keeps the constraint's own backstop.
        con.execute(
            "INSERT OR IGNORE INTO research_claims "
            "(finding_id,artifact_id,source_file,quote,evidence_sha,created_at) "
            "SELECT ?,?,?,?,?,strftime('%s','now') WHERE NOT EXISTS ("
            "SELECT 1 FROM research_claims WHERE finding_id=? AND artifact_id IS ? "
            "AND source_file=? AND quote=?)",
            (fid, evidence["artifact_id"], evidence["source_file"], quote, evidence["evidence_sha"],
             fid, evidence["artifact_id"], evidence["source_file"], quote),
        )
        made_claims += int(con.total_changes > before)
    if len(raw) > MAX_FINDINGS:
        dropped.append(f"more than {MAX_FINDINGS} findings submitted; the remainder were ignored")
    return {"findings": made, "claims": made_claims, "dropped": dropped}
