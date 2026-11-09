"""MISAKA fork (misaka/core/export_html): theme keys and values are whitelisted before they reach the export page's
<style>; a value that could close the element falls back to `inherit`."""

from __future__ import annotations

from misaka.core import export_html


def test_theme_values_that_could_break_out_of_the_style_element_fall_back(monkeypatch):
    monkeypatch.setattr(export_html, "getResolvedThemeColors", lambda _name=None: {
        "accent": "red;} </style><script>alert(1)</script>",
        "bad key;": "#ffffff",
        "ok": "#123456",
        "userMessageBg": "#343541",
    })
    monkeypatch.setattr(export_html, "getThemeExportColors", lambda _name=None: {"pageBg": "x;}"})

    css = export_html.generateThemeVars()

    assert "--accent: inherit;" in css
    assert "<script" not in css and "</style" not in css
    assert "bad key" not in css
    assert "--ok: #123456;" in css
    assert "--exportPageBg: inherit;" in css
    assert "--exportCardBg: rgb(" in css
