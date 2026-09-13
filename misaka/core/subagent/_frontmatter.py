"""CCB frontmatterParser, with its pinned npm yaml 2.8.3 core scalar schema.

The host is MISAKA's existing ruamel parser; this private resolver does not alter
Pi/Hermes YAML. Source Bun.YAML has different duplicate-key/merge semantics;
use the pinned npm branch, not an unpinned Bun runtime, as the native contract.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, ClassVar

from ruamel.yaml import YAML
from ruamel.yaml.composer import Composer
from ruamel.yaml.constructor import SafeConstructor
from ruamel.yaml.error import YAMLError
from ruamel.yaml.events import AliasEvent, MappingStartEvent, SequenceStartEvent
from ruamel.yaml.nodes import MappingNode, ScalarNode
from ruamel.yaml.reader import Reader
from ruamel.yaml.resolver import VersionedResolver
from ruamel.yaml.scanner import Scanner

from misaka.core.prompt_templates import _ECMASCRIPT_WHITESPACE

_SPACE = ''.join(_ECMASCRIPT_WHITESPACE)
_WS = '[' + re.escape(_SPACE) + ']'
_FRONTMATTER = re.compile(r'^---' + _WS + r'*\n([\s\S]*?)---' + _WS + r'*\n?')
_SIMPLE_VALUE = re.compile(r'^([a-zA-Z_-]+):' + _WS + r'+([^\n\r\u2028\u2029]+)$')
_SPECIAL = re.compile(r'[{}[\]*&#!|>%@`]|: ')


class _CoreResolver(VersionedResolver):
    # yaml 2.8.3 dist/schema/core/{bool,int,float}.js and common/null.js.
    # add_implicit_resolver_base is class-local; its non-base counterpart would
    # also mutate ruamel's global versioned resolver registry.
    yaml_implicit_resolvers: ClassVar[dict] = {}

    @property
    def versioned_resolver(self):
        return self.yaml_implicit_resolvers


for _tag, _pattern, _first in (
    ('null', r'^(?:~|[Nn]ull|NULL)?$', ['~', 'n', 'N', '']),
    ('bool', r'^(?:[Tt]rue|TRUE|[Ff]alse|FALSE)$', 'tTfF'),
    ('int', r'^(?:0o[0-7]+|[-+]?[0-9]+|0x[0-9a-fA-F]+)$', '-+0123456789'),
    ('float', r'^(?:[-+]?\.(?:inf|Inf|INF)|\.nan|\.NaN|\.NAN|[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)[eE][-+]?[0-9]+|[-+]?(?:\.[0-9]+|[0-9]+\.[0-9]*))$', '-+.0123456789'),
):
    _CoreResolver.add_implicit_resolver_base('tag:yaml.org,2002:' + _tag, re.compile(_pattern), _first)


class _CoreConstructor(SafeConstructor):
    def construct_unknown(self, node):
        # npm yaml warns for unknown tags but keeps the node's ordinary value.
        logging.getLogger(__name__).warning('Unresolved agent YAML tag: %s', node.tag)
        if isinstance(node, ScalarNode):
            return self.construct_scalar(node)
        if isinstance(node, MappingNode):
            return self.construct_yaml_map(node)
        return self.construct_yaml_seq(node)


_CoreConstructor.add_constructor(None, _CoreConstructor.construct_unknown)


class _CoreReader(Reader):
    in_plain_scalar = False

    def peek(self, index=0):
        # yaml 2.8.3 lexer treats only CR/LF as line breaks. ruamel's scanner
        # still classifies NEL/LS/PS as YAML 1.1 breaks. Give classification a
        # non-break sentinel; prefix() continues returning the original bytes
        # for every scalar token. No input/output replacement or global patch.
        char = super().peek(index)
        if char == '\t':
            # YAML 1.2 permits separation tabs, but not indentation tabs.
            end = self.pointer + index
            start = self.buffer.rfind('\n', 0, end) + 1
            if self.in_plain_scalar or self.buffer[start:end].strip(' \t\r'):
                return ' '
        return '\uffff' if char in '\x85\u2028\u2029' else char


class _CoreScanner(Scanner):
    def scan_plain(self):
        # Continuation tabs are scalar separation, not block indentation.
        self.reader.in_plain_scalar = True
        try:
            return super().scan_plain()
        finally:
            self.reader.in_plain_scalar = False


@dataclass
class _Anchor:
    node: Any
    count: int = 1
    alias_count: int = 0


def _alias_count(node):
    """yaml 2.8.3 Alias.js getAliasCount; aliases use cached anchor weight."""
    if isinstance(node, _Anchor):
        return node.count * node.alias_count
    if isinstance(node, list):
        return max((_alias_count(item) for item in node), default=0)
    return 1


class _CoreComposer(Composer):
    def __init__(self, loader=None):
        super().__init__(loader)
        self.warn_double_anchors = False  # Source aliases resolve the latest anchor.
        self._alias_anchors = {}
        self._alias_stack = []

    def compose_node(self, parent, index):
        # Preserve ruamel's actual node identities and recursive aliases. This
        # parallel shape stores only the source alias-cost graph, not YAML data.
        event = self.parser.peek_event()
        if isinstance(event, AliasEvent):
            anchor = self._alias_anchors.get(event.anchor)
            if anchor is not None:
                if self._alias_stack:
                    self._alias_stack[-1].append(anchor)
                anchor.count += 1
                if anchor.alias_count == 0:
                    anchor.alias_count = _alias_count(anchor.node)
                if anchor.count * anchor.alias_count > 100:
                    raise ValueError('Excessive alias count indicates a resource exhaustion attack')
            return super().compose_node(parent, index)
        node = [] if isinstance(event, (SequenceStartEvent, MappingStartEvent)) else None
        if self._alias_stack:
            self._alias_stack[-1].append(node)
        if event.anchor is not None:
            self._alias_anchors[event.anchor] = _Anchor(node)
        if node is not None:
            self._alias_stack.append(node)
        try:
            return super().compose_node(parent, index)
        finally:
            if node is not None:
                self._alias_stack.pop()


def _load(raw: str) -> Any:
    parser = YAML(typ='safe', pure=True)
    parser.Reader = _CoreReader
    parser.Scanner = _CoreScanner
    parser.Resolver = _CoreResolver
    parser.Constructor = _CoreConstructor
    parser.Composer = _CoreComposer
    return parser.load(raw)


def _quote_problematic_values(raw: str) -> str:
    """Structural port of quoteProblematicValues; retry only after parse error."""
    result = []
    for line in raw.split('\n'):
        match = _SIMPLE_VALUE.match(line)
        if match:
            key, value = match.groups()
            quoted = any(value.startswith(q) and value.endswith(q) for q in ('"', "'"))
            if not quoted and _SPECIAL.search(value):
                escaped = value.replace('\\', '\\\\').replace('"', '\\"')
                result.append(f'{key}: "{escaped}"')
                continue
        result.append(line)
    return '\n'.join(result)


def split_frontmatter(raw: str) -> tuple[dict[str, Any], str] | None:
    match = _FRONTMATTER.match(raw)
    if match is None:
        return None
    data = {}
    try:
        data = _load(match[1])
    except (YAMLError, ValueError, TypeError):
        try:
            data = _load(_quote_problematic_values(match[1]))
        except (YAMLError, ValueError, TypeError) as error:
            logging.getLogger(__name__).warning('Failed to parse agent YAML frontmatter: %s', error)
    # Source parseFrontmatter retains the body even when YAML is invalid.
    # parseAgentFromMarkdown owns trim; the native parser returns that final body.
    return data if isinstance(data, dict) else {}, raw[match.end():].strip(_SPACE)
