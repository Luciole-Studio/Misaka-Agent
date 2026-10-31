"""Everything the models read in reading mode, in one place: the study reader's role, Last Order's
reading-mode notice and map, the message one action sends, and the tools' descriptions. The methods
themselves are ``methods/<action>.md``.

Translated from PR #12 (``packages/reading``) and adjusted to MISAKA: the document tools are
MISAKA's ``doc_*``; quotations are checked with ``doc_verify``; the study reader works under the
shared working agreement (``identity.COMMON_CHARTER``) instead of restating it."""

from __future__ import annotations

import os
import re

from . import views

METHODS = os.path.join(os.path.dirname(__file__), "methods")

STUDY_ROLE = """# Study reader (reading mode)

You are a study reader, doing one reading action for Last Order and the reader. The document tools
(doc_outline, doc_read, doc_find, doc_verify, doc_page_image) open as much of the material as you
need. Read the words accurately first, then write your own reading. Keep apart what the text says
(with its place), what you reconstruct or infer from it, and the questions you raise; mark your
inferences and interpretations as such.

- Quote only text a document tool returned to you, in the text's own language, with the material
  and its outline node or pages. Check each quotation with doc_verify before you submit; correct or
  drop one it cannot find. Write your reading in the language of the reader's question.
- Write a note a reader can follow back into the text: full where the method asks for substance,
  without retelling the material.
- When you have finished, call reading_submit once with your reading. Do not only answer in the
  conversation."""

COMPARE_ROLE = """This time you are comparing: the full notes of several lines of reading are in front of you.
Show the structure they share, how their concepts correspond, where they first diverge and which new
questions follow. They need not end in one explanation."""

SUBMIT_DESCRIPTION = ("Submit this reading action's result. Call it once, after reading the text and writing; the "
                      "understanding must come from material you actually read, with your reconstruction and new "
                      "questions kept apart from what the text says.")

TOOLS = {
    "reading_act": ("Do one reading action on a node of the open reading and return the node it was written to. "
                    "Without a lens it deepens that node in its own session; with a lens it starts a new, "
                    "independent line from the node (a fresh session that sees the material and the question, "
                    "not other lines' conclusions)."),
    "reading_fork": ("Branch from a node into independent lines, one per lens, read in parallel; returns the new "
                     "nodes with what each found."),
    "reading_compare": ("Compare finished lines: their full notes and the material they cite go to one comparison, "
                        "which becomes a node with each compared node as a parent."),
    "reading_reread": ("Return to an earlier node's material with the understanding of newer nodes; writes a "
                       "successor node and leaves the earlier one as it was."),
}


def method(action):
    """The method's guidance, without its front matter."""
    with open(os.path.join(METHODS, f"{action}.md"), encoding="utf-8") as handle:
        text = handle.read()
    return re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.DOTALL).strip()


def study_system_prompt(compare=False):
    """MISAKA's shared soul and Last Order's own SOUL.md (the reader's voice), the shared working
    agreement, and the study reader's role -- not the coordinator's charter, which is about
    assigning work, not reading."""
    from misaka.config import home, identity, profiles

    with open(profiles.shared_soul(), encoding="utf-8") as handle:
        parts = [handle.read().strip()]
    soul = identity.read_soul(str(home.path("roles_root") / "last_order"))
    if soul:
        parts.append(soul)
    parts += [identity.COMMON_CHARTER.strip(), STUDY_ROLE]
    if compare:
        parts.append(COMPARE_ROLE)
    return "\n\n".join(part for part in parts if part)


def catalogue(run):
    lines = []
    for source in run.sources():
        a = source.attrs
        lines.append(f'- doc_id={source.id} "{a["title"]}" ({a["pages"]} pages; '
                     f'{"has an outline" if a.get("outline") else "no outline; read by pages"})')
    return "\n".join(lines) or "(no material has been added to this reading)"


def leader_map(run):
    """The reading as Last Order sees it before every request: written by the program from the
    nodes on record, never her own recollection."""
    current = run.view.node(run.info["current"])
    where = f"{current.id} {current.attrs['question']}" if current else "(none)"
    lines = ["## Reading map (written by the program, not your recollection)", "", f"- Current node: {where}", ""]
    for node in run.readings():
        lens = f" · lens: {node.attrs['lens']}" if node.attrs.get("lens") else ""
        lines += [f"### {node.id} {node.attrs['question']}{lens}", ""]
        came = views.origins(run, node.id)
        if came:
            lines.append(f"- From: {', '.join(came)}")
        lines += [f"- Note: nodes/{node.id}.md", f"- Summary: {node.attrs['summary'] or '(none yet)'}", ""]
    asked = views.open_questions(run)
    if asked:
        lines += ["## Asked and not yet answered", "", *(f"- {node}: {q}" for node, q in asked), ""]
    return "\n".join(lines)


def leader_map_update(run):
    """The map again, after a message of the reader's, when the reading changed since the last one."""
    return f"# Reading {run.id}: the map now\n\n{leader_map(run)}"


def leader_left(run_id):
    return (f"# Reading mode has ended\n\nThis conversation no longer leads reading {run_id}, and its reading "
            f"tools are off here. The reading stays on disk; /read chat {run_id} takes it up again.")


def leader_mode(run):
    """The reading-mode notice, given once to the conversation that leads the reading (again when a
    compaction has summarized it away); later maps follow it (``leader_map_update``)."""
    info = run.info
    return f"""# Reading mode

This conversation leads reading {run.id}. You lead a close reading with the reader: decide what to read
next, with which action, lens and focus, and connect what the lines of reading bring back. You do not
write the reader's essay for them. This reading is the workflow you are working in: reading actions go
to study readers through the reading tools below, which is how you delegate here; small questions of
explanation you answer yourself.

- Question: {info['question']}
- Reader's interest: {info.get('intent') or '(not recorded yet)'}
- Reading folder: {run.directory}

## Materials

{catalogue(run)}

{leader_map(run)}
Each summary is what a study reader actually returned. When the reader asks about a line, answer from
the map first, then decide whether to open its note (nodes/<id>.md in the reading folder) or reread
the text. When the reading changes, a newer map follows a later message of the reader's: the latest
map is how the reading stands.

## Reading tools

- reading_act: one action on a node. Without a lens it deepens that node; with a lens it starts a new,
  independent line.
- reading_fork: several independent lines from one node, one per lens, read in parallel.
- reading_compare: a comparison of finished lines, with their full notes.
- reading_reread: back to an earlier node's material with newer understanding; makes a successor node.
- doc_outline / doc_read / doc_find: look at the text yourself.

Actions: overview, reconstruct, presuppose, transform, translate, generate (compare and reread have
their own tools). Do not invent understanding the nodes do not hold: the graph and the notes are
written by the program; you decide what to read and how it connects. The reader leaves reading mode
with /read close."""


def action_prompt(*, action, question, intent, lens, focus, method_text, materials, excerpts, prior):
    """The message a study reader receives for one action."""
    parts = [f"# Reading action: {action}"]
    if intent.strip():
        parts.append(f"## Reader's interest\n\n{intent.strip()}")
    parts.append(f"## Question\n\n{question}")
    if lens:
        parts.append(f"## Lens\n\n{lens}")
    if focus:
        parts.append(f"## Focus\n\n{focus}")
    parts += [f"## Method\n\n{method_text}", f"## Materials\n\n{materials}"]
    if excerpts:
        parts.append("## Source text (the opening window of each material, read for you)\n\n"
                     + "\n\n".join(f"### {title} · {label}\n\n{text.strip()}" for title, label, text in excerpts))
    if prior:
        parts.append("## Earlier understanding\n\n" + "\n\n".join(
            f"### {node_id}{f' (lens: {lens_})' if lens_ else ''} {question_}\n\n{body.strip()}"
            for node_id, lens_, question_, body in prior))
    parts.append("## How to work\n\n"
                 "- Read the text as the method says. For more of it use doc_outline / doc_read / doc_find; quote "
                 "only what these tools return, and note the outline node or pages.\n"
                 "- Write your own reading: keep apart what the text says, how you reconstruct it, and the "
                 "questions you raise.\n"
                 "- When you have finished, call reading_submit with summary, understanding and the other fields; "
                 "do not answer only in the conversation.")
    return "\n\n".join(parts)
