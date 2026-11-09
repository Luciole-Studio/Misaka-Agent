"""Source text to diagram model.

Every ``parseX`` returns ``None`` when the source is not that kind of diagram,
or when it exceeds a cap — ``render`` tries each in turn and falls back to a
framed copy of the source when they all decline.

Translated from grok-mermaid 0.2.3's ``src/parse.ts``. MISAKA fork: ``_at`` is reading past the end of a string's
code points, which is ``undefined`` in JavaScript.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final, Literal

from misaka.utils.js import (
    ECMASCRIPT_WHITESPACE,
    jsSplitWhitespace,
    jsTrim,
    jsTrimEnd,
    jsTrimStart,
)

from .graph import (
    MAX_EDGES,
    MAX_GROUP_DEPTH,
    MAX_GROUPS,
    MAX_MEMBERS,
    MAX_NODES,
    ClassInfo,
    Edge,
    Graph,
    Group,
    Head,
    LineKind,
    Shape,
    emptyClassInfo,
    parseDir,
)
from .labels import asciiLower, cleanLabel, decodeHtmlEntities, isIdChar, srcLines


def _at(chars: list[str], i: int) -> str | None:
    return chars[i] if 0 <= i < len(chars) else None


def _hasSpace(s: str) -> bool:
    """``/\\s/.test(s)``."""
    return any(c in ECMASCRIPT_WHITESPACE for c in s)


# ---------------------------------------------------------------- statements


def flushStatement(cur: str, out: list[str]) -> str:
    trimmed = jsTrim(cur)
    if trimmed != "":
        out.append(trimmed)
    return ""


def splitStatements(line: str, out: list[str]) -> None:
    """Split one source line into statements on ``;``, stopping at a ``%%`` comment.

    Quoted spans are opaque, so a label may contain ``;`` and ``%%``.
    """
    chars = list(line)
    cur = ""
    inQuotes = False
    for i, c in enumerate(chars):
        if inQuotes:
            if c == '"':
                inQuotes = False
            cur += c
        elif c == '"':
            inQuotes = True
            cur += c
        elif c == "%" and _at(chars, i + 1) == "%":
            break
        elif c == ";":
            cur = flushStatement(cur, out)
        else:
            cur += c
    flushStatement(cur, out)


def statementsOf(src: str) -> list[str]:
    """All statements in a source block, in order."""
    out: list[str] = []
    for line in srcLines(src):
        splitStatements(line, out)
    return out


def firstWord(s: str) -> str:
    return next((w for w in jsSplitWhitespace(s) if w != ""), "")


def words(s: str) -> list[str]:
    return [w for w in jsSplitWhitespace(s) if w != ""]


def splitOnce(s: str, sep: str) -> tuple[str, str] | None:
    """Split on the first occurrence of ``sep``, Rust's ``split_once``."""
    i = s.find(sep)
    return None if i == -1 else (s[:i], s[i + len(sep) :])


def nonEmpty(s: str) -> str | None:
    return None if s == "" else s


def headerKind(statements: list[str]) -> str | None:
    """Diagram kind from the header statement, lowercased."""
    if len(statements) == 0:
        return None
    kind = firstWord(statements[0])
    return None if kind == "" else asciiLower(kind)


# A diagram type this renderer draws.
type DiagramKind = Literal["flowchart", "state", "class", "er", "sequence"]


def diagramKind(src: str) -> DiagramKind | None:
    """The kind of diagram ``src`` declares, or ``None`` if its header names no type
    this renderer draws.

    Reads the header only — it says nothing about whether the body parses. Pair
    it with ``render`` to tell a source this renderer will never draw from one that
    is merely malformed::

        render(src) is None and diagramKind(src) is not None   # syntax error

    Each branch mirrors the header test in the matching ``parseX``, so the two
    always agree on what they recognise.
    """
    kind = headerKind(statementsOf(src))
    if kind is None:
        return None
    if kind in ("graph", "flowchart"):
        return "flowchart"
    if kind.startswith("statediagram"):
        return "state"
    if kind.startswith("classdiagram"):
        return "class"
    if kind == "erdiagram":
        return "er"
    if kind == "sequencediagram":
        return "sequence"
    return None


# ----------------------------------------------------------------- flowchart


def parseGraph(src: str) -> Graph | None:
    statements = statementsOf(src)
    kind = headerKind(statements)
    if kind not in ("graph", "flowchart"):
        return None

    headerWords = words(statements[0])
    graph = Graph(parseDir(headerWords[1] if len(headerWords) > 1 else "TB"))
    stack: list[int] = []

    for st in statements[1:]:
        first = asciiLower(firstWord(st))
        if first == "subgraph":
            if len(graph.groups) >= MAX_GROUPS or len(stack) >= MAX_GROUP_DEPTH:
                return None
            id, label = parseSubgraphDecl(jsTrim(st[len("subgraph") :]))
            graph.groups.append(Group(id=id, label=label, parent=stack[-1] if stack else None))
            stack.append(len(graph.groups) - 1)
            graph.curGroup = stack[-1] if stack else None
            continue
        if first == "end":
            if stack:
                stack.pop()
            graph.curGroup = stack[-1] if stack else None
            continue
        if first in ("classdef", "class", "style", "linkstyle", "click", "direction"):
            continue
        parseStatement(st, graph)
        if graph.overCap:
            return None

    return None if len(graph.nodes) == 0 else graph


def parseSubgraphDecl(rest: str) -> tuple[str, str]:
    """``subgraph id[Title]``, ``subgraph "Title"``, or a bare title."""
    if rest.startswith('"'):
        close = rest.find('"', 1)
        if close != -1:
            label = rest[1:close]
            return label, decodeHtmlEntities(label)
    open = rest.find("[")
    if open != -1:
        id = jsTrim(rest[:open])
        label = cleanLabel(jsTrim(re.sub(r"\]+\Z", "", rest[open + 1 :])))
        if id != "" and label != "":
            return id, label
    return rest, rest


def parseStatement(st: str, graph: Graph) -> None:
    """A chain of ``node link node link node ...``, each link fanning out over ``&``.

    Parses as far as it can and keeps the prefix, matching upstream and
    mermaid.js. Whatever it could not read is recorded in ``graph.warnings`` rather
    than failing the diagram — see the note on that field.
    """
    chars = list(st)
    i = 0

    head = parseNodeGroup(chars, i, graph)
    if head is None:
        graph.warnings.append(f'dropped, does not start with a node: "{st}"')
        return
    prev = head[0]
    i = head[1]

    while True:
        i = skipSpaces(chars, i)
        if i >= len(chars):
            break
        link = parseLink(chars, i)
        if link is None:
            graph.warnings.append(f'dropped, expected a link: "{"".join(chars[i:])}"')
            break
        i = skipSpaces(chars, link.next)
        target = parseNodeGroup(chars, i, graph)
        if target is None:
            graph.warnings.append(f'dropped, link has no target: "{st}"')
            break
        i = target[1]
        for f in prev:
            for t in target[0]:
                # `A <-- B` reads right-to-left: swap the endpoints so the arrow that
                # was written on the left becomes a normal forward head.
                reversed = link.left == "arrow" and link.right != "arrow"
                pushed = graph.pushEdge(Edge(
                    from_=t if reversed else f,
                    to=f if reversed else t,
                    label=link.label,
                    headTo="arrow" if reversed else link.right,
                    headFrom=link.right if reversed else link.left,
                    line=link.line,
                ))
                if not pushed:
                    return
        prev = target[0]


def parseNodeGroup(chars: list[str], start: int, graph: Graph) -> tuple[list[int], int] | None:
    """One or more nodes joined by ``&``, which fan out into a cross product."""
    first = parseNode(chars, start, graph)
    if first is None:
        return None
    group = [first[0]]
    i = first[1]
    while True:
        j = skipSpaces(chars, i)
        if _at(chars, j) != "&":
            break
        nextNode = parseNode(chars, j + 1, graph)
        if nextNode is None:
            return None
        group.append(nextNode[0])
        i = nextNode[1]
    return group, i


def skipSpaces(chars: list[str], i: int) -> int:
    while i < len(chars) and chars[i] in (" ", "\t"):
        i += 1
    return i


def parseNode(chars: list[str], start: int, graph: Graph) -> tuple[int, int] | None:
    i = skipSpaces(chars, start)
    idStart = i
    while i < len(chars) and isIdChar(chars[i]):
        i += 1
    if i == idStart:
        return None
    id = "".join(chars[idStart:i])

    shaped = readShapeAt(chars, i)
    if shaped.unclosed is not None:
        graph.warnings.append(f'node "{id}": label is missing its closing `{shaped.unclosed}`')
    index = graph.nodeIndex(id, shaped.label, shaped.shape)
    if index is None:
        return None

    # `id:::name` (after any shape) attaches a style class; swallow it so the
    # statement keeps parsing — upstream drops the rest of the line here.
    next = shaped.after
    if _at(chars, next) == ":" and _at(chars, next + 1) == ":" and _at(chars, next + 2) == ":":
        k = next + 3
        while k < len(chars) and (isIdChar(chars[k]) or chars[k] == "-"):
            k += 1
        # A name never ends in `-`: back off so `A:::x-->B` keeps its link.
        while k > next + 3 and chars[k - 1] == "-":
            k -= 1
        if k > next + 3:
            next = k
    return index, next


@dataclass(slots=True)
class Shaped:
    """What a shape bracket yielded. ``unclosed`` is set when the bracket never closed."""

    shape: Shape
    label: str | None
    after: int
    # The closing token that was expected but never found.
    unclosed: str | None = None


def readShapeAt(chars: list[str], i: int) -> Shaped:
    """Dispatch on the bracket following an id to pick shape and closing token."""
    c = _at(chars, i)
    n = _at(chars, i + 1)
    if c == "[":
        if n == "[":
            return readShape(chars, i + 2, "]]", "rect")
        if n == "(":
            return readShape(chars, i + 2, ")]", "round")
        return readShape(chars, i + 1, "]", "rect")
    if c == "(":
        if n == "(":
            return readShape(chars, i + 2, "))", "round")
        if n == "[":
            return readShape(chars, i + 2, "])", "round")
        return readShape(chars, i + 1, ")", "round")
    if c == "{":
        if n == "{":
            return readShape(chars, i + 2, "}}", "diamond")
        return readShape(chars, i + 1, "}", "diamond")
    if c == ">":
        return readShape(chars, i + 1, "]", "rect")
    return Shaped(shape="rect", label=None, after=i)


def readShape(chars: list[str], start: int, closer: str, shape: Shape) -> Shaped:
    """Read label text up to ``closer``.

    Quoting is decided by the first non-space character: inside a quoted label
    the closer is ignored until the quote closes, so ``A["a] b"]`` is one node.
    An unquoted label ends at the first closer, so ``A[5" pipe]`` keeps its quote.
    """
    j = start
    while _at(chars, j) in (" ", "\t"):
        j += 1
    quoted = _at(chars, j) == '"'

    i = start
    text = ""
    inQuotes = False
    while i < len(chars):
        c = chars[i]
        if quoted and c == '"':
            inQuotes = not inQuotes
            text += c
            i += 1
            continue
        if not inQuotes and "".join(chars[i : i + len(closer)]) == closer:
            return Shaped(shape=shape, label=cleanLabel(text), after=i + len(closer))
        text += c
        i += 1
    # Ran off the end still looking for the closer: everything after the opening
    # bracket became label text, so any link operator in it was swallowed.
    return Shaped(shape=shape, label=cleanLabel(text), after=len(chars), unclosed=closer)


def isLinkChar(c: str | None) -> bool:
    return c in ("-", ".", "=", "<", ">")


@dataclass(slots=True)
class Link:
    left: Head
    right: Head
    line: LineKind
    label: str | None
    next: int


def parseLink(chars: list[str], start: int) -> Link | None:
    """Read a link operator and its label.

    Labels come in two forms: ``-->|text|`` and the inline ``-- text -->``, the
    latter only when the first operator carried no head.
    """
    i = skipSpaces(chars, start)
    left: Head = "none"
    # A leading `o`/`x` decorates the tail, but only directly before an operator.
    if _at(chars, i) in ("o", "x") and _at(chars, i + 1) in ("-", ".", "="):
        left = "circle" if chars[i] == "o" else "cross"
        i += 1

    opStart = i
    while i < len(chars) and isLinkChar(chars[i]):
        i += 1
    if i == opStart:
        return None
    op1 = "".join(chars[opStart:i])
    if left == "none" and op1.startswith("<"):
        left = "arrow"

    line = lineKind(op1)
    right: Head = "arrow" if ">" in op1 else "none"
    if right == "none":
        trailing = trailingHead(chars, i)
        if trailing is not None:
            right = trailing[0]
            i = trailing[1]

    if _at(chars, i) == "|":
        i += 1
        lStart = i
        while i < len(chars) and chars[i] != "|":
            i += 1
        label = cleanLabel("".join(chars[lStart:i]))
        if _at(chars, i) == "|":
            i += 1
        return Link(left=left, right=right, line=line, label=nonEmpty(label), next=i)

    if right == "none":
        textStart = skipSpaces(chars, i)
        j = textStart
        while j < len(chars) and not isLinkChar(chars[j]):
            j += 1
        if j < len(chars) and j > textStart and chars[j] != "<":
            text = "".join(chars[textStart:j])
            op2Start = j
            while j < len(chars) and isLinkChar(chars[j]):
                j += 1
            op2 = "".join(chars[op2Start:j])
            if ">" in op2:
                right = "arrow"
            else:
                trailing = trailingHead(chars, j)
                if trailing is not None:
                    right = trailing[0]
                    j = trailing[1]
            if line == "solid":
                line = lineKind(op2)
            return Link(left=left, right=right, line=line, label=nonEmpty(cleanLabel(text)), next=j)

    return Link(left=left, right=right, line=line, label=None, next=i)


def lineKind(op: str) -> LineKind:
    if "=" in op:
        return "thick"
    if "." in op:
        return "dotted"
    return "solid"


def trailingHead(chars: list[str], i: int) -> tuple[Head, int] | None:
    """A trailing ``o``/``x`` head, only when followed by a statement boundary."""
    c = _at(chars, i)
    head: Head | None = "circle" if c == "o" else "cross" if c == "x" else None
    if head is None:
        return None
    after = _at(chars, i + 1)
    boundary = after is None or after in (" ", "\t", "|", "&", ";")
    return (head, i + 1) if boundary else None


# --------------------------------------------------------------------- state


def parseState(src: str) -> Graph | None:
    statements = statementsOf(src)
    kind = headerKind(statements)
    if kind is None or not kind.startswith("statediagram"):
        return None

    graph = Graph()
    inNote = False

    for raw in statements[1:]:
        if inNote:
            if asciiLower(raw) == "end note":
                inNote = False
            continue
        st = dropStyleTags(raw)
        first = asciiLower(firstWord(st))
        if first == "direction":
            stWords = words(st)
            graph.dir = parseDir(stWords[1] if len(stWords) > 1 else "")
        elif first == "note":
            # A single-line `note ... : text` needs no terminator.
            if ":" not in st:
                inNote = True
        elif first == "state":
            if parseStateDecl(st, graph) is None:
                return None
        elif first in ("classdef", "class", "hide", "scale", "}", "--"):
            # Styling and composite-state punctuation carry no layout meaning.
            pass
        elif "-->" in st:
            if parseTransition(st, graph) is None:
                return None
        elif parseStateDesc(st, graph) is None:
            return None
        if graph.overCap:
            return None

    return None if len(graph.nodes) == 0 else graph


def parseStateDecl(st: str, graph: Graph) -> Literal[True] | None:
    """``state "Label" as id``, ``state id <<choice>>``, or ``state id {``."""
    rest = jsTrim(re.sub(r"\{\Z", "", jsTrim(st[len("state") :])))
    if rest == "":
        return True

    if rest.startswith('"'):
        close = rest.find('"', 1)
        if close == -1:
            return None
        label = rest[1:close]
        after = jsTrim(rest[close + 1 :])
        id = jsTrim(after[2:]) if after.startswith("as") else label
        return None if graph.nodeLabel(id, decodeHtmlEntities(label)) is None else True

    shape: Shape = "round"
    id = rest
    stereotyped = False
    pos = rest.find("<<")
    if pos != -1:
        stereo = jsTrim(re.sub(r">>\Z", "", rest[pos + 2 :]))
        if stereo == "choice":
            shape = "diamond"
        id = jsTrim(rest[:pos])
        stereotyped = True
    if id == "" or _hasSpace(id):
        return None
    return None if graph.nodeIndex(id, id if stereotyped else None, shape) is None else True


def parseTransition(st: str, graph: Graph) -> Literal[True] | None:
    """``A --> B: label``, including chains ``A --> B --> C``."""
    rest = st
    prev: int | None = None

    while True:
        split = splitOnce(rest, "-->")
        if split is None:
            break
        lhs, rhs = split

        fromId = jsTrim(re.sub(r"-+\Z", "", jsTrimEnd(lhs)))
        if prev is not None:
            # Mid-chain: the source is the previous target, so nothing may precede.
            if fromId != "":
                return None
            from_ = prev
        else:
            if fromId == "":
                return None
            f = stateEndpoint(graph, fromId, True)
            if f is None:
                return None
            from_ = f

        nextArrow = rhs.find("-->")
        toPartRaw = rhs if nextArrow == -1 else rhs[:nextArrow]
        tail = "" if nextArrow == -1 else rhs[nextArrow:]

        colon = splitOnce(toPartRaw, ":")
        toPart = colon[0] if colon else toPartRaw
        label = nonEmpty(decodeHtmlEntities(jsTrim(colon[1]))) if colon else None

        toId = jsTrim(re.sub(r"-+\Z", "", jsTrimEnd(re.sub(r"\A>+", "", jsTrimStart(toPart)))))
        if toId == "":
            return None
        to = stateEndpoint(graph, toId, False)
        if to is None:
            return None

        if not graph.pushEdge(Edge(from_=from_, to=to, label=label, headTo="arrow", headFrom="none", line="solid")):
            return True
        prev = to
        rest = tail
    return True


def dropStyleTags(st: str) -> str:
    """Remove every ``:::name`` style tag from a statement, parsed with the same
    name scan as the flowchart shorthand. State and class statements are read
    by string splits, and the ``:`` label split would cut inside a ``:::``, so tags
    are dropped before dispatch rather than at each id."""
    chars = list(st)
    out: list[str] = []
    i = 0
    while i < len(chars):
        if chars[i] == ":" and _at(chars, i + 1) == ":" and _at(chars, i + 2) == ":":
            k = i + 3
            while k < len(chars) and (isIdChar(chars[k]) or chars[k] == "-"):
                k += 1
            while k > i + 3 and chars[k - 1] == "-":
                k -= 1
            if k > i + 3:
                i = k
                continue
        out.append(chars[i])
        i += 1
    return "".join(out)


def stateEndpoint(graph: Graph, id: str, isSource: bool) -> int | None:
    """``[*]`` is start or end depending on which side of the arrow it sits."""
    if id == "[*]":
        return graph.nodeIndex("[*]start" if isSource else "[*]end", "●", "round")
    return graph.nodeIndex(id, None, "round")


def parseStateDesc(st: str, graph: Graph) -> Literal[True] | None:
    """``id: description``, or a bare state name."""
    split = splitOnce(st, ":")
    if split:
        id = jsTrim(split[0])
        desc = jsTrim(split[1])
        if id == "" or _hasSpace(id) or desc == "":
            return None
        return None if graph.nodeLabel(id, decodeHtmlEntities(desc)) is None else True
    if _hasSpace(st):
        return None
    return None if graph.nodeIndex(st, None, "round") is None else True


# --------------------------------------------------------------------- class

# Relation operators, longest-first so `--|>` wins over `--`.
CLASS_OPS: Final[list[tuple[str, Head, Head, LineKind]]] = [
    ("<|--", "triangle", "none", "solid"),
    ("--|>", "none", "triangle", "solid"),
    ("<|..", "triangle", "none", "dotted"),
    ("..|>", "none", "triangle", "dotted"),
    ("*--", "diamondFill", "none", "solid"),
    ("--*", "none", "diamondFill", "solid"),
    ("o--", "diamondOpen", "none", "solid"),
    ("--o", "none", "diamondOpen", "solid"),
    ("<--", "arrow", "none", "solid"),
    ("-->", "none", "arrow", "solid"),
    ("<..", "arrow", "none", "dotted"),
    ("..>", "none", "arrow", "dotted"),
    ("--", "none", "none", "solid"),
    ("..", "none", "none", "dotted"),
]

MAX_CLASS_OP: Final = 4


def parseClass(src: str) -> tuple[Graph, list[ClassInfo]] | None:
    statements = statementsOf(src)
    kind = headerKind(statements)
    if kind is None or not kind.startswith("classdiagram"):
        return None

    graph = Graph()
    infos: list[ClassInfo] = []

    def sync() -> None:
        while len(infos) < len(graph.nodes):
            infos.append(emptyClassInfo())

    def declare(name: str) -> int | None:
        """Declare a class, keeping ``infos`` aligned with ``graph.nodes``."""
        idx = graph.nodeIndex(name, None, "rect")
        sync()
        return idx

    curClass: int | None = None

    for raw in statements[1:]:
        if curClass is not None:
            if raw == "}":
                curClass = None
            else:
                pushMember(infos[curClass], raw)
            continue
        st = dropStyleTags(raw)

        first = asciiLower(firstWord(st))
        if first == "direction":
            stWords = words(st)
            graph.dir = parseDir(stWords[1] if len(stWords) > 1 else "")
            continue
        if first in ("note", "callback", "click", "link", "style", "cssclass", "classdef", "namespace", "}"):
            continue
        if first == "class":
            rest = jsTrim(st[len("class") :])
            open = rest.endswith("{")
            name = jsTrim(rest[:-1]) if open else rest
            if name == "" or _hasSpace(name):
                return None
            idx = declare(name)
            if idx is None:
                return None
            if open:
                curClass = idx
            continue

        if st.startswith("<<"):
            split = splitOnce(st[2:], ">>")
            if split is None:
                return None
            name = jsTrim(split[1])
            if name == "" or _hasSpace(name):
                return None
            idx = declare(name)
            if idx is None:
                return None
            infos[idx].annotation = jsTrim(split[0])
            continue

        rel = parseClassRelation(st)
        if rel is not None:
            f = declare(rel.from_)
            if f is None:
                return None
            t = declare(rel.to)
            if t is None:
                return None
            if len(graph.edges) >= MAX_EDGES:
                return None
            graph.edges.append(Edge(from_=f, to=t, label=rel.label, headTo=rel.headTo, headFrom=rel.headFrom, line=rel.line))
            continue

        member = splitOnce(st, ":")
        if member:
            id = jsTrim(member[0])
            text = jsTrim(member[1])
            if id == "" or _hasSpace(id) or text == "":
                return None
            idx = declare(id)
            if idx is None:
                return None
            pushMember(infos[idx], text)
            continue
        return None

    if len(graph.nodes) == 0:
        return None
    sync()
    return graph, infos


def pushMember(info: ClassInfo, raw: str) -> None:
    """Add a member to the attribute or method compartment, eliding past the cap."""
    if raw.startswith("<<"):
        split = splitOnce(raw[2:], ">>")
        if split:
            info.annotation = jsTrim(split[0])
        return
    member = decodeHtmlEntities(displayGenerics(jsTrim(raw)))
    target = info.methods if "(" in member else info.attrs
    if len(target) < MAX_MEMBERS:
        target.append(member)
    elif len(target) == MAX_MEMBERS:
        target.append("…")


@dataclass(slots=True)
class ClassRelation:
    from_: str
    to: str
    headFrom: Head
    headTo: Head
    line: LineKind
    label: str | None


def parseClassRelation(st: str) -> ClassRelation | None:
    chars = list(st)
    found: tuple[int, str, Head, Head, LineKind] | None = None

    for pos in range(len(chars)):
        tail = "".join(chars[pos : pos + MAX_CLASS_OP])
        for op, headFrom, headTo, line in CLASS_OPS:
            if not tail.startswith(op):
                continue
            # `o` is also an identifier character: skip a match glued to a name.
            if op.startswith("o") and pos > 0 and isIdChar(chars[pos - 1]):
                continue
            after = _at(chars, pos + len(op))
            if op.endswith("o") and after is not None and isIdChar(after):
                continue
            found = (pos, op, headFrom, headTo, line)
            break
        if found is not None:
            break
    if found is None:
        return None

    lhsRaw = jsTrim("".join(chars[: found[0]]))
    rhsRaw = jsTrim("".join(chars[found[0] + len(found[1]) :]))

    lhs, cardFrom = stripCardinalitySuffix(lhsRaw)
    rhs, cardTo = stripCardinalityPrefix(rhsRaw)

    split = splitOnce(rhs, ":")
    toId = jsTrim(split[0] if split else rhs)
    relLabel = nonEmpty(decodeHtmlEntities(jsTrim(split[1]))) if split else None

    if lhs == "" or toId == "" or _hasSpace(lhs) or _hasSpace(toId):
        return None

    label = nonEmpty(" ".join(s for s in (cardFrom, relLabel if relLabel is not None else "", cardTo) if s != ""))
    return ClassRelation(from_=lhs, to=toId, headFrom=found[2], headTo=found[3], line=found[4], label=label)


def stripCardinalitySuffix(s: str) -> tuple[str, str]:
    """``Class "1"`` — a quoted cardinality trailing the left-hand name."""
    t = jsTrimEnd(s)
    if t.endswith('"'):
        rest = t[:-1]
        q = rest.rfind('"')
        if q != -1:
            return jsTrimEnd(rest[:q]), rest[q + 1 :]
    return t, ""


def stripCardinalityPrefix(s: str) -> tuple[str, str]:
    """``"0..*" Class`` — a quoted cardinality leading the right-hand name."""
    t = jsTrimStart(s)
    if t.startswith('"'):
        rest = t[1:]
        q = rest.find('"')
        if q != -1:
            return jsTrimStart(rest[q + 1 :]), rest[:q]
    return t, ""


def displayGenerics(s: str) -> str:
    """Mermaid writes generics as ``List~T~``; show them as ``List<T>``."""
    out = ""
    isOpen = False
    for c in s:
        if c == "~":
            out += ">" if isOpen else "<"
            isOpen = not isOpen
        else:
            out += c
    return out


# ------------------------------------------------------------------------ ER


def parseEr(src: str) -> tuple[Graph, list[ClassInfo]] | None:
    statements = statementsOf(src)
    if headerKind(statements) != "erdiagram":
        return None

    graph = Graph()
    infos: list[ClassInfo] = []
    curEntity: int | None = None

    for st in statements[1:]:
        if curEntity is not None:
            if st == "}":
                curEntity = None
            else:
                pushErAttribute(infos[curEntity], st)
            continue

        rel = splitErRelationship(st)
        if rel is not None:
            tokens = words(rel[0])
            if len(tokens) != 3:
                return None
            op = parseErOp(tokens[1])
            if op is None:
                return None
            f = erEntity(graph, infos, tokens[0])
            if f is None:
                return None
            t = erEntity(graph, infos, tokens[2])
            if t is None:
                return None
            if len(graph.edges) >= MAX_EDGES:
                return None
            relLabel = "" if rel[1] is None else cleanLabel(rel[1])
            graph.edges.append(Edge(
                from_=f,
                to=t,
                label=nonEmpty(" ".join(s for s in (op[0], relLabel, op[1]) if s != "")),
                headTo="none",
                headFrom="none",
                line=op[2],
            ))
            continue

        open = st.endswith("{")
        decl = jsTrim(st[:-1]) if open else st
        if decl == "" or len(words(decl)) != 1:
            return None
        idx = erEntity(graph, infos, decl)
        if idx is None:
            return None
        if open:
            curEntity = idx

    if len(graph.nodes) == 0:
        return None
    while len(infos) < len(graph.nodes):
        infos.append(emptyClassInfo())
    return graph, infos


def erEntity(graph: Graph, infos: list[ClassInfo], token: str) -> int | None:
    open = token.find("[")
    idx: int | None
    if open != -1:
        id = token[:open]
        label = cleanLabel(re.sub(r"\]+\Z", "", token[open + 1 :]))
        if id == "" or label == "":
            return None
        idx = graph.nodeLabel(id, label)
    else:
        idx = graph.nodeIndex(token, None, "rect")
    if idx is None:
        return None
    while len(infos) < len(graph.nodes):
        infos.append(emptyClassInfo())
    return idx


def splitErRelationship(st: str) -> tuple[str, str | None] | None:
    split = splitOnce(st, ":")
    rel = split[0] if split else st
    label = jsTrim(split[1]) if split else None
    return (rel, label) if any(parseErOp(t) is not None for t in words(rel)) else None


def isAscii(s: str) -> bool:
    return all(ord(c) <= 0x7F for c in s)


def parseErOp(tok: str) -> tuple[str, str, LineKind] | None:
    """A crow's-foot operator: two cardinality glyphs around ``--`` or ``..``."""
    if not isAscii(tok) or len(tok) != 6:
        return None
    mid = tok[2:4]
    line: LineKind | None = "solid" if mid == "--" else "dotted" if mid == ".." else None
    if line is None:
        return None
    cardL = erCard(tok[0:2])
    cardR = erCard(tok[4:6])
    return None if cardL is None or cardR is None else (cardL, cardR, line)


def erCard(tok: str) -> str | None:
    match tok:
        case "|o" | "o|":
            return "0..1"
        case "||":
            return "1"
        case "}o" | "o{":
            return "0..*"
        case "}|" | "|{":
            return "1..*"
        case _:
            return None


def pushErAttribute(info: ClassInfo, raw: str) -> None:
    """ER attributes are ``type name``; a trailing quoted comment is dropped."""
    parts: list[str] = []
    for tok in words(raw):
        if tok.startswith('"'):
            break
        parts.append(tok)
    if len(parts) == 0:
        return
    line = decodeHtmlEntities(" ".join(parts))
    if len(info.attrs) < MAX_MEMBERS:
        info.attrs.append(line)
    elif len(info.attrs) == MAX_MEMBERS:
        info.attrs.append("…")


# ------------------------------------------------------------------ sequence

type SeqHead = Literal["arrow", "cross"]

# Message operators, longest-first so `-->>` wins over `-->`.
SEQ_OPS: Final[list[tuple[str, bool, SeqHead]]] = [
    ("-->>", True, "arrow"),
    ("->>", False, "arrow"),
    ("--x", True, "cross"),
    ("-x", False, "cross"),
    ("--)", True, "arrow"),
    ("-)", False, "arrow"),
    ("-->", True, "arrow"),
    ("->", False, "arrow"),
]

MAX_SEQ_OP: Final = 4


@dataclass(slots=True)
class NoteAnchor:
    """``{kind: "over", from, to}``, ``{kind: "left", at}`` or ``{kind: "right", at}``."""

    kind: Literal["over", "left", "right"]
    from_: int = 0
    to: int = 0
    at: int = 0


@dataclass(slots=True)
class SeqItem:
    """A ``message`` (``from_``, ``to``, ``text``, ``dashed``, ``head``), a ``note`` (``anchor``, ``text``) or a
    ``divider`` (``text``)."""

    kind: Literal["message", "note", "divider"]
    text: str | None = None
    from_: int = 0
    to: int = 0
    dashed: bool = False
    head: SeqHead = "arrow"
    anchor: NoteAnchor | None = None


@dataclass(slots=True)
class Sequence:
    labels: list[str] = field(default_factory=list)
    index: dict[str, int] = field(default_factory=dict)
    items: list[SeqItem] = field(default_factory=list)

    def participant(self, id: str, label: str | None) -> int | None:
        existing = self.index.get(id)
        if existing is not None:
            if label is not None:
                self.labels[existing] = label
            return existing
        if len(self.labels) >= MAX_NODES:
            return None
        self.index[id] = len(self.labels)
        self.labels.append(label if label is not None else id)
        return len(self.labels) - 1


def parseSequence(src: str) -> Sequence | None:
    statements = statementsOf(src)
    if headerKind(statements) != "sequencediagram":
        return None

    seq = Sequence()
    autonumber = False
    msgCount = 0
    # One entry per open block; `True` when it draws a divider on `end`.
    blocks: list[bool] = []

    for st in statements[1:]:
        first = firstWord(st)
        lower = asciiLower(first)

        if lower in ("participant", "actor"):
            rest = jsTrim(st[len(first) :])
            if rest == "":
                return None
            asSplit = splitOnce(rest, " as ")
            if seq.participant(jsTrim(asSplit[0]) if asSplit else rest, cleanLabel(asSplit[1]) if asSplit else None) is None:
                return None
            continue
        if lower == "autonumber":
            autonumber = True
            continue
        if lower in ("activate", "deactivate", "create", "destroy", "title", "acctitle", "accdescr", "links", "link", "properties"):
            continue
        if lower == "note":
            note = parseNoteAnchor(jsTrim(st[len(first) :]), seq)
            if note is None:
                return None
            if len(seq.items) >= MAX_EDGES:
                return None
            seq.items.append(SeqItem(kind="note", anchor=note[1], text=note[0]))
            continue
        if lower in ("loop", "alt", "opt", "par", "critical", "break", "else", "and", "option"):
            if lower in ("else", "and", "option"):
                # A continuation only divides a block that opened one.
                if not (blocks and blocks[-1] is True):
                    continue
            else:
                blocks.append(True)
            if len(seq.items) >= MAX_EDGES:
                return None
            seq.items.append(SeqItem(kind="divider", text=decodeHtmlEntities(st)))
            continue
        if lower in ("rect", "box"):
            blocks.append(False)
            continue
        if lower == "end":
            if blocks and blocks.pop() is True:
                if len(seq.items) >= MAX_EDGES:
                    return None
                seq.items.append(SeqItem(kind="divider", text="end"))
            continue

        msg = parseSeqMessage(st, seq)
        if msg is None:
            return None
        text = msg.text
        if autonumber:
            msgCount += 1
            text = f"{msgCount}." if text is None else f"{msgCount}. {text}"
        if len(seq.items) >= MAX_EDGES:
            return None
        seq.items.append(SeqItem(kind="message", from_=msg.from_, to=msg.to, text=text, dashed=msg.dashed, head=msg.head))

    return None if len(seq.labels) == 0 else seq


def parseNoteAnchor(rest: str, seq: Sequence) -> tuple[str, NoteAnchor] | None:
    lower = asciiLower(rest)
    kind: Literal["over", "left", "right"]
    if lower.startswith("over "):
        kind = "over"
        idsAndText = rest[len("over ") :]
    elif lower.startswith("left of "):
        kind = "left"
        idsAndText = rest[len("left of ") :]
    elif lower.startswith("right of "):
        kind = "right"
        idsAndText = rest[len("right of ") :]
    else:
        return None

    split = splitOnce(idsAndText, ":")
    if split is None:
        return None
    text = decodeHtmlEntities(jsTrim(split[1]))
    parts = [s for s in (jsTrim(part) for part in split[0].split(",")) if s != ""]
    if len(parts) == 0:
        return None
    a = seq.participant(parts[0], None)
    if a is None:
        return None

    if kind != "over":
        return text, NoteAnchor(kind=kind, at=a)
    b = a
    if len(parts) > 1:
        second = seq.participant(parts[1], None)
        if second is None:
            return None
        b = second
    return text, NoteAnchor(kind="over", from_=min(a, b), to=max(a, b))


def parseSeqMessage(st: str, seq: Sequence) -> SeqItem | None:
    chars = list(st)
    found: tuple[int, str, bool, SeqHead] | None = None
    for pos in range(len(chars)):
        tail = "".join(chars[pos : pos + MAX_SEQ_OP])
        for op, dashed, head in SEQ_OPS:
            if tail.startswith(op):
                found = (pos, op, dashed, head)
                break
        if found is not None:
            break
    if found is None:
        return None

    fromId = jsTrim("".join(chars[: found[0]]))
    if fromId == "":
        return None
    # `+`/`-` activate and deactivate the target; they carry no layout meaning.
    rest = re.sub(r"\A[+-]+", "", jsTrimStart("".join(chars[found[0] + len(found[1]) :])))

    split = splitOnce(rest, ":")
    toId = jsTrim(split[0] if split else rest)
    text = nonEmpty(decodeHtmlEntities(jsTrim(split[1]))) if split else None
    if toId == "":
        return None

    from_ = seq.participant(fromId, None)
    if from_ is None:
        return None
    to = seq.participant(toId, None)
    if to is None:
        return None
    return SeqItem(kind="message", from_=from_, to=to, text=text, dashed=found[2], head=found[3])


__all__ = [
    "DiagramKind",
    "NoteAnchor",
    "SeqItem",
    "Sequence",
    "diagramKind",
    "parseClass",
    "parseEr",
    "parseGraph",
    "parseSequence",
    "parseState",
    "pushErAttribute",
    "pushMember",
    "splitStatements",
    "statementsOf",
]
