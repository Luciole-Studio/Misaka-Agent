"""Translated from marked's ``src/rules.ts`` (marked 18.0.11, MIT).

MISAKA fork: regex literals are ``RegExp`` objects (``misaka.utils.js_regexp``) built from the same JavaScript
sources, so ``edit()`` composes them exactly as upstream does.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from misaka.utils import js_regexp
from misaka.utils.js_regexp import RegExp


class _NoopTest:
    def exec(self, _src: str) -> None:
        return None


noopTest: Any = _NoopTest()


def cachedIndentRegex(createRegex: Callable[[int], RegExp]) -> Callable[[int], RegExp]:
    cache: dict[int, RegExp] = {}

    def get(indent: int) -> RegExp:
        cacheIndex = max(0, min(3, indent - 1))
        regex = cache.get(cacheIndex)
        if not regex:
            regex = createRegex(cacheIndex)
            cache[cacheIndex] = regex
        return regex

    return get


class _Edit:
    def __init__(self, regex: str | RegExp, opt: str) -> None:
        self._source = regex if isinstance(regex, str) else regex.source
        self._opt = opt

    def replace(self, name: str | RegExp, val: str | RegExp) -> _Edit:
        valSource = val if isinstance(val, str) else val.source
        valSource = js_regexp.replace(valSource, other.caret, "$1")
        self._source = js_regexp.replace(self._source, name, valSource)
        return self

    def getRegex(self) -> RegExp:
        return RegExp(self._source, self._opt)


def edit(regex: str | RegExp, opt: str = "") -> _Edit:
    return _Edit(regex, opt)


# use a default variable to ensure the regex is evaluated on client an not removed by minifiers
def _supportsLookbehind(a: str = "") -> bool:
    try:
        return bool(RegExp("(?<=1)(?<!1)" + a))
    except Exception:  # noqa: BLE001
        # See browser support here:
        # https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Regular_expressions/Lookbehind_assertion
        return False


supportsLookbehind = _supportsLookbehind()


class _Other:
    codeRemoveIndent = RegExp(r"^(?: {1,4}| {0,3}\t)", "gm")
    outputLinkReplace = RegExp(r"\\([\[\]])", "g")
    indentCodeCompensation = RegExp(r"^(\s+)(?:```)")
    beginningSpace = RegExp(r"^\s+")
    endingHash = RegExp(r"#$")
    startingSpaceChar = RegExp(r"^ ")
    endingSpaceChar = RegExp(r" $")
    nonSpaceChar = RegExp(r"[^ ]")
    newLineCharGlobal = RegExp(r"\n", "g")
    tabCharGlobal = RegExp(r"\t", "g")
    multipleSpaceGlobal = RegExp(r"\s+", "g")
    blankLine = RegExp(r"^[ \t]*$")
    doubleBlankLine = RegExp(r"\n[ \t]*\n[ \t]*$")
    blockquoteStart = RegExp(r"^ {0,3}>")
    blockquoteSetextReplace = RegExp(r"\n {0,3}((?:=+|-+) *)(?=\n|$)", "g")
    blockquoteSetextReplace2 = RegExp(r"^ {0,3}>[ \t]?", "gm")
    listReplaceNesting = RegExp(r"^ {1,4}(?=( {4})*[^ ])", "g")
    listIsTask = RegExp(r"^\[[ xX]\] +\S")
    listReplaceTask = RegExp(r"^\[[ xX]\] +")
    listTaskCheckbox = RegExp(r"\[[ xX]\]")
    anyLine = RegExp(r"\n.*\n")
    hrefBrackets = RegExp(r"^<(.*)>$")
    tableDelimiter = RegExp(r"[:|]")
    tableAlignChars = RegExp(r"^\||\| *$", "g")
    tableRowBlankLine = RegExp(r"\n[ \t]*$")
    tableAlignRight = RegExp(r"^ *-+: *$")
    tableAlignCenter = RegExp(r"^ *:-+: *$")
    tableAlignLeft = RegExp(r"^ *:-+ *$")
    startATag = RegExp(r"^<a ", "i")
    endATag = RegExp(r"^<\/a>", "i")
    startPreScriptTag = RegExp(r"^<(pre|code|kbd|script)(\s|>)", "i")
    endPreScriptTag = RegExp(r"^<\/(pre|code|kbd|script)(\s|>)", "i")
    startAngleBracket = RegExp(r"^<")
    endAngleBracket = RegExp(r">$")
    pedanticHrefTitle = RegExp(r"""^([^'"]*[^\s])\s+(['"])(.*)\2""")
    unicodeAlphaNumeric = RegExp(r"[\p{L}\p{N}]", "u")
    escapeTest = RegExp(r"""[&<>"']""")
    escapeReplace = RegExp(r"""[&<>"']""", "g")
    escapeTestNoEncode = RegExp(r"""[<>"']|&(?!(#\d{1,7}|#[Xx][a-fA-F0-9]{1,6}|\w+);)""")
    escapeReplaceNoEncode = RegExp(r"""[<>"']|&(?!(#\d{1,7}|#[Xx][a-fA-F0-9]{1,6}|\w+);)""", "g")
    caret = RegExp(r"(^|[^\[])\^", "g")
    percentDecode = RegExp(r"%25", "g")
    findPipe = RegExp(r"\|", "g")
    splitPipe = RegExp(r" \|")
    slashPipe = RegExp(r"\\\|", "g")
    carriageReturn = RegExp(r"\r\n|\r", "g")
    spaceLine = RegExp(r"^ +$", "gm")
    notSpaceStart = RegExp(r"^\S*")
    endingNewline = RegExp(r"\n$")

    @staticmethod
    def listItemRegex(bull: str) -> RegExp:
        return RegExp(f"^( {{0,3}}{bull})((?:[\t ][^\\n]*)?(?:\\n|$))")

    nextBulletRegex = staticmethod(
        cachedIndentRegex(lambda indent: RegExp(f"^ {{0,{indent}}}(?:[*+-]|\\d{{1,9}}[.)])((?:[ \t][^\\n]*)?(?:\\n|$))"))
    )
    hrRegex = staticmethod(
        cachedIndentRegex(lambda indent: RegExp(f"^ {{0,{indent}}}((?:- *){{3,}}|(?:_ *){{3,}}|(?:\\* *){{3,}})(?:\\n+|$)"))
    )
    fencesBeginRegex = staticmethod(cachedIndentRegex(lambda indent: RegExp(f"^ {{0,{indent}}}(?:```|~~~)")))
    headingBeginRegex = staticmethod(cachedIndentRegex(lambda indent: RegExp(f"^ {{0,{indent}}}#")))
    htmlBeginRegex = staticmethod(cachedIndentRegex(lambda indent: RegExp(f"^ {{0,{indent}}}<(?:[a-z].*>|!--)", "i")))
    blockquoteBeginRegex = staticmethod(cachedIndentRegex(lambda indent: RegExp(f"^ {{0,{indent}}}>")))


other = _Other()

# Block-Level Grammar

newline = RegExp(r"^(?:[ \t]*(?:\n|$))+")
blockCode = RegExp(r"^((?: {4}| {0,3}\t)[^\n]+(?:\n(?:[ \t]*(?:\n|$))*)?)+")
fences = RegExp(r"^ {0,3}(`{3,}(?=[^`\n]*(?:\n|$))|~{3,})([^\n]*)(?:\n|$)(?:|([\s\S]*?)(?:\n|$))(?: {0,3}\1[~`]* *(?=\n|$)|$)")
hr = RegExp(r"^ {0,3}((?:-[\t ]*){3,}|(?:_[ \t]*){3,}|(?:\*[ \t]*){3,})(?:\n+|$)")
heading = RegExp(r"^ {0,3}(#{1,6})(?=\s|$)(.*)(?:\n+|$)")
bullet = RegExp(r" {0,3}(?:[*+-]|\d{1,9}[.)])")
lheadingCore = RegExp(r"^(?!bull |blockCode|fences|blockquote|heading|html|table)((?:.|\n(?!\s*?\n|bull |blockCode|fences|blockquote|heading|html|table))+?)\n {0,3}(=+|-+) *(?:\n+|$)")
lheading = (
    edit(lheadingCore)
    .replace(RegExp("bull", "g"), bullet)  # lists can interrupt
    .replace(RegExp("blockCode", "g"), RegExp(r"(?: {4}| {0,3}\t)"))  # indented code blocks can interrupt
    .replace(RegExp("fences", "g"), RegExp(r" {0,3}(?:`{3,}|~{3,})"))  # fenced code blocks can interrupt
    .replace(RegExp("blockquote", "g"), RegExp(r" {0,3}>"))  # blockquote can interrupt
    .replace(RegExp("heading", "g"), RegExp(r" {0,3}#{1,6}(?:\s|$)"))  # ATX heading can interrupt
    .replace(RegExp("html", "g"), RegExp(r" {0,3}<[^\n>]+>\n"))  # block html can interrupt
    .replace(RegExp(r"\|table", "g"), "")  # table not in commonmark
    .getRegex()
)
lheadingGfm = (
    edit(lheadingCore)
    .replace(RegExp("bull", "g"), bullet)  # lists can interrupt
    .replace(RegExp("blockCode", "g"), RegExp(r"(?: {4}| {0,3}\t)"))  # indented code blocks can interrupt
    .replace(RegExp("fences", "g"), RegExp(r" {0,3}(?:`{3,}|~{3,})"))  # fenced code blocks can interrupt
    .replace(RegExp("blockquote", "g"), RegExp(r" {0,3}>"))  # blockquote can interrupt
    .replace(RegExp("heading", "g"), RegExp(r" {0,3}#{1,6}(?:\s|$)"))  # ATX heading can interrupt
    .replace(RegExp("html", "g"), RegExp(r" {0,3}<[^\n>]+>\n"))  # block html can interrupt
    .replace(RegExp("table", "g"), RegExp(r" {0,3}\|?(?:[:\- ]*\|)+[\:\- ]*\n"))  # table can interrupt
    .getRegex()
)
_paragraph = RegExp(r"^([^\n]+(?:\n(?!hr|heading|lheading|blockquote|fences|list|html|table|[ \t]+\n)[^\n]+)*)")
blockText = RegExp(r"^[^\n]+")
_blockLabel = RegExp(r"(?!\s*\])(?:\\[\s\S]|[^\[\]\\])+")
def_ = (
    edit(RegExp(r"^ {0,3}\[(label)\]: *(?:\n[ \t]*)?([^<\s][^\s]*|<.*?>)(?:(?: +(?:\n[ \t]*)?| *\n[ \t]*)(title))? *(?:\n+|$)"))
    .replace("label", _blockLabel)
    .replace("title", RegExp(r"""(?:"(?:\\"?|[^"\\])*"|'[^'\n]*(?:\n[^'\n]+)*\n?'|\([^()]*\))"""))
    .getRegex()
)

list_ = edit(RegExp(r"^(bull)([ \t][^\n]*?)?(?:\n|$)")).replace(RegExp("bull", "g"), bullet).getRegex()

_tag = (
    "address|article|aside|base|basefont|blockquote|body|caption"
    "|center|col|colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|figcaption"
    "|figure|footer|form|frame|frameset|h[1-6]|head|header|hr|html|iframe"
    "|legend|li|link|main|menu|menuitem|meta|nav|noframes|ol|optgroup|option"
    "|p|param|search|section|summary|table|tbody|td|tfoot|th|thead|title"
    "|tr|track|ul"
)
_comment = RegExp(r"<!--(?:-?>|[\s\S]*?(?:-->|$))")
html = (
    edit(
        "^ {0,3}(?:"  # optional indentation
        "<(script|pre|style|textarea)[\\s>][\\s\\S]*?(?:</\\1>[^\\n]*\\n*|$)"  # (1)
        "|comment[^\\n]*(\\n+|$)"  # (2)
        "|<\\?[\\s\\S]*?(?:\\?>[^\\n]*\\n*|$)"  # (3)
        "|<![A-Z][\\s\\S]*?(?:>[^\\n]*\\n*|$)"  # (4)
        "|<!\\[CDATA\\[[\\s\\S]*?(?:\\]\\]>[^\\n]*\\n*|$)"  # (5)
        "|</?(tag)(?: +|\\n|/?>)[\\s\\S]*?(?:(?:\\n[ \t]*)+\\n|$)"  # (6)
        "|<(?!script|pre|style|textarea)([a-z][\\w-]*)(?:attribute)*? */?>(?=[ \\t]*(?:\\n|$))[\\s\\S]*?(?:(?:\\n[ \t]*)+\\n|$)"  # (7) open tag
        "|</(?!script|pre|style|textarea)[a-z][\\w-]*\\s*>(?=[ \\t]*(?:\\n|$))[\\s\\S]*?(?:(?:\\n[ \t]*)+\\n|$)"  # (7) closing tag
        ")",
        "i",
    )
    .replace("comment", _comment)
    .replace("tag", _tag)
    .replace("attribute", RegExp(r""" +[a-zA-Z:_][\w.:-]*(?: *= *"[^"\n]*"| *= *'[^'\n]*'| *= *[^\s"'=<>`]+)?"""))
    .getRegex()
)


def createParagraph(listInterrupt: RegExp) -> RegExp:
    return (
        edit(_paragraph)
        .replace("hr", hr)
        .replace("heading", " {0,3}#{1,6}(?:\\s|$)")
        .replace("|lheading", "")  # setext headings don't interrupt commonmark paragraphs
        .replace("|table", "")
        .replace("blockquote", " {0,3}>")
        .replace("fences", " {0,3}(?:`{3,}(?=[^`\\n]*(?:\\n|$))|~~~)[^\\n]*(?:\\n|$)")
        .replace("list", listInterrupt)
        .replace("html", "</?(?:tag)(?: +|\\n|/?>)|<(?:script|pre|style|textarea|!--)")
        .replace("tag", _tag)  # pars can be interrupted by type (6) html blocks
        .getRegex()
    )


# only non-empty lists starting from 1 can interrupt paragraphs
paragraph = createParagraph(RegExp(r" {0,3}(?:[*+-]|1[.)])[ \t]+[^ \t\n]"))
# inside a blockquote a bare list marker (any number) starts a sibling list,
# so it must not be lazily continued as paragraph text (unlike a top level
# paragraph, where an empty list cannot interrupt)
blockquoteParagraph = createParagraph(RegExp(r" {0,3}(?:[*+-]|\d{1,9}[.)])(?:[ \t]|\n|$)"))

blockquote = edit(RegExp(r"^( {0,3}> ?(paragraph|[^\n]*)(?:\n|$))+")).replace("paragraph", blockquoteParagraph).getRegex()

# Normal Block Grammar

blockNormal: dict[str, Any] = {
    "blockquote": blockquote,
    "code": blockCode,
    "def": def_,
    "fences": fences,
    "heading": heading,
    "hr": hr,
    "html": html,
    "lheading": lheading,
    "list": list_,
    "newline": newline,
    "paragraph": paragraph,
    "table": noopTest,
    "text": blockText,
}

# GFM Block Grammar

gfmTable = (
    edit(
        "^ *([^\\n ].*)\\n"  # Header
        " {0,3}((?:\\| *)?:?-+:? *(?:\\| *:?-+:? *)*(?:\\| *)?)"  # Align
        "(?:\\n((?:(?! *\\n|hr|heading|blockquote|code|fences|list|html).*(?:\\n|$))*)\\n*|$)"  # Cells
    )
    .replace("hr", hr)
    .replace("heading", " {0,3}#{1,6}(?:\\s|$)")
    .replace("blockquote", " {0,3}>")
    .replace("code", "(?: {4}| {0,3}\t)[^\\n]")
    .replace("fences", " {0,3}(?:`{3,}(?=[^`\\n]*(?:\\n|$))|~~~)[^\\n]*(?:\\n|$)")
    .replace("list", " {0,3}(?:[*+-]|1[.)])[ \\t]")  # any bullet ends the table rows
    .replace("html", "</?(?:tag)(?: +|\\n|/?>)|<(?:script|pre|style|textarea|!--)")
    .replace("tag", _tag)  # tables can be interrupted by type (6) html blocks
    .getRegex()
)

blockGfm: dict[str, Any] = {
    **blockNormal,
    "lheading": lheadingGfm,
    "table": gfmTable,
    "paragraph": edit(_paragraph)
    .replace("hr", hr)
    .replace("heading", " {0,3}#{1,6}(?:\\s|$)")
    .replace("|lheading", "")  # setext headings don't interrupt commonmark paragraphs
    .replace("table", gfmTable)  # interrupt paragraphs with table
    .replace("blockquote", " {0,3}>")
    .replace("fences", " {0,3}(?:`{3,}(?=[^`\\n]*(?:\\n|$))|~~~)[^\\n]*(?:\\n|$)")
    .replace("list", " {0,3}(?:[*+-]|1[.)])[ \\t]+[^ \\t\\n]")  # only non-empty lists starting from 1 can interrupt
    .replace("html", "</?(?:tag)(?: +|\\n|/?>)|<(?:script|pre|style|textarea|!--)")
    .replace("tag", _tag)  # pars can be interrupted by type (6) html blocks
    .getRegex(),
}

# Pedantic grammar (original John Gruber's loose markdown specification)

blockPedantic: dict[str, Any] = {
    **blockNormal,
    "html": edit(
        "^ *(?:comment *(?:\\n|\\s*$)"
        "|<(tag)[\\s\\S]+?</\\1> *(?:\\n{2,}|\\s*$)"  # closed tag
        "|<tag(?:\"[^\"]*\"|'[^']*'|\\s[^'\"/>\\s]*)*?/?> *(?:\\n{2,}|\\s*$))"
    )
    .replace("comment", _comment)
    .replace(
        RegExp("tag", "g"),
        "(?!(?:"
        "a|em|strong|small|s|cite|q|dfn|abbr|data|time|code|var|samp|kbd|sub"
        "|sup|i|b|u|mark|ruby|rt|rp|bdi|bdo|span|br|wbr|ins|del|img)"
        "\\b)\\w+(?!:|[^\\w\\s@]*@)\\b",
    )
    .getRegex(),
    "def": RegExp(r"""^ *\[([^\]]+)\]: *<?([^\s>]+)>?(?: +(["(][^\n]+[")]))? *(?:\n+|$)"""),
    "heading": RegExp(r"^(#{1,6})(.*)(?:\n+|$)"),
    "fences": noopTest,  # fences not supported
    "lheading": RegExp(r"^(.+?)\n {0,3}(=+|-+) *(?:\n+|$)"),
    "paragraph": edit(_paragraph)
    .replace("hr", hr)
    .replace("heading", " *#{1,6} *[^\n]")
    .replace("lheading", lheading)
    .replace("|table", "")
    .replace("blockquote", " {0,3}>")
    .replace("|fences", "")
    .replace("|list", "")
    .replace("|html", "")
    .replace("|tag", "")
    .getRegex(),
}

# Inline-Level Grammar

escape = RegExp(r"""^\\([!"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~])""")
inlineCode = RegExp(r"^(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)")
br = RegExp(r"^( {2,}|\\)\n(?!\s*$)")
inlineText = RegExp(r"^(`+|[^`])(?:(?= {2,}\n)|[\s\S]*?(?:(?=[\\<!\[`*_]|\b_|$)|[^ ](?= {2,}\n)))")

# list of unicode punctuation marks, plus any missing characters from CommonMark spec
_punctuation = RegExp(r"[\p{P}\p{S}]", "u")
_punctuationOrSpace = RegExp(r"[\s\p{P}\p{S}]", "u")
_notPunctuationOrSpace = RegExp(r"[^\s\p{P}\p{S}]", "u")
punctuation = edit(RegExp(r"^((?![*_])punctSpace)"), "u").replace(RegExp("punctSpace", "g"), _punctuationOrSpace).getRegex()

# Opening punct/quotes excluded from pedantic LDelim nextChar so
# `aa**"A"**bb` / `aa**(A)**bb` can start strong (Pi + Ps + ASCII straight quotes).
_openQuote = RegExp(r"""[\p{Pi}\p{Ps}"']""", "u")

# GFM allows ~ inside strong and em for strikethrough
_punctuationGfmStrongEm = RegExp(r"(?!~)[\p{P}\p{S}]", "u")
_punctuationOrSpaceGfmStrongEm = RegExp(r"(?!~)[\s\p{P}\p{S}]", "u")
_notPunctuationOrSpaceGfmStrongEm = RegExp(r"(?:[^\s\p{P}\p{S}]|~)", "u")

# sequences em should skip over [title](link), `code`, <html>
blockSkip = (
    edit(RegExp("link|precode-code|html"), "g")
    .replace("link", RegExp(r"\[(?:[^\[\]`]|(?<a>`+)[^`]+\k<a>(?!`))*?\]\((?:\\[\s\S]|[^\\\(\)]|\((?:\\[\s\S]|[^\\\(\)])*\))*\)"))
    .replace("precode-", "(?<!`)()" if supportsLookbehind else "(^^|[^`])")
    .replace("code", RegExp(r"(?<b>`+)[^`]+\k<b>(?!`)"))
    .replace("html", RegExp(r"<(?! )[^<>]*?>"))
    .getRegex()
)

emStrongLDelimCore = RegExp(r"^(?:\*+(?:((?!\*)punct)|([^\s*]))?)|^_+(?:((?!_)punct)|([^\s_]))?")

emStrongLDelim = edit(emStrongLDelimCore, "u").replace(RegExp("punct", "g"), _punctuation).getRegex()

emStrongLDelimGfm = edit(emStrongLDelimCore, "u").replace(RegExp("punct", "g"), _punctuationGfmStrongEm).getRegex()

# Pedantic: do not treat opening punct/quotes as left-delim punct (e.g. **( / **").
emStrongLDelimPedanticCore = RegExp(r"^(?:\*+(?:((?!\*)(?!openQuote)punct)|([^\s*]))?)|^_+(?:((?!_)(?!openQuote)punct)|([^\s_]))?")

emStrongLDelimPedantic = (
    edit(emStrongLDelimPedanticCore, "u")
    .replace(RegExp("openQuote", "g"), _openQuote)
    .replace(RegExp("punct", "g"), _punctuation)
    .getRegex()
)

emStrongRDelimAstCore = (
    "^[^_*]*?__[^_*]*?\\*[^_*]*?(?=__)"  # Skip orphan inside strong
    "|[^*]+(?=[^*])"  # Consume to delim
    "|(?!\\*)punct(\\*+)(?=[\\s]|$)"  # (1) #*** can only be a Right Delimiter
    "|notPunctSpace(\\*+)(?!\\*)(?=punctSpace|$)"  # (2) a***#, a*** can only be a Right Delimiter
    "|(?!\\*)punctSpace(\\*+)(?=notPunctSpace)"  # (3) #***a, ***a can only be Left Delimiter
    "|[\\s](\\*+)(?!\\*)(?=punct)"  # (4) ***# can only be Left Delimiter
    "|(?!\\*)punct(\\*+)(?!\\*)(?=punct)"  # (5) #***# can be either Left or Right Delimiter
    "|notPunctSpace(\\*+)(?=notPunctSpace)"  # (6) a***a can be either Left or Right Delimiter
)

emStrongRDelimAst = (
    edit(emStrongRDelimAstCore, "gu")
    .replace(RegExp("notPunctSpace", "g"), _notPunctuationOrSpace)
    .replace(RegExp("punctSpace", "g"), _punctuationOrSpace)
    .replace(RegExp("punct", "g"), _punctuation)
    .getRegex()
)

emStrongRDelimAstGfm = (
    edit(emStrongRDelimAstCore, "gu")
    .replace(RegExp("notPunctSpace", "g"), _notPunctuationOrSpaceGfmStrongEm)
    .replace(RegExp("punctSpace", "g"), _punctuationOrSpaceGfmStrongEm)
    .replace(RegExp("punct", "g"), _punctuationGfmStrongEm)
    .getRegex()
)

# Pedantic: rule 3 requires whitespace before left delim; rule 6 allows punct before close (e.g. `:**bar`).
emStrongRDelimAstPedanticCore = (
    "^[^_*]*?__[^_*]*?\\*[^_*]*?(?=__)"  # Skip orphan inside strong
    "|[^*]+(?=[^*])"  # Consume to delim
    "|(?!\\*)punct(\\*+)(?=[\\s]|$)"  # (1) #*** can only be a Right Delimiter
    "|notPunctSpace(\\*+)(?!\\*)(?=punctSpace|$)"  # (2) a***#, a*** can only be a Right Delimiter
    "|(?!\\*)[\\s](\\*+)(?=notPunctSpace)"  # (3) ***a can only be Left Delimiter (whitespace required; not #***a)
    "|[\\s](\\*+)(?!\\*)(?=punct)"  # (4) ***# can only be Left Delimiter
    "|(?!\\*)punct(\\*+)(?!\\*)(?=punct)"  # (5) #***# can be either Left or Right Delimiter
    "|(?:(?!\\*)punct|notPunctSpace)(\\*+)(?!\\*)(?=notPunctSpace)"  # (6) a***a, #***a, and :** (e.g. **foo:**bar)
)

emStrongRDelimAstPedantic = (
    edit(emStrongRDelimAstPedanticCore, "gu")
    .replace(RegExp("notPunctSpace", "g"), _notPunctuationOrSpace)
    .replace(RegExp("punctSpace", "g"), _punctuationOrSpace)
    .replace(RegExp("punct", "g"), _punctuation)
    .getRegex()
)

# Normal: no rule 6 for _ (a___a cannot close as emphasis).
emStrongRDelimUnd = (
    edit(
        "^[^_*]*?\\*\\*[^_*]*?_[^_*]*?(?=\\*\\*)"  # Skip orphan inside strong
        "|[^_]+(?=[^_])"  # Consume to delim
        "|(?!_)punct(_+)(?=[\\s]|$)"  # (1) #___ can only be a Right Delimiter
        "|notPunctSpace(_+)(?!_)(?=punctSpace|$)"  # (2) a___#, a___ can only be a Right Delimiter
        "|(?!_)punctSpace(_+)(?=notPunctSpace)"  # (3) #___a, ___a can only be Left Delimiter
        "|[\\s](_+)(?!_)(?=punct)"  # (4) ___# can only be Left Delimiter
        "|(?!_)punct(_+)(?!_)(?=punct)",  # (5) #___# can be either Left or Right Delimiter
        "gu",
    )
    .replace(RegExp("notPunctSpace", "g"), _notPunctuationOrSpace)
    .replace(RegExp("punctSpace", "g"), _punctuationOrSpace)
    .replace(RegExp("punct", "g"), _punctuation)
    .getRegex()
)

# Pedantic: same rule 3/6 adjustments as emStrongRDelimAstPedantic (e.g. `_foo:_bar`).
emStrongRDelimUndPedanticCore = (
    "^[^_*]*?\\*\\*[^_*]*?_[^_*]*?(?=\\*\\*)"  # Skip orphan inside strong
    "|[^_]+(?=[^_])"  # Consume to delim
    "|(?!_)punct(_+)(?=[\\s]|$)"  # (1) #___ can only be a Right Delimiter
    "|notPunctSpace(_+)(?!_)(?=punctSpace|$)"  # (2) a___#, a___ can only be a Right Delimiter
    "|(?!_)[\\s](_+)(?=notPunctSpace)"  # (3) ___a can only be Left Delimiter (whitespace required; not #___a)
    "|[\\s](_+)(?!_)(?=punct)"  # (4) ___# can only be Left Delimiter
    "|(?!_)punct(_+)(?!_)(?=punct)"  # (5) #___# can be either Left or Right Delimiter
    "|(?:(?!_)punct|notPunctSpace)(_+)(?!_)(?=notPunctSpace)"  # (6) a___a, #___a, and :__ (e.g. _foo:_bar)
)

emStrongRDelimUndPedantic = (
    edit(emStrongRDelimUndPedanticCore, "gu")
    .replace(RegExp("notPunctSpace", "g"), _notPunctuationOrSpace)
    .replace(RegExp("punctSpace", "g"), _punctuationOrSpace)
    .replace(RegExp("punct", "g"), _punctuation)
    .getRegex()
)

# Tilde left delimiter for strikethrough (similar to emStrongLDelim for asterisk)
delLDelim = edit(RegExp(r"^~~?(?:((?!~)punct)|[^\s~])"), "u").replace(RegExp("punct", "g"), _punctuation).getRegex()

# Tilde delimiter patterns for strikethrough (similar to asterisk)
delRDelimCore = (
    "^[^~]+(?=[^~])"  # Consume to delim
    "|(?!~)punct(~~?)(?=[\\s]|$)"  # (1) #~~ can only be a Right Delimiter
    "|notPunctSpace(~~?)(?!~)(?=punctSpace|$)"  # (2) a~~#, a~~ can only be a Right Delimiter
    "|(?!~)punctSpace(~~?)(?=notPunctSpace)"  # (3) #~~a, ~~a can only be Left Delimiter
    "|[\\s](~~?)(?!~)(?=punct)"  # (4) ~~# can only be Left Delimiter
    "|(?!~)punct(~~?)(?!~)(?=punct)"  # (5) #~~# can be either Left or Right Delimiter
    "|notPunctSpace(~~?)(?=notPunctSpace)"  # (6) a~~a can be either Left or Right Delimiter
)

delRDelim = (
    edit(delRDelimCore, "gu")
    .replace(RegExp("notPunctSpace", "g"), _notPunctuationOrSpace)
    .replace(RegExp("punctSpace", "g"), _punctuationOrSpace)
    .replace(RegExp("punct", "g"), _punctuation)
    .getRegex()
)

anyPunctuation = edit(RegExp(r"\\(punct)"), "gu").replace(RegExp("punct", "g"), _punctuation).getRegex()

autolink = (
    edit(RegExp(r"^<(scheme:[^\s\x00-\x1f<>]*|email)>"))
    .replace("scheme", RegExp(r"[a-zA-Z][a-zA-Z0-9+.-]{1,31}"))
    .replace("email", RegExp(r"[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+(@)[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+(?![-_])"))
    .getRegex()
)

_inlineComment = edit(_comment).replace("(?:-->|$)", "-->").getRegex()
tag = (
    edit(
        "^comment"
        "|^</[a-zA-Z][\\w:-]*\\s*>"  # self-closing tag
        "|^<[a-zA-Z][\\w-]*(?:attribute)*?\\s*/?>"  # open tag
        "|^<\\?[\\s\\S]*?\\?>"  # processing instruction, e.g. <?php ?>
        "|^<![a-zA-Z]+\\s[\\s\\S]*?>"  # declaration, e.g. <!DOCTYPE html>
        "|^<!\\[CDATA\\[[\\s\\S]*?\\]\\]>"  # CDATA section
    )
    .replace("comment", _inlineComment)
    .replace("attribute", RegExp(r"""\s+[a-zA-Z:_][\w.:-]*(?:\s*=\s*"[^"]*"|\s*=\s*'[^']*'|\s*=\s*[^\s"'=<>`]+)?"""))
    .getRegex()
)

_inlineLabel = RegExp(r"(?:\[(?:\\[\s\S]|[^\[\]\\])*\]|\\[\s\S]|`+(?!`)[^`]*?`+(?!`)|``+(?=\])|[^\[\]\\`])*?")

link = (
    edit(RegExp(r"^!?\[(label)\]\(\s*(href)(?:(?:[ \t]+(?:\n[ \t]*)?|\n[ \t]*)(title))?\s*\)"))
    .replace("label", _inlineLabel)
    .replace("href", RegExp(r"<(?:\\.|[^\n<>\\])+>|[^ \t\n\x00-\x1f]+|(?=\))"))
    .replace("title", RegExp('"' + r"""(?:\\"?|[^"\\])*"|'(?:\\'?|[^'\\])*'|\((?:\\\)?|[^)\\])*\)"""))
    .getRegex()
)

reflink = edit(RegExp(r"^!?\[(label)\]\[(ref)\]")).replace("label", _inlineLabel).replace("ref", _blockLabel).getRegex()

nolink = edit(RegExp(r"^!?\[(ref)\](?:\[\])?")).replace("ref", _blockLabel).getRegex()

reflinkSearch = edit("reflink|nolink(?!\\()", "g").replace("reflink", reflink).replace("nolink", nolink).getRegex()

_caseInsensitiveProtocol = RegExp(r"[hH][tT][tT][pP][sS]?|[fF][tT][pP]")

# Normal Inline Grammar

inlineNormal: dict[str, Any] = {
    "_backpedal": noopTest,  # only used for GFM url
    "anyPunctuation": anyPunctuation,
    "autolink": autolink,
    "blockSkip": blockSkip,
    "br": br,
    "code": inlineCode,
    "del": noopTest,
    "delLDelim": noopTest,
    "delRDelim": noopTest,
    "emStrongLDelim": emStrongLDelim,
    "emStrongRDelimAst": emStrongRDelimAst,
    "emStrongRDelimUnd": emStrongRDelimUnd,
    "escape": escape,
    "link": link,
    "nolink": nolink,
    "punctuation": punctuation,
    "reflink": reflink,
    "reflinkSearch": reflinkSearch,
    "tag": tag,
    "text": inlineText,
    "url": noopTest,
}

# Pedantic Inline Grammar

inlinePedantic: dict[str, Any] = {
    **inlineNormal,
    "emStrongLDelim": emStrongLDelimPedantic,
    "emStrongRDelimAst": emStrongRDelimAstPedantic,
    "emStrongRDelimUnd": emStrongRDelimUndPedantic,
    "link": edit(RegExp(r"^!?\[(label)\]\((.*?)\)")).replace("label", _inlineLabel).getRegex(),
    "reflink": edit(RegExp(r"^!?\[(label)\]\s*\[([^\]]*)\]")).replace("label", _inlineLabel).getRegex(),
}

# GFM Inline Grammar

inlineGfm: dict[str, Any] = {
    **inlineNormal,
    "emStrongRDelimAst": emStrongRDelimAstGfm,
    "emStrongLDelim": emStrongLDelimGfm,
    "delLDelim": delLDelim,
    "delRDelim": delRDelim,
    "url": edit(RegExp(r"^((?:protocol):\/\/|www\.)(?:[a-zA-Z0-9\-]+\.?)+[^\s<]*|^email"))
    .replace("protocol", _caseInsensitiveProtocol)
    .replace("email", RegExp(r"[A-Za-z0-9._+-]+(@)[a-zA-Z0-9-_]+(?:\.[a-zA-Z0-9-_]*[a-zA-Z0-9])+(?![-_])"))
    .getRegex(),
    "_backpedal": RegExp(r"""(?:[^?!.,:;*_'"~()&]+|\([^)]*\)|&(?![a-zA-Z0-9]+;$)|[?!.,:;*_'"~)]+(?!$))+"""),
    "del": RegExp(r"^(~~?)(?=[^\s~])((?:\\[\s\S]|[^\\])*?(?:\\[\s\S]|[^\s~\\]))\1(?=[^~]|$)"),
    "text": edit(RegExp(r"""^(`+|~+|[^`~])(?:(?=[`~])|(?= {2,}\n)|(?=[a-zA-Z0-9.!#$%&'*+\/=?_`{\|}~-]+@)|[\s\S]*?(?:(?=[\\<!\[`*~_]|\b_|protocol:\/\/|www\.|$)|[^ ](?= {2,}\n)|[^a-zA-Z0-9.!#$%&'*+\/=?_`{\|}~-](?=[a-zA-Z0-9.!#$%&'*+\/=?_`{\|}~-]+@)))"""))
    .replace("protocol", _caseInsensitiveProtocol)
    .getRegex(),
}

# GFM + Line Breaks Inline Grammar

inlineBreaks: dict[str, Any] = {
    **inlineGfm,
    "br": edit(br).replace("{2,}", "*").getRegex(),
    "text": edit(inlineGfm["text"]).replace("\\b_", "\\b_| {2,}\\n").replace(RegExp(r"\{2,\}", "g"), "*").getRegex(),
}

# exports

block: dict[str, dict[str, Any]] = {
    "normal": blockNormal,
    "gfm": blockGfm,
    "pedantic": blockPedantic,
}

inline: dict[str, dict[str, Any]] = {
    "normal": inlineNormal,
    "gfm": inlineGfm,
    "breaks": inlineBreaks,
    "pedantic": inlinePedantic,
}


class Rules:
    """``{ other, block, inline }``: the rule set a lexer hands its tokenizer."""

    def __init__(self, other: _Other, block: dict[str, Any], inline: dict[str, Any]) -> None:
        self.other = other
        self.block = _RuleTable(block)
        self.inline = _RuleTable(inline)


class _RuleTable:
    """MISAKA fork: a rule table read as attributes (``rules.block.newline``), as upstream's objects are."""

    def __init__(self, rules: dict[str, Any]) -> None:
        self.__dict__.update(rules)

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)


__all__ = ["Rules", "block", "edit", "inline", "other"]
