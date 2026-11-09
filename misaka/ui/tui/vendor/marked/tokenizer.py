"""Translated from marked's ``src/Tokenizer.ts`` (marked 18.0.11, MIT).

MISAKA fork: ``def`` and ``del`` are Python keywords, so those tokenizer methods are ``def_`` and ``del_``; JavaScript
string methods are ``misaka.utils.js`` / ``misaka.utils.js_regexp`` helpers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from misaka.utils import js_regexp
from misaka.utils.js import jsLength, jsSlice, jsTrim, jsTrimEnd, jsTrimStart

from .defaults import defaults
from .helpers import (
    expandTabs,
    findClosingBracket,
    rtrim,
    splitCells,
    trimTrailingBlankLines,
)
from .tokens import Token

if TYPE_CHECKING:
    from .lexer import _Lexer
    from .rules import Rules


def outputLink(cap: list[Any], link: Any, raw: str, lexer: _Lexer, rules: Rules) -> Token | None:
    href = link.href if not isinstance(link, dict) else link.get("href")
    title = (link.title if not isinstance(link, dict) else link.get("title")) or None
    text = js_regexp.replace(cap[1], rules.other.outputLinkReplace, "$1")
    isImage = cap[0][:1] == "!"

    lexer.state.inLink = True
    outerLinkEmitted = lexer.state.linkEmitted
    outerInRawBlock = lexer.state.inRawBlock
    lexer.state.linkEmitted = False
    tokens = lexer.inlineTokens(text)
    textHasLink = lexer.state.linkEmitted
    lexer.state.linkEmitted = outerLinkEmitted
    lexer.state.inLink = False

    if not isImage:
        # CommonMark: "Links may not contain other links, at any level of nesting."
        # Bail so the caller falls through to text and the inner link is the one kept.
        # Images are exempt: their text is flattened into an alt attribute.
        if textHasLink:
            # these tokens are discarded, so undo the raw-block state they opened;
            # leaving it set would suppress escaping for the text that is re-scanned
            lexer.state.inRawBlock = outerInRawBlock
            return None
        lexer.state.linkEmitted = True

    return Token(
        type="image" if isImage else "link",
        raw=raw,
        href=href,
        title=title,
        text=text,
        tokens=tokens,
    )


def indentCodeCompensation(raw: str, text: str, rules: Rules) -> str:
    matchIndentToCode = rules.other.indentCodeCompensation.exec(raw)

    if matchIndentToCode is None:
        return text

    indentToCode = matchIndentToCode[1]

    def compensate(node: str) -> str:
        matchIndentInNode = rules.other.beginningSpace.exec(node)
        if matchIndentInNode is None:
            return node

        indentInNode = matchIndentInNode[0]

        if len(indentInNode) >= len(indentToCode):
            return node[len(indentToCode) :]

        return node

    return "\n".join(compensate(node) for node in text.split("\n"))


class _Tokenizer:
    """Tokenizer"""

    rules: Rules  # set by the lexer
    lexer: _Lexer  # set by the lexer

    def __init__(self, options: Any = None) -> None:
        self.options = options or defaults()

    def space(self, src: str) -> Token | None:
        cap = self.rules.block.newline.exec(src)
        if cap and len(cap[0]) > 0:
            return Token(
                type="space",
                raw=cap[0],
            )
        return None

    def code(self, src: str) -> Token | None:
        cap = self.rules.block.code.exec(src)
        if cap:
            raw = cap[0] if self.options.pedantic else trimTrailingBlankLines(cap[0])
            text = js_regexp.replace(raw, self.rules.other.codeRemoveIndent, "")
            return Token(
                type="code",
                raw=raw,
                codeBlockStyle="indented",
                text=text,
            )
        return None

    def fences(self, src: str) -> Token | None:
        cap = self.rules.block.fences.exec(src)
        if cap:
            raw = cap[0]
            text = indentCodeCompensation(raw, cap[3] or "", self.rules)

            return Token(
                type="code",
                raw=raw,
                lang=js_regexp.replace(jsTrim(cap[2]), self.rules.inline.anyPunctuation, "$1") if cap[2] else cap[2],
                text=text,
            )
        return None

    def heading(self, src: str) -> Token | None:
        cap = self.rules.block.heading.exec(src)
        if cap:
            text = jsTrim(cap[2])

            # remove trailing #s
            if self.rules.other.endingHash.test(text):
                trimmed = rtrim(text, "#")
                if self.options.pedantic:
                    text = jsTrim(trimmed)
                elif not trimmed or self.rules.other.endingSpaceChar.test(trimmed):
                    # CommonMark requires space before trailing #s
                    text = jsTrim(trimmed)

            return Token(
                type="heading",
                raw=rtrim(cap[0], "\n"),
                depth=len(cap[1]),
                text=text,
                tokens=self.lexer.inline(text),
            )
        return None

    def hr(self, src: str) -> Token | None:
        cap = self.rules.block.hr.exec(src)
        if cap:
            return Token(
                type="hr",
                raw=rtrim(cap[0], "\n"),
            )
        return None

    def blockquote(self, src: str) -> Token | None:
        cap = self.rules.block.blockquote.exec(src)
        if cap:
            lines = rtrim(cap[0], "\n").split("\n")
            raw = ""
            text = ""
            tokens: list[Any] = []

            while len(lines) > 0:
                inBlockquote = False
                currentLines = []

                i = 0
                while i < len(lines):
                    # get lines up to a continuation
                    if self.rules.other.blockquoteStart.test(lines[i]):
                        currentLines.append(lines[i])
                        inBlockquote = True
                    elif not inBlockquote:
                        currentLines.append(lines[i])
                    else:
                        break
                    i += 1
                lines = lines[i:]

                currentRaw = "\n".join(currentLines)
                currentText = js_regexp.replace(
                    # precede setext continuation with 4 spaces so it isn't a setext
                    js_regexp.replace(currentRaw, self.rules.other.blockquoteSetextReplace, "\n    $1"),
                    self.rules.other.blockquoteSetextReplace2,
                    "",
                )
                raw = f"{raw}\n{currentRaw}" if raw else currentRaw
                text = f"{text}\n{currentText}" if text else currentText

                # parse blockquote lines as top level tokens
                # merge paragraphs if this is a continuation
                top = self.lexer.state.top
                self.lexer.state.top = True
                self.lexer.blockTokens(currentText, tokens, True)
                self.lexer.state.top = top

                # if there is no continuation then we are done
                if len(lines) == 0:
                    break

                lastToken = tokens[-1] if tokens else None

                if lastToken is not None and lastToken.type == "code":
                    # blockquote continuation cannot be preceded by a code block
                    break
                elif lastToken is not None and lastToken.type == "blockquote":
                    # include continuation in nested blockquote
                    oldToken = lastToken
                    # The continuation lines belong to the same nesting frame as the
                    # nested blockquote, which already had one '>' marker stripped, so
                    # strip one marker from them too. Otherwise a restated marker after a
                    # lazy line is re-parsed as a spurious deeper blockquote.
                    continuation = "\n".join(lines)
                    newText = (
                        oldToken.raw
                        + "\n"
                        + js_regexp.replace(continuation, self.rules.other.blockquoteSetextReplace2, "")
                    )
                    newToken = self.blockquote(newText)
                    assert newToken is not None
                    tokens[len(tokens) - 1] = newToken

                    raw = f"{raw}\n{continuation}"
                    text = text[: len(text) - len(oldToken.text)] + newToken.text
                    break
                elif lastToken is not None and lastToken.type == "list":
                    # include continuation in nested list
                    oldToken = lastToken
                    newText = oldToken.raw + "\n" + "\n".join(lines)
                    newToken = self.list(newText)
                    assert newToken is not None
                    tokens[len(tokens) - 1] = newToken

                    raw = raw[: len(raw) - len(lastToken.raw)] + newToken.raw
                    text = text[: len(text) - len(oldToken.raw)] + newToken.raw
                    lines = newText[len(tokens[-1].raw) :].split("\n")
                    continue

            return Token(
                type="blockquote",
                raw=raw,
                tokens=tokens,
                text=text,
            )
        return None

    def list(self, src: str) -> Token | None:
        cap = self.rules.block.list.exec(src)
        if cap:
            bull = jsTrim(cap[1])
            isordered = len(bull) > 1

            list_ = Token(
                type="list",
                raw="",
                ordered=isordered,
                start=int(bull[:-1]) if isordered else "",
                loose=False,
                items=[],
            )

            bull = f"\\d{{1,9}}\\{bull[-1:]}" if isordered else f"\\{bull}"

            if self.options.pedantic:
                bull = bull if isordered else "[*+-]"

            # Get next list item
            itemRegex = self.rules.other.listItemRegex(bull)
            endsWithBlankLine = False
            # Check if current bullet point can start a new List Item
            while src:
                endEarly = False
                raw = ""
                itemContents = ""
                cap = itemRegex.exec(src)
                if not cap:
                    break

                if self.rules.block.hr.test(src):  # End list if bullet was actually HR (possibly move into itemRegex?)
                    break

                raw = cap[0]
                src = src[len(raw) :]

                line = expandTabs(cap[2].split("\n", 1)[0], len(cap[1]))
                nextLine = src.split("\n", 1)[0]
                blankLine = not jsTrim(line)

                indent = 0
                if self.options.pedantic:
                    indent = 2
                    itemContents = jsTrimStart(line)
                elif blankLine:
                    indent = len(cap[1]) + 1
                else:
                    indent = js_regexp.search(line, self.rules.other.nonSpaceChar)  # Find first non-space char
                    indent = 1 if indent > 4 else indent  # Treat indented code blocks (> 4 spaces) as having only 1 indent
                    itemContents = line[indent:]
                    indent += len(cap[1])

                if blankLine and self.rules.other.blankLine.test(nextLine):  # Items begin with at most one blank line
                    raw += nextLine + "\n"
                    src = src[len(nextLine) + 1 :]
                    endEarly = True

                if not endEarly:
                    nextBulletRegex = self.rules.other.nextBulletRegex(indent)
                    hrRegex = self.rules.other.hrRegex(indent)
                    fencesBeginRegex = self.rules.other.fencesBeginRegex(indent)
                    headingBeginRegex = self.rules.other.headingBeginRegex(indent)
                    htmlBeginRegex = self.rules.other.htmlBeginRegex(indent)
                    blockquoteBeginRegex = self.rules.other.blockquoteBeginRegex(indent)

                    # Check if following lines should be included in List Item
                    while src:
                        rawLine = src.split("\n", 1)[0]
                        nextLine = rawLine

                        # Re-align to follow commonmark nesting rules
                        if self.options.pedantic:
                            nextLine = js_regexp.replace(nextLine, self.rules.other.listReplaceNesting, "  ")
                            nextLineWithoutTabs = nextLine
                        else:
                            nextLineWithoutTabs = js_regexp.replace(nextLine, self.rules.other.tabCharGlobal, "    ")

                        # End list item if found code fences
                        if fencesBeginRegex.test(nextLine):
                            break

                        # End list item if found start of new heading
                        if headingBeginRegex.test(nextLine):
                            break

                        # End list item if found start of html block
                        if htmlBeginRegex.test(nextLine):
                            break

                        # End list item if found start of blockquote
                        if blockquoteBeginRegex.test(nextLine):
                            break

                        # End list item if found start of new bullet
                        if nextBulletRegex.test(nextLine):
                            break

                        # Horizontal rule found
                        if hrRegex.test(nextLine):
                            break

                        if (
                            js_regexp.search(nextLineWithoutTabs, self.rules.other.nonSpaceChar) >= indent
                            or not jsTrim(nextLine)
                        ):  # Dedent if possible
                            itemContents += "\n" + nextLineWithoutTabs[indent:]
                        else:
                            # not enough indentation
                            if blankLine:
                                break

                            # paragraph continuation unless last line was a different block level element
                            if (
                                js_regexp.search(
                                    js_regexp.replace(line, self.rules.other.tabCharGlobal, "    "),
                                    self.rules.other.nonSpaceChar,
                                )
                                >= 4
                            ):  # indented code block
                                break
                            if fencesBeginRegex.test(line):
                                break
                            if headingBeginRegex.test(line):
                                break
                            if hrRegex.test(line):
                                break

                            itemContents += "\n" + nextLine

                        blankLine = not jsTrim(nextLine)

                        raw += rawLine + "\n"
                        src = src[len(rawLine) + 1 :]
                        line = nextLineWithoutTabs[indent:]

                if not list_.loose:
                    # If the previous item ended with a blank line, the list is loose
                    if endsWithBlankLine:
                        list_.loose = True
                    elif self.rules.other.doubleBlankLine.test(raw):
                        endsWithBlankLine = True

                list_.items.append(
                    Token(
                        type="list_item",
                        raw=raw,
                        task=bool(self.options.gfm) and self.rules.other.listIsTask.test(itemContents),
                        loose=False,
                        text=itemContents,
                        tokens=[],
                    )
                )

                list_.raw += raw

            # Do not consume newlines at end of final item. Alternatively, make itemRegex *start* with any newlines to simplify/speed up endsWithBlankLine logic
            lastItem = list_.items[-1] if list_.items else None
            if lastItem:
                lastItem.raw = jsTrimEnd(lastItem.raw)
                lastItem.text = jsTrimEnd(lastItem.text)
            else:
                # not a list since there were no items
                return None
            list_.raw = jsTrimEnd(list_.raw)

            # Item child tokens handled here at end because we needed to have the final item to trim it first
            # First pass: tokenize items and finalize list.loose from spacers before placing checkboxes
            for item in list_.items:
                self.lexer.state.top = False
                item.tokens = self.lexer.blockTokens(item.text, [])

                if not list_.loose:
                    # Check if list should be loose
                    spacers = [t for t in item.tokens if t.type == "space"]
                    hasMultipleLineBreaks = len(spacers) > 0 and any(
                        self.rules.other.anyLine.test(t.raw) for t in spacers
                    )

                    list_.loose = hasMultipleLineBreaks

            # Second pass: place task checkboxes using the final list.loose
            for item in list_.items:
                itemToken = item.tokens[0] if item.tokens else None
                if item.task and itemToken is not None and itemToken.type in ("text", "paragraph"):
                    # Remove checkbox markdown from item tokens
                    item.text = js_regexp.replace(item.text, self.rules.other.listReplaceTask, "")
                    itemToken.raw = js_regexp.replace(itemToken.raw, self.rules.other.listReplaceTask, "")
                    itemToken.text = js_regexp.replace(itemToken.text, self.rules.other.listReplaceTask, "")
                    i = len(self.lexer.inlineQueue) - 1
                    while i >= 0:
                        if self.rules.other.listIsTask.test(self.lexer.inlineQueue[i].src):
                            self.lexer.inlineQueue[i].src = js_regexp.replace(
                                self.lexer.inlineQueue[i].src, self.rules.other.listReplaceTask, ""
                            )
                            break
                        i -= 1

                    taskRaw = self.rules.other.listTaskCheckbox.exec(item.raw)
                    if taskRaw:
                        checkboxToken = Token(
                            type="checkbox",
                            raw=taskRaw[0] + " ",
                            checked=taskRaw[0] != "[ ]",
                        )
                        item.checked = checkboxToken.checked
                        if list_.loose:
                            if (
                                item.tokens[0]
                                and item.tokens[0].type in ("paragraph", "text")
                                and "tokens" in item.tokens[0]
                                # An array is truthy even when empty, and the inline tokens are not lexed yet.
                                and item.tokens[0].tokens is not None
                            ):
                                item.tokens[0].raw = checkboxToken.raw + item.tokens[0].raw
                                item.tokens[0].text = checkboxToken.raw + item.tokens[0].text
                                item.tokens[0].tokens.insert(0, checkboxToken)
                            else:
                                item.tokens.insert(
                                    0,
                                    Token(
                                        type="paragraph",
                                        raw=checkboxToken.raw,
                                        text=checkboxToken.raw,
                                        tokens=[checkboxToken],
                                    ),
                                )
                        else:
                            item.tokens.insert(0, checkboxToken)
                elif item.task:
                    item.task = False

            # Set all items to loose if list is loose
            if list_.loose:
                for item in list_.items:
                    item.loose = True
                    for token in item.tokens:
                        if token.type == "text":
                            token.type = "paragraph"

            return list_
        return None

    def html(self, src: str) -> Token | None:
        cap = self.rules.block.html.exec(src)
        if cap:
            raw = trimTrailingBlankLines(cap[0])
            token = Token(
                type="html",
                block=True,
                raw=raw,
                pre=cap[1] == "pre" or cap[1] == "script" or cap[1] == "style",
                text=raw,
            )
            return token
        return None

    def def_(self, src: str) -> Token | None:
        cap = getattr(self.rules.block, "def").exec(src)
        if cap:
            tag = js_regexp.replace(cap[1].lower(), self.rules.other.multipleSpaceGlobal, " ")
            href = (
                js_regexp.replace(
                    js_regexp.replace(cap[2], self.rules.other.hrefBrackets, "$1"),
                    self.rules.inline.anyPunctuation,
                    "$1",
                )
                if cap[2]
                else ""
            )
            title = (
                js_regexp.replace(cap[3][1 : len(cap[3]) - 1], self.rules.inline.anyPunctuation, "$1")
                if cap[3]
                else cap[3]
            )
            return Token(
                type="def",
                tag=tag,
                raw=rtrim(cap[0], "\n"),
                href=href,
                title=title,
            )
        return None

    def table(self, src: str) -> Token | None:
        cap = self.rules.block.table.exec(src)
        if not cap:
            return None

        if not self.rules.other.tableDelimiter.test(cap[2]):
            # delimiter row must have a pipe (|) or colon (:) otherwise it is a setext heading
            return None

        headers = splitCells(cap[1])
        aligns = js_regexp.replace(cap[2], self.rules.other.tableAlignChars, "").split("|")
        rows = (
            js_regexp.replace(cap[3], self.rules.other.tableRowBlankLine, "").split("\n")
            if cap[3] is not None and jsTrim(cap[3])
            else []
        )

        item = Token(
            type="table",
            raw=rtrim(cap[0], "\n"),
            header=[],
            align=[],
            rows=[],
        )

        if len(headers) != len(aligns):
            # header and align columns must be equal, rows can be different.
            return None

        for align in aligns:
            if self.rules.other.tableAlignRight.test(align):
                item.align.append("right")
            elif self.rules.other.tableAlignCenter.test(align):
                item.align.append("center")
            elif self.rules.other.tableAlignLeft.test(align):
                item.align.append("left")
            else:
                item.align.append(None)

        for i in range(len(headers)):
            item.header.append(
                Token(
                    text=headers[i],
                    tokens=self.lexer.inline(headers[i]),
                    header=True,
                    align=item.align[i],
                )
            )

        for row in rows:
            item.rows.append(
                [
                    Token(
                        text=cell,
                        tokens=self.lexer.inline(cell),
                        header=False,
                        align=item.align[i] if i < len(item.align) else None,
                    )
                    for i, cell in enumerate(splitCells(row, len(item.header)))
                ]
            )

        return item

    def lheading(self, src: str) -> Token | None:
        cap = self.rules.block.lheading.exec(src)
        if cap:
            text = jsTrim(cap[1])
            return Token(
                type="heading",
                raw=rtrim(cap[0], "\n"),
                depth=1 if cap[2][:1] == "=" else 2,
                text=text,
                tokens=self.lexer.inline(text),
            )
        return None

    def paragraph(self, src: str) -> Token | None:
        cap = self.rules.block.paragraph.exec(src)
        if cap:
            text = cap[1][:-1] if cap[1][-1:] == "\n" else cap[1]
            return Token(
                type="paragraph",
                raw=cap[0],
                text=text,
                tokens=self.lexer.inline(text),
            )
        return None

    def text(self, src: str) -> Token | None:
        cap = self.rules.block.text.exec(src)
        if cap:
            return Token(
                type="text",
                raw=cap[0],
                text=cap[0],
                tokens=self.lexer.inline(cap[0]),
            )
        return None

    def escape(self, src: str) -> Token | None:
        cap = self.rules.inline.escape.exec(src)
        if cap:
            return Token(
                type="escape",
                raw=cap[0],
                text=cap[1],
            )
        return None

    def tag(self, src: str) -> Token | None:
        cap = self.rules.inline.tag.exec(src)
        if cap:
            if not self.lexer.state.inLink and self.rules.other.startATag.test(cap[0]):
                self.lexer.state.inLink = True
            elif self.lexer.state.inLink and self.rules.other.endATag.test(cap[0]):
                self.lexer.state.inLink = False
            if not self.lexer.state.inRawBlock and self.rules.other.startPreScriptTag.test(cap[0]):
                self.lexer.state.inRawBlock = True
            elif self.lexer.state.inRawBlock and self.rules.other.endPreScriptTag.test(cap[0]):
                self.lexer.state.inRawBlock = False

            return Token(
                type="html",
                raw=cap[0],
                inLink=self.lexer.state.inLink,
                inRawBlock=self.lexer.state.inRawBlock,
                block=False,
                text=cap[0],
            )
        return None

    def link(self, src: str) -> Token | None:
        cap = self.rules.inline.link.exec(src)
        if cap:
            trimmedUrl = jsTrim(cap[2])
            if not self.options.pedantic and self.rules.other.startAngleBracket.test(trimmedUrl):
                # commonmark requires matching angle brackets
                if not (self.rules.other.endAngleBracket.test(trimmedUrl)):
                    return None

                # ending angle bracket cannot be escaped
                rtrimSlash = rtrim(trimmedUrl[:-1], "\\")
                if (len(trimmedUrl) - len(rtrimSlash)) % 2 == 0:
                    return None
            else:
                # find closing parenthesis
                lastParenIndex = findClosingBracket(cap[2], "()")
                if lastParenIndex == -2:
                    # more open parens than closed
                    return None

                if lastParenIndex > -1:
                    start = 5 if cap[0].find("!") == 0 else 4
                    linkLen = start + len(cap[1]) + lastParenIndex
                    cap[2] = cap[2][:lastParenIndex]
                    cap[0] = jsTrim(cap[0][:linkLen])
                    cap[3] = ""
            href = cap[2]
            title = ""
            if self.options.pedantic:
                # split pedantic href and title
                link = self.rules.other.pedanticHrefTitle.exec(href)

                if link:
                    href = link[1]
                    title = link[3]
            else:
                title = cap[3][1:-1] if cap[3] else ""

            href = jsTrim(href)
            if self.rules.other.startAngleBracket.test(href):
                if self.options.pedantic and not (self.rules.other.endAngleBracket.test(trimmedUrl)):
                    # pedantic allows starting angle bracket without ending angle bracket
                    href = href[1:]
                else:
                    href = href[1:-1]
            return outputLink(
                cap,
                {
                    "href": js_regexp.replace(href, self.rules.inline.anyPunctuation, "$1") if href else href,
                    "title": js_regexp.replace(title, self.rules.inline.anyPunctuation, "$1") if title else title,
                },
                cap[0],
                self.lexer,
                self.rules,
            )
        return None

    def reflink(self, src: str, links: dict[str, Any]) -> Token | None:
        cap = self.rules.inline.reflink.exec(src) or self.rules.inline.nolink.exec(src)
        if cap:
            linkString = js_regexp.replace(cap[2] or cap[1], self.rules.other.multipleSpaceGlobal, " ")
            link = links.get(linkString.lower())
            if not link:
                text = cap[0][:1]
                return Token(
                    type="text",
                    raw=text,
                    text=text,
                )
            return outputLink(cap, link, cap[0], self.lexer, self.rules)
        return None

    def emStrong(self, src: str, maskedSrc: str, prevChar: str = "") -> Token | None:
        match = self.rules.inline.emStrongLDelim.exec(src)
        if not match:
            return None
        if not match[1] and not match[2] and not match[3] and not match[4]:
            return None

        # _ can't be between two alphanumerics. \p{L}\p{N} includes non-english alphabet/numbers as well
        if match[4] and self.rules.other.unicodeAlphaNumeric.exec(prevChar):
            return None

        nextChar = match[1] or match[3] or ""

        if not nextChar or not prevChar or self.rules.inline.punctuation.exec(prevChar):
            # unicode Regex counts emoji as 1 char; spread into array for proper count (used multiple times below)
            lLength = len(match[0]) - 1
            delimTotal = lLength
            midDelimTotal = 0

            delimChar = match[0][0]
            # A mid-run opener (for example the second star of an unmatched `**`) must
            # only pair with a delimiter that can only close, otherwise it steals the
            # opener of a later span (`**a*b*c` must be `**a<em>b</em>c`).
            midRun = prevChar == delimChar
            endReg = (
                self.rules.inline.emStrongRDelimAst if delimChar == "*" else self.rules.inline.emStrongRDelimUnd
            )
            endReg.lastIndex = 0

            # Clip maskedSrc to same section of string as src (move to lexer?)
            # MISAKA fork: in UTF-16 code units, as upstream mixes `.length` (units) with code point counts.
            maskedSrc = jsSlice(maskedSrc, -1 * jsLength(src) + lLength)

            while (match := endReg.exec(maskedSrc)) is not None:
                rDelim = match[1] or match[2] or match[3] or match[4] or match[5] or match[6]

                if not rDelim:
                    continue  # skip single * in __abc*abc__

                rLength = len(rDelim)

                if match[3] or match[4]:  # found another Left Delim
                    delimTotal += rLength
                    continue
                elif match[5] or match[6]:  # either Left or Right Delim
                    if lLength % 3 and not ((lLength + rLength) % 3):
                        midDelimTotal += rLength
                        continue  # CommonMark Emphasis Rules 9-10
                    if midRun:
                        # A mid-run opener cannot close against an ambiguous delimiter that
                        # can also open; that delimiter opens its own emphasis span instead.
                        break

                delimTotal -= rLength

                if delimTotal > 0:
                    continue  # Haven't found enough closing delimiters

                # Remove extra characters. *a*** -> *a*
                rLength = min(rLength, rLength + delimTotal + midDelimTotal)
                # char length can be >1 for unicode characters;
                lastCharLength = jsLength(match[0][0])
                raw = jsSlice(src, 0, lLength + jsLength(maskedSrc[: match.index]) + lastCharLength + rLength)

                # Create `em` if smallest delimiter has odd char count. *a***
                if min(lLength, rLength) % 2:
                    text = jsSlice(raw, 1, -1)
                    return Token(
                        type="em",
                        raw=raw,
                        text=text,
                        tokens=self.lexer.inlineTokens(text),
                    )

                # Create 'strong' if smallest delimiter has even char count. **a***
                text = jsSlice(raw, 2, -2)
                return Token(
                    type="strong",
                    raw=raw,
                    text=text,
                    tokens=self.lexer.inlineTokens(text),
                )
        return None

    def codespan(self, src: str) -> Token | None:
        cap = self.rules.inline.code.exec(src)
        if cap:
            text = js_regexp.replace(cap[2], self.rules.other.newLineCharGlobal, " ")
            hasNonSpaceChars = self.rules.other.nonSpaceChar.test(text)
            hasSpaceCharsOnBothEnds = self.rules.other.startingSpaceChar.test(
                text
            ) and self.rules.other.endingSpaceChar.test(text)
            if hasNonSpaceChars and hasSpaceCharsOnBothEnds:
                text = text[1 : len(text) - 1]
            return Token(
                type="codespan",
                raw=cap[0],
                text=text,
            )
        return None

    def br(self, src: str) -> Token | None:
        cap = self.rules.inline.br.exec(src)
        if cap:
            return Token(
                type="br",
                raw=cap[0],
            )
        return None

    def del_(self, src: str, maskedSrc: str, prevChar: str = "") -> Token | None:
        match = self.rules.inline.delLDelim.exec(src)
        if not match:
            return None

        nextChar = match[1] or ""

        if not nextChar or not prevChar or self.rules.inline.punctuation.exec(prevChar):
            # unicode Regex counts emoji as 1 char; spread into array for proper count
            lLength = len(match[0]) - 1
            delimTotal = lLength

            endReg = self.rules.inline.delRDelim
            endReg.lastIndex = 0

            # Clip maskedSrc to same section of string as src
            # MISAKA fork: in UTF-16 code units (see emStrong).
            maskedSrc = jsSlice(maskedSrc, -1 * jsLength(src) + lLength)

            while (match := endReg.exec(maskedSrc)) is not None:
                rDelim = match[1] or match[2] or match[3] or match[4] or match[5] or match[6]

                if not rDelim:
                    continue

                rLength = len(rDelim)

                if rLength != lLength:
                    continue

                if match[3] or match[4]:  # found another Left Delim
                    delimTotal += rLength
                    continue

                delimTotal -= rLength

                if delimTotal > 0:
                    continue  # Haven't found enough closing delimiters

                # Remove extra characters
                rLength = min(rLength, rLength + delimTotal)
                # char length can be >1 for unicode characters
                lastCharLength = jsLength(match[0][0])
                raw = jsSlice(src, 0, lLength + jsLength(maskedSrc[: match.index]) + lastCharLength + rLength)

                # Create del token - only single ~ or double ~~ supported
                text = jsSlice(raw, lLength, -lLength)
                return Token(
                    type="del",
                    raw=raw,
                    text=text,
                    tokens=self.lexer.inlineTokens(text),
                )
        return None

    def autolink(self, src: str) -> Token | None:
        cap = self.rules.inline.autolink.exec(src)
        if cap:
            if cap[2] == "@":
                text = cap[1]
                href = "mailto:" + text
            else:
                text = cap[1]
                href = text

            return Token(
                type="link",
                raw=cap[0],
                text=text,
                href=href,
                tokens=[
                    Token(
                        type="text",
                        raw=text,
                        text=text,
                    ),
                ],
            )
        return None

    def url(self, src: str) -> Token | None:
        cap = self.rules.inline.url.exec(src)
        if cap:
            if cap[2] == "@":
                text = cap[0]
                href = "mailto:" + text
            else:
                # do extended autolink path validation
                while True:
                    prevCapZero = cap[0]
                    backpedal = self.rules.inline._backpedal.exec(cap[0])
                    cap[0] = backpedal[0] if backpedal is not None else ""
                    if prevCapZero == cap[0]:
                        break
                text = cap[0]
                if cap[1] == "www.":
                    href = "http://" + cap[0]
                else:
                    href = cap[0]
            return Token(
                type="link",
                raw=cap[0],
                text=text,
                href=href,
                tokens=[
                    Token(
                        type="text",
                        raw=text,
                        text=text,
                    ),
                ],
            )
        return None

    def inlineText(self, src: str) -> Token | None:
        cap = self.rules.inline.text.exec(src)
        if cap:
            escaped = self.lexer.state.inRawBlock
            return Token(
                type="text",
                raw=cap[0],
                text=cap[0],
                escaped=escaped,
            )
        return None


__all__ = ["_Tokenizer"]
