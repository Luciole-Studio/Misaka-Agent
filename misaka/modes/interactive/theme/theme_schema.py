"""Translated from pi's ``modes/interactive/theme/theme-schema.ts`` (v1.3.0).

Theme JSON validation, kept out of ``theme.py`` on purpose.

Validating user-authored theme files needs typebox, which costs ~17 MB of module graph to import.
Palette lookup does not, so a presentation that only uses built-in themes should never pay for it.
``main.ts`` installs this validator before runtime resource loading; other consumers can opt in with
``setThemeJsonValidator()``. Built-in themes do not need validation.

MISAKA forks:

- ``cli/engine.main`` (``main.ts``) installs it first thing, and so does ``cli/bootstrap.install`` for MISAKA's other
  process entries (the panel daemon, sub-agent children, research nodes), whose sessions load themes too.
- A TypeBox schema is its JSON schema here (a ``dict``), and TypeBox's ``Compile`` is a jsonschema Draft 2020-12
  validator, so the texts under "Other errors" come from jsonschema (as in ``misaka/ai/utils/validation.py``).
- A color value is a hex color, a variable reference or "" (D11: no ``oklch()``/``okhsl()`` values).
- The title and description name MISAKA.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict, cast

from jsonschema import Draft202012Validator

from .theme_tokens import THEME_TOKENS, ThemeColorValues, ThemeTokenDescriptors


def colorValue(description: str | None = None) -> dict[str, Any]:
    return {
        "anyOf": [
            {
                "type": "string",
                # MISAKA fork: no OKLCH or OKHSL colors (D11).
                "description": "Hex color (#RGB or #RRGGBB), variable reference, or empty string for terminal default",
            },
            {
                "type": "integer",
                "minimum": 0,
                "maximum": 255,
                "description": "256-color palette index (0-255)",
            },
        ],
        **({"description": description} if description else {}),
    }


ColorValueSchema = colorValue()

type ThemeColorValue = str | int


def themeTokenProperties(tokens: ThemeTokenDescriptors) -> tuple[dict[str, Any], list[str]]:
    """The properties, and the names ``Type.Object`` lists as ``required`` (MISAKA fork: TypeBox reads them from
    the ``Type.Optional`` marks)."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, descriptor in tokens.items():
        properties[name] = colorValue(descriptor["description"])
        if "fallback" not in descriptor:
            required.append(name)
    return properties, required


_themeTokenProperties, _requiredThemeTokens = themeTokenProperties(THEME_TOKENS)

ThemeColorsSchema: dict[str, Any] = {
    "type": "object",
    "required": _requiredThemeTokens,
    "properties": _themeTokenProperties,
    "description": (
        "Theme color definitions (scrollbar, thinkingMax, and search highlight colors are optional and use compatible "
        "fallbacks)"
    ),
    "additionalProperties": False,
}

ThemeJsonSchema: dict[str, Any] = {
    "type": "object",
    "required": ["name", "colors"],
    "properties": {
        "$schema": {"type": "string", "description": "JSON schema reference"},
        "name": {
            "type": "string",
            "pattern": "^[^/]+$",
            "description": (
                "Theme name. Must not contain '/' because it is reserved for automatic light/dark theme settings."
            ),
        },
        "appearance": {
            "anyOf": [{"type": "string", "const": "dark"}, {"type": "string", "const": "light"}],
            "description": "Background the theme is designed for. Detected from the theme colors when omitted.",
        },
        "vars": {
            "type": "object",
            "patternProperties": {"^.*$": ColorValueSchema},
            "description": "Reusable color variables",
        },
        "colors": ThemeColorsSchema,
        "export": {
            "type": "object",
            "properties": {
                "pageBg": colorValue("Page background color"),
                "cardBg": colorValue("Card/container background color"),
                "infoBg": colorValue("Info sections background (system prompt, notices)"),
            },
            "description": "Optional colors for HTML export (defaults derived from userMessageBg if not specified)",
            "additionalProperties": False,
        },
    },
    # MISAKA fork: MISAKA's title and description.
    "title": "MISAKA Coding Agent Theme",
    "description": "Theme schema for the MISAKA coding agent",
    "additionalProperties": False,
}

compiledThemeSchema = Draft202012Validator(ThemeJsonSchema)


class _ThemeJsonExport(TypedDict, total=False):
    pageBg: ThemeColorValue
    cardBg: ThemeColorValue
    infoBg: ThemeColorValue


ValidatedThemeJson = TypedDict(
    "ValidatedThemeJson",
    {
        "$schema": NotRequired[str],
        "name": str,
        "appearance": NotRequired[Literal["dark", "light"]],
        "vars": NotRequired[dict[str, ThemeColorValue]],
        "colors": ThemeColorValues[ThemeColorValue],
        "export": NotRequired[_ThemeJsonExport],
    },
)


def validateThemeJson(label: str, json: Any) -> ValidatedThemeJson:
    """Validate one theme document, throwing a message that names the offending tokens."""
    if isinstance(json, dict) and "name" in json and isinstance(json["name"], str) and "/" in json["name"]:
        raise ValueError(
            f'Invalid theme name "{json["name"]}": theme names cannot contain "/" because it is reserved for '
            "automatic light/dark theme settings."
        )

    errors = list(compiledThemeSchema.iter_errors(json))
    if errors:
        missingColors: set[str] = set()
        otherErrors: list[str] = []

        for error in errors:
            if error.validator == "required" and list(error.absolute_path) == ["colors"]:
                for requiredProperty in cast(list[str], error.validator_value):
                    if requiredProperty not in error.instance:
                        missingColors.add(requiredProperty)
                continue

            path = "".join(f"/{part}" for part in error.absolute_path) or "/"
            if error.validator == "additionalProperties" and error.validator_value is False:
                # MISAKA fork: one line per rejected key at its own path, as TypeBox reports it (jsonschema reports
                # the object once).
                parent = path.rstrip("/")
                for key in error.instance:
                    if key not in error.schema.get("properties", {}):
                        otherErrors.append(f"  - {parent}/{key}: Additional properties are not allowed ({key!r} was unexpected)")
                continue
            otherErrors.append(f"  - {path}: {error.message}")

        errorMessage = f'Invalid theme "{label}":\n'
        if len(missingColors) > 0:
            errorMessage += "\nMissing required color tokens:\n"
            errorMessage += "\n".join(f"  - {color}" for color in sorted(missingColors))
            errorMessage += "\n\nPlease add these colors to your theme's \"colors\" object."
            errorMessage += "\nSee the built-in themes (dark.json, light.json) for reference values."
        if len(otherErrors) > 0:
            errorMessage += f"\n\nOther errors:\n{chr(10).join(otherErrors)}"

        raise ValueError(errorMessage)

    return cast(ValidatedThemeJson, json)


__all__ = [
    "ColorValueSchema",
    "ThemeColorValue",
    "ThemeJsonSchema",
    "ValidatedThemeJson",
    "compiledThemeSchema",
    "validateThemeJson",
]
