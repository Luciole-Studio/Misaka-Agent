"""Ported from pi's ``packages/coding-agent/test/export-html-xss.test.ts`` (v1.1.0): the export page's template sanitises
markdown URLs and escapes every session field it puts into attributes or innerHTML."""

from __future__ import annotations

import re
from pathlib import Path

from misaka.core import export_html

templateJs = (Path(export_html.__file__).parent / "template.js").read_text(encoding="utf-8")


def test_overrides_the_marked_link_renderer_to_use_scheme_allow_list_sanitization():
    assert re.search(r"link\s*\(\s*token\s*\)", templateJs)
    assert re.search(r"sanitizeMarkdownUrl\(token\.href\)", templateJs)
    assert re.search(r"\^\(https\?\|mailto\|tel\|ftp\)", templateJs)


def test_overrides_the_marked_image_renderer_to_use_scheme_allow_list_sanitization():
    assert re.search(r"image\s*\(\s*token\s*\)", templateJs)
    assert re.search(r"sanitizeMarkdownUrl\(token\.href\)", templateJs)


def test_strips_c0_controls_before_checking_and_emitting_markdown_urls():
    assert r"replace(/[\x00-\x1f\x7f]/g, '')" in templateJs
    assert not re.search(r"\^\\s\*\(javascript\|vbscript\|data\):", templateJs, re.IGNORECASE)


def test_escapes_href_attributes_in_the_custom_link_renderer():
    # The link renderer must escape href values to prevent attribute breakout
    assert re.search(r"escapeHtml\(href\)", templateJs)


def test_escapes_image_mime_type_attributes():
    # Image mimeType must be escaped to prevent attribute breakout
    assert not re.search(r"\$\{img\.mimeType\}", templateJs)
    assert re.search(r"escapeHtml\(img\.mimeType", templateJs)


def test_escapes_image_data_attributes():
    # Image data is embedded in src attributes and must not allow attribute breakout.
    assert not re.search(r';base64,\$\{img\.data\}"', templateJs)
    assert re.search(r";base64,\$\{escapeHtml\(img\.data \|\| (?:''|\"\")\)\}\"", templateJs)


def test_escapes_entry_ids_before_inserting_them_into_attributes():
    # Session entry IDs are embedded in id and data-entry-id attributes.
    assert not re.search(r'id="\$\{entryId\}"', templateJs)
    assert not re.search(r'data-entry-id="\$\{entryId\}"', templateJs)
    assert re.search(r"entry-\$\{escapeHtml\(entry\.id\)\}", templateJs)
    assert re.search(r'data-entry-id="\$\{escapeHtml\(entryId\)\}"', templateJs)


def test_escapes_tree_metadata_rendered_from_session_fields():
    # The tree renders session metadata via innerHTML, so dynamic fields must be escaped.
    assert not re.search(r"\[\$\{msg\.toolName \|\| 'tool'\}\]", templateJs)
    assert not re.search(r"\[\$\{msg\.role\}\]", templateJs)
    assert not re.search(r"\[model: \$\{entry\.modelId\}\]", templateJs)
    assert not re.search(r"\[thinking: \$\{entry\.thinkingLevel\}\]", templateJs)
    assert not re.search(r"\[\$\{entry\.type\}\]", templateJs)
    assert re.search(r"\$\{escapeHtml\(msg\.toolName \|\| 'tool'\)\}", templateJs)
    assert re.search(r"\$\{escapeHtml\(msg\.role\)\}", templateJs)
    assert re.search(r"\$\{escapeHtml\(entry\.modelId\)\}", templateJs)
    assert re.search(r"\$\{escapeHtml\(entry\.thinkingLevel\)\}", templateJs)
    assert re.search(r"\$\{escapeHtml\(entry\.type\)\}", templateJs)


def test_escapes_model_names_in_the_exported_header():
    # Assistant message provider/model values are collected from the session and rendered with innerHTML.
    assert not re.search(r"\$\{globalStats\.models\.join\(', '\) \|\| 'unknown'\}", templateJs)
    assert re.search(r"\$\{escapeHtml\(globalStats\.models\.join\(', '\) \|\| 'unknown'\)\}", templateJs)
