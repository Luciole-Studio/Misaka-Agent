"""Selected CCB legacy command splitter, solely for Bash auto-background policy.

CCB pin 77a7934e15d69da13879112ed7db695c9ee7a52a:
utils/bash/{commands,heredoc}.ts (default extractHeredocs path).
The lexer ports shell-quote 1.8.3 parse.js with its fixed preserving-env callback
and default escape option; see LICENSE.shell-quote. Not a shell executor or a
replacement for MISAKA's permission/path validation. Quoted-only extraction,
argument quoting and arbitrary environment-object expansion are not called here.
"""
from __future__ import annotations

import re
import secrets
import struct

from misaka.core.prompt_templates import _ECMASCRIPT_WHITESPACE

_WS = ''.join(_ECMASCRIPT_WHITESPACE)
_SPACE = '[' + re.escape(_WS) + ']'
_CONTROL = r'(?:\|\||\&\&|;;|\|\&|\<\(|\<\<\<|>>|>\&|<\&|[&;()|<>])'
_META = '|&;()<> \t'
_BARE = r'''(\\['"''' + _META + r''']|[^''' + re.escape(_WS) + r''' '"|&;()<>])+'''
_CHUNKER = re.compile('(' + _CONTROL + ')|(' + _BARE + r'''|"((\\"|[^"])*?)"|'((\\'|[^'])*?)')+''')
_HEREDOC = re.compile(r'''(?<!<)<<(?!<)(-)?[ \t]*(?:(['"])(\\?\w+)\2|\\?(\w+))''', re.ASCII)
_OPERATORS = {'&&', '||', ';', ';;', '|', '>&', '>', '>>'}
_FDS = {'0', '1', '2'}


def _extract_heredocs(command):
    """Direct port of extractHeredocs with quotedOnly omitted (always false)."""
    if '<<' not in command or re.search(r'''\$['"]''', command):
        return command, {}
    before = command[:command.index('<<')]
    if '`' in before or before.count('((') > before.count('))'):
        return command, {}
    found = []
    pos = 0
    single = double = comment = dq_escape = False
    backslashes = 0
    for match in _HEREDOC.finditer(command):
        start, end = match.span()
        # Comment-blind quote tracking: preserve the source's conservative skips.
        for ch in command[pos:start]:
            if ch == '\n':
                comment = False
            if single:
                if ch == "'":
                    single = False
                continue
            if double:
                if dq_escape:
                    dq_escape = False
                elif ch == '\\':
                    dq_escape = True
                elif ch == '"':
                    double = False
                continue
            if ch == '\\':
                backslashes += 1
                continue
            escaped = backslashes % 2 == 1
            backslashes = 0
            if escaped:
                continue
            if ch == "'":
                single = True
            elif ch == '"':
                double = True
            elif not comment and ch == '#':
                comment = True
        pos = start
        if single or double or comment or backslashes % 2 == 1:
            continue
        delimiter = match[3] or match[4]
        if match[2] and command[end - 1] != match[2]:
            continue
        if end < len(command) and command[end] not in ' \t\n|&;()<>':
            continue
        # Logical line ends only outside quotes; continuation is checked below.
        sq = dq = False
        k = end
        while k < len(command):
            ch = command[k]
            if sq:
                if ch == "'":
                    sq = False
            elif dq:
                if ch == '\\':
                    k += 1
                elif ch == '"':
                    dq = False
            elif ch == '\n':
                break
            else:
                j = k - 1
                while j >= end and command[j] == '\\':
                    j -= 1
                if (k - 1 - j) % 2 == 0:
                    if ch == "'":
                        sq = True
                    elif ch == '"':
                        dq = True
            k += 1
        if k >= len(command):
            continue
        same_line = command[end:k]
        if (len(same_line) - len(same_line.rstrip('\\'))) % 2 == 1:
            continue
        lines = command[k + 1:].split('\n')
        closing = None
        for i, line in enumerate(lines):
            line = line.lstrip('\t') if match[1] == '-' else line
            if line == delimiter:
                closing = i
                break
            if len(line) > len(delimiter) and line.startswith(delimiter) and line[len(delimiter)] in ')}`|&;(<>':
                break
        if closing is None:
            continue
        content_end = k + 1 + len('\n'.join(lines[:closing + 1]))
        found.append((start, end, k, content_end, command[start:end] + command[k:content_end]))
    top_level = [h for h in found if not any(other is not h and other[2] < h[0] < other[3] for other in found)]
    if len({h[2] for h in top_level}) < len(top_level):
        return command, {}
    salt = secrets.token_hex(8)
    heredocs = {}
    for i, (start, end, content_start, content_end, text) in enumerate(sorted(top_level, key=lambda h: -h[3])):
        placeholder = f'__HEREDOC_{len(top_level) - 1 - i}_{salt}__'
        heredocs[placeholder] = text
        command = command[:start] + placeholder + command[end:content_start] + command[content_end:]
    return command, heredocs


def _parse(command):
    """shell-quote 1.8.3 parseInternal; env(key) is always '$' + key."""
    parsed = []
    for match in _CHUNKER.finditer(command):
        s = match[0]
        if re.fullmatch(_CONTROL, s):
            parsed.append({'op': s})
            continue
        quote = ''
        escaped = glob = False
        out = ''
        i = 0
        while i < len(s):
            c = s[i]
            glob = glob or (not quote and c in '*?')
            if escaped:
                out += c
                escaped = False
            elif quote and c == quote:
                quote = ''
            elif quote == "'":
                out += c
            elif quote and c == '\\':
                i += 1
                c = s[i:i + 1]
                out += c if c in ('"', '\\', '$') else '\\' + c
            elif not quote and c in ('"', "'"):
                quote = c
            elif not quote and re.fullmatch(_CONTROL, c):
                out = None
                parsed.append({'op': s})
                break
            elif not quote and c == '#':
                if out:
                    parsed.append(out)
                parsed.append({'comment': command[match.start() + i + 1:]})
                return parsed
            elif not quote and c == '\\':
                escaped = True
            elif c == '$':
                i += 1
                char = s[i:i + 1]
                if char == '{':
                    i += 1
                    end = s.find('}', i)
                    if s[i:i + 1] == '}' or end < 0:
                        raise ValueError('Bad substitution')
                    name = s[i:end]
                    i = end
                elif char and char in '*@#?$!_-':
                    name = char
                    i += 1  # Source skips the following character in this branch.
                else:
                    rest = s[i:]
                    end_match = re.search(r'[^\w\d_]', rest, re.ASCII)
                    if end_match is None:
                        name = rest
                        i = len(s)
                    else:
                        name = rest[:end_match.start()]
                        i += end_match.start() - 1
                out += '$' + name
            else:
                out += c
            i += 1
        if out is not None:
            parsed.append({'op': 'glob', 'pattern': out} if glob else out)
    return parsed


def _join_continuations(command):
    return re.sub(r'\\+\n', lambda m: '\\' * (len(m[0]) - 2) if (len(m[0]) - 1) % 2 else m[0], command)


def _split_with_operators(command):
    salt = secrets.token_hex(8)
    placeholders = {name: f'__{name}_{salt}__' for name in (
        'SINGLE_QUOTE', 'DOUBLE_QUOTE', 'NEW_LINE', 'ESCAPED_OPEN_PAREN', 'ESCAPED_CLOSE_PAREN')}
    processed, heredocs = _extract_heredocs(command)
    processed = _join_continuations(processed)
    for ch, value in [('"', '"' + placeholders['DOUBLE_QUOTE']), ("'", "'" + placeholders['SINGLE_QUOTE']),
                      ('\n', '\n' + placeholders['NEW_LINE'] + '\n'),
                      ('\\(', placeholders['ESCAPED_OPEN_PAREN']), ('\\)', placeholders['ESCAPED_CLOSE_PAREN'])]:
        processed = processed.replace(ch, value)
    try:
        parsed = _parse(processed)
    except ValueError:
        return [_join_continuations(command)]
    parts = []
    for part in parsed:
        if parts and isinstance(parts[-1], str):
            if isinstance(part, str):
                if part == placeholders['NEW_LINE']:
                    parts.append(None)
                else:
                    parts[-1] += ' ' + part
                continue
            if part.get('op') == 'glob':
                parts[-1] += ' ' + part['pattern']
                continue
        parts.append(part)
    result = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, dict):
            if 'comment' in part:
                part = '#' + part['comment'].replace('"' + placeholders['DOUBLE_QUOTE'], placeholders['DOUBLE_QUOTE']).replace("'" + placeholders['SINGLE_QUOTE'], placeholders['SINGLE_QUOTE'])
            else:
                part = part['pattern'] if part.get('op') == 'glob' else part['op']
        for name, ch in [('SINGLE_QUOTE', "'"), ('DOUBLE_QUOTE', '"'), ('ESCAPED_OPEN_PAREN', '\\('), ('ESCAPED_CLOSE_PAREN', '\\)')]:
            part = part.replace(placeholders[name], ch)
        part = part.replace('\n' + placeholders['NEW_LINE'] + '\n', '\n')
        for placeholder, text in heredocs.items():
            part = part.replace(placeholder, text)
        result.append(part)
    return result


def _static_redirect_target(target):
    return bool(target) and not re.search(_SPACE + "|['\"]", target) and target[0] not in '#!=&' and not any(c in target for c in '$`*?[{~(<')


def split_command(command: str) -> list[str]:
    """Source splitCommand_DEPRECATED (whole commands, NOT argv)."""
    # JS charAt/index arithmetic uses UTF-16 units (including lone surrogates).
    command = ''.join(chr(unit) for (unit,) in struct.iter_unpack('<H', command.encode('utf-16-le', 'surrogatepass')))
    parts = _split_with_operators(command)
    for i, part in enumerate(parts):
        if part not in ('>&', '>', '>>'):
            continue
        prev = parts[i - 1].strip(_WS) if i and parts[i - 1] is not None else None
        next_part = parts[i + 1].strip(_WS) if i + 1 < len(parts) and parts[i + 1] is not None else None
        after = parts[i + 2].strip(_WS) if i + 2 < len(parts) and parts[i + 2] is not None else None
        if next_part is None:
            continue
        effective = next_part
        if part in ('>', '>>') and len(next_part) >= 3 and next_part[-2] == ' ' and next_part[-1] in _FDS and after in ('>', '>>', '>&'):
            effective = next_part[:-2]
        third = part == '>' and next_part == '&' and after in _FDS
        strip = ((part == '>&' and next_part in _FDS) or third or
                 (part == '>' and next_part.startswith('&') and next_part[1:] in _FDS) or
                 (part in ('>', '>>') and _static_redirect_target(effective)))
        if strip:
            if prev and len(prev) >= 3 and prev[-1] in _FDS and prev[-2] == ' ':
                parts[i - 1] = prev[:-2]
            parts[i] = parts[i + 1] = None
            if third:
                parts[i + 2] = None
    return [part.encode('utf-16-le', 'surrogatepass').decode('utf-16-le', 'surrogatepass')
            for part in parts if part is not None and part != '' and part not in _OPERATORS]


def is_autobackgrounding_allowed(command: str) -> bool:
    parts = split_command(command)
    return not parts or parts[0].strip(_WS) != 'sleep'
