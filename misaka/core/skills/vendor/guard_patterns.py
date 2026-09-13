# Hermes f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140 / tools/skills_guard.py; see PROVENANCE.json and LICENSE.

MODIFY_VERB_RE = (
    r'(?:\bwrit(?:e|es|ing)\b|\bwritten\b|\bedit(?:s|ed|ing)?\b'
    r'|\bmodif(?:y|ies|ied|ying|ication)s?\b|\bupdat(?:e|es|ed|ing)\b'
    r'|\bappend(?:s|ed|ing)?\b|\bprepend(?:s|ed|ing)?\b'
    r'|\binject(?:s|ed|ing)?\b|\boverwrit(?:e|es|ing)\b|\boverwritten\b'
    r'|\breplac(?:e|es|ed|ing)\b|\balter(?:s|ed|ing)?\b|\badd(?:s|ed|ing)\b)')


_AGENT_CONFIG_FILES = r'(?:AGENTS\.md|CLAUDE\.md|\.cursorrules|\.clinerules)'


def _shell_write_re(file_alt: str) -> str:
    """Mechanical shell write into *file_alt*: ``>``/``>>``, ``sed -i``, ``tee`` (target as immediate argument, so
    ``| tee output | AGENTS.md |`` cells miss), ``cp``/``mv`` with the file as destination (source arg required, so
    ``cp AGENTS.md backup/`` misses; ``AGENTS.md.bak`` is not the file). A single ``>`` needs a preceding word/quote/
    paren char so blockquotes (``> text``) and arrows (``-> file``) miss."""
    return (
        rf'(?:>>|[\w"\'`)\]]\s*>)\s*[~\w./-]*{file_alt}(?!\.?\w)'
        rf'|\bsed\b[^\n]*\s(?:-[A-Za-z]*i[A-Za-z]*|--in-place)\b[^\n]*{file_alt}(?!\.?\w)'
        rf'|\btee\s+(?:-a\s+)?[~\w./"\'-]*{file_alt}(?!\.?\w)'
        rf'|\b(?:cp|mv)\s+[^\s|;&]+\s+[^\n|;&]{{0,40}}?{file_alt}(?!\.?\w)')


def _prose_modify_re(file_alt: str) -> str:
    """Prose instructing modification of *file_alt*: an imperative-position verb (line start / bullet), or a mid-line
    verb with a directive marker ("you must", "please", "make sure to"). Descriptive prose ("skills that edit
    AGENTS.md") misses; the verb→file gap forbids commas so enumerations ("Write skills, AGENTS.md, CLAUDE.md") miss."""
    return (
        rf'^\s*(?:[-*+]\s+|\d+[.)]\s+)?{MODIFY_VERB_RE}[^\n,]{{0,80}}?{file_alt}\b'
        rf'|(?:\byou\s+(?:must|should|need\s+to)\s+|\bplease\s+'
        rf'|\bmake\s+sure\s+(?:to\s+|you\s+)|\bbe\s+sure\s+to\s+)'
        rf'{MODIFY_VERB_RE}[^\n,]{{0,80}}?{file_alt}\b')


def _content_contract_re(file_alt: str) -> str:
    """"<file> should contain/include ..." prose. Authoring guides and attacks share this shape and are not
    separable statically, so the tier is scored high (caution → confirmation), never critical."""
    return rf'{file_alt}\b[^\n]{{0,40}}?\b(?:should|must|needs?\s+to)\s+(?:contain|say|include|have|list)\b'

