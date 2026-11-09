"""What a reading leaves in the project, rendered from its graph after every change and never read
back: ``graph.json`` (the graph as data), ``graph.md`` (the DAG in Mermaid, with every node),
``reading.md`` (the question, the map and where to go on), ``reader.md`` (the reader's stated
interest) and ``nodes/<id>.md``, each reading's note: its header and the sections its actions
wrote, as the graph holds them (:func:`note`, which is also what later actions are handed as
earlier understanding)."""

from __future__ import annotations

import json
import os
import re

from misaka.utils.atomic import write_text


def place(ref):
    """``node 0013`` / ``p. 5`` / ``pp. 5-9``, or "" for a material as a whole."""
    parts = []
    if ref.get("node"):
        parts.append(f"node {ref['node']}")
    if ref.get("pages"):
        parts.append(("pp. " if "-" in str(ref["pages"]) else "p. ") + str(ref["pages"]))
    return " ".join(parts)


def ref_label(ref, titles):
    """``Title, node 0013`` / ``Title, p. 5`` / ``Title, pp. 5-9``: a place in a material."""
    title = titles.get(ref.get("doc_id"), ref.get("doc_id", ""))
    where = place(ref)
    return f"{title}, {where}" if where else title


def refs_label(refs, titles):
    """One label per place, a bare title only for a material cited nowhere more precisely."""
    by_doc = {}
    for ref in refs:
        by_doc.setdefault(ref.get("doc_id"), []).append(ref)
    labels = []
    for group in by_doc.values():
        anchored = [ref for ref in group if ref.get("node") or ref.get("pages")]
        for ref in anchored or group[:1]:
            label = ref_label(ref, titles)
            if label not in labels:
                labels.append(label)
    return "; ".join(labels)


def titles(run):
    return {source.id: source.attrs["title"] for source in run.sources()}


def origins(run, node_id):
    return [f"{parent} {attrs.get('how')}" for parent, attrs in run.parents(node_id)]


def open_questions(run):
    return [(node.id, question) for node in run.readings() for question in node.attrs["questions"]]


# -- notes ------------------------------------------------------------------------------------


def note_header(node_id, question, lens):
    return f"# {node_id} · {question}\n" + (f"\n- Lens: {lens}\n" if lens else "")


def demote_headings(body):
    """Push a section's own headings below the section's ``##`` heading, so an answer that opens
    with ``# N1 · ...`` does not sit beside the node's title or the action's; fenced code is left
    alone."""
    fenced, out = False, []
    for line in body.split("\n"):
        if re.match(r"\s*(```|~~~)", line):
            fenced = not fenced
        elif not fenced and (heading := re.match(r"(#{1,5})\s", line)):
            line = "#" * min(2, 6 - len(heading.group(1))) + line
        out.append(line)
    return "\n".join(out)


def section_text(section):
    a = section.attrs
    return f"\n## {a['action']} · {a['mode']} · {a['at']}\n\n{demote_headings(a['text'].strip())}\n"


def note(run, node_id, limit=None):
    """A reading's note: its header, then one ``## action · mode · time`` section per action. With
    ``limit``, only the newest whole sections that fit it (at least the newest one), so a section is
    never cut in the middle of a sentence or a fenced block."""
    node = run.reading(node_id)
    sections = [section_text(section) for section in run.sections(node_id)]
    kept = sections
    if limit is not None:
        kept, size = [], 0
        for text in reversed(sections):
            if kept and size + len(text) > limit:
                break
            kept.insert(0, text)
            size += len(text)
    omitted = "\n(earlier sections are in the node's note)\n" if len(kept) < len(sections) else ""
    return note_header(node_id, node.attrs["question"], node.attrs.get("lens")) + omitted + "".join(kept)


# -- the rendered files ------------------------------------------------------------------------


def session_record(path, role, fallback, **label):
    """A conversation as PR #12's ``graph.json`` lists it: its id and creation time, which pi
    writes into the file's name (``<time>_<id>.jsonl``)."""
    named = re.fullmatch(r"(\d{4}-\d\d-\d\d)T(\d\d)-(\d\d)-(\d\d)-(\d{3})Z_(.+)\.jsonl",
                         os.path.basename(path))
    session_id = named.group(6) if named else os.path.splitext(os.path.basename(path))[0]
    created = f"{named.group(1)}T{named.group(2)}:{named.group(3)}:{named.group(4)}.{named.group(5)}Z" if named else fallback
    return {"sessionId": session_id, "role": role, **label, "sessionFile": path, "createdAt": created}


def graph_data(run):
    """The graph as data, in the shape PR #12's ``graph.json`` had, with what this reading also
    keeps (the leader's conversation, each node's session file, the model of each action)."""
    info, names = run.info, titles(run)
    origins = {source.id: source.attrs.get("origin") for source in run.sources()}

    def ref(item):
        numbers = [int(n) for n in re.findall(r"\d+", str(item.get("pages") or ""))]
        return {"sourceId": item["doc_id"], "docId": item["doc_id"], "title": names.get(item["doc_id"], ""),
                **({"origin": origins[item["doc_id"]]} if origins.get(item["doc_id"]) else {}),
                **({"pageStart": min(numbers), "pageEnd": max(numbers)} if numbers else {}),
                **({"pages": item["pages"]} if item.get("pages") else {}),
                **({"outlineNode": item["node"]} if item.get("node") else {})}

    def line(node):
        # PR #12 starts a line with every node (L0 for N0) and a continuation stays on its node.
        return f"L{node.id[1:]}" if re.fullmatch(r"N\d+", node.id) else node.id

    sessions = []
    if info.get("leader"):
        sessions.append(session_record(info["leader"], "leader", info["created"]))
    for node in run.readings():
        if node.attrs.get("session"):
            actions = node.attrs["actions"]
            role = "compare" if actions and actions[0]["contextMode"] == "compare" else "study"
            sessions.append({**session_record(node.attrs["session"], role, node.attrs["created"], nodeId=node.id,
                                              lineId=line(node)),
                             **({"model": actions[-1]["model"]} if actions and actions[-1].get("model") else {})})
    return {
        "version": 1, "runId": run.id, "question": info["question"], "rootNodeId": "N0",
        "currentNodeId": info["current"], "workspace": info["workspace"], "createdAt": info["created"],
        "updatedAt": run.updated, "leaderSession": info.get("leader"),
        "sources": [{"sourceId": source.id, "docId": source.id, "title": source.attrs["title"],
                     "pages": source.attrs["pages"], **({"origin": source.attrs["origin"]} if source.attrs.get("origin") else {}),
                     "outline": bool(source.attrs.get("outline")),
                     "addedAt": source.attrs.get("added") or info["created"]} for source in run.sources()],
        "nodes": [{"id": node.id, "lineId": line(node), "question": node.attrs["question"],
                   **({"lens": node.attrs["lens"]} if node.attrs.get("lens") else {}),
                   "summary": node.attrs["summary"], "bodyPath": f"nodes/{node.id}.md",
                   "sourceRefs": [ref(item) for item in node.attrs["refs"]],
                   "actions": [dict(action) for action in node.attrs["actions"]],
                   **({"sessionId": session_record(node.attrs["session"], "study", "")["sessionId"],
                       "sessionFile": node.attrs["session"]} if node.attrs.get("session") else {}),
                   "nextQuestions": list(node.attrs["questions"]), "createdAt": node.attrs["created"]}
                  for node in run.readings()],
        "generationEdges": [{"from": edge.source, "to": edge.target, "kind": edge.attrs["how"],
                             **({"label": edge.attrs["label"]} if edge.attrs.get("label") else {})}
                            for edge in sorted(run.view.edges("derives"), key=lambda e: e.ordinal)],
        "relations": [{"from": edge.source, "to": edge.target, "kind": edge.attrs["kind"],
                       "explanation": edge.attrs["explanation"]} for edge in run.relations()],
        "sessions": sessions,
    }


def _mermaid_label(text):
    return re.sub(r"\s+", " ", text.replace('"', "'"))[:80]


def graph_markdown(run):
    names = titles(run)
    lines = [f"# Reading graph · {run.id}", "", f"Question: {run.info['question']}", "", "```mermaid", "flowchart TD"]
    for node in run.readings():
        lens = f" · {node.attrs['lens']}" if node.attrs.get("lens") else ""
        label = _mermaid_label(f"{node.id} {node.attrs['question']}{lens}")
        lines.append(f'    {node.id}["{label}"]')
    for edge in sorted(run.view.edges("derives"), key=lambda e: e.ordinal):
        label = f"{edge.attrs['how']}: {edge.attrs['label']}" if edge.attrs.get("label") else edge.attrs["how"]
        lines.append(f"    {edge.source} -->|{_mermaid_label(label)}| {edge.target}")
    lines += ["```", "", "## Nodes", ""]
    for node in run.readings():
        attrs = node.attrs
        lines.append(f"### [{node.id}] {attrs['question']}")
        if attrs.get("lens"):
            lines += ["", f"Lens: {attrs['lens']}"]
        lines += ["", attrs["summary"] or "(no understanding yet)", "", f"- Note: nodes/{node.id}.md",
                  f"- Actions: {' → '.join(a['action'] for a in attrs['actions']) or 'none'}"]
        if attrs["refs"]:
            lines.append(f"- Material: {refs_label(attrs['refs'], names)}")
        if attrs["questions"]:
            lines += ["- Further questions:", *(f"  - {q}" for q in attrs["questions"])]
        lines.append("")
    if run.relations():
        lines += ["## Relations", ""]
        lines += [f"- {e.source} {e.attrs['kind']} {e.target}: {e.attrs['explanation']}" for e in run.relations()]
        lines.append("")
    lines += [("> Generation edges (fork, compare, reread) say where an understanding came from; "
               "relations are for reading and navigation and do not order the reading."), ""]
    return "\n".join(lines)


def reading_markdown(run):
    info = run.info
    current = run.view.node(info["current"])
    lines = [f"# Reading map · {run.id}", "", "## Question", "", info["question"], "", "## Current node", "",
             f"- {current.id}: {current.attrs['question']}" if current else "- (none)", "", "## Materials", ""]
    for source in run.sources():
        origin = f" (from {source.attrs['origin']})" if source.attrs.get("origin") else ""
        lines.append(f"- {source.id} {source.attrs['title']} · {source.attrs['pages']} pages{origin}")
    if not run.sources():
        lines.append("- (no material yet)")
    lines += ["", "## Map", ""]
    for node in run.readings():
        came = origins(run, node.id)
        lines.append(f"- {node.id} {node.attrs['question']}" + (f" — from {', '.join(came)}" if came else ""))
        if node.attrs["summary"]:
            lines.append(f"  {node.attrs['summary']}")
    asked = open_questions(run)
    if asked:
        lines += ["", "## Asked and not yet answered", "", *(f"- {node}: {q}" for node, q in asked)]
    lines += ["", "## Open", "", "- Graph: graph.md", "- Notes: nodes/", ""]
    return "\n".join(lines)


def reader_markdown(intent):
    unrecorded = ("(Not recorded yet. Tell Last Order what you care about, which terms matter to you and how you "
                  "like to read.)")
    return f"# Reader's interest\n\n{intent.strip() or unrecorded}\n"


def render(run):
    """Rewrite every rendered file of the reading from its graph."""
    directory = run.directory
    os.makedirs(os.path.join(directory, "nodes"), exist_ok=True)
    write_text(os.path.join(directory, "graph.json"), json.dumps(graph_data(run), ensure_ascii=False, indent=2) + "\n")
    write_text(os.path.join(directory, "graph.md"), graph_markdown(run))
    write_text(os.path.join(directory, "reading.md"), reading_markdown(run))
    write_text(os.path.join(directory, "reader.md"), reader_markdown(run.info.get("intent") or ""))
    for node in run.readings():
        write_text(run.note_path(node.id), note(run, node.id))
