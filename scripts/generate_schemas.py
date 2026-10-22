#!/usr/bin/env python3
"""Write MISAKA's configuration schemas, ``misaka/schemas/*.schema.json``, from the Python schemas.

Translated from pi's ``packages/coding-agent/scripts/generate-schemas.ts`` (v1.3.0).

    python scripts/generate_schemas.py           write the files
    python scripts/generate_schemas.py --check   say which files are stale, write nothing (exit 1 if any)

``tests/test_config_schemas.py`` fails while a committed file differs from what this script writes, so run it after
changing a schema, a setting's default, ``config/product.KNOBS``, or the package version (``$id`` names the release).

MISAKA forks:
- ``$id`` is the file in MISAKA's repository at the release tag of this version (pyproject ``version``), never pi.dev.
- A schema is a pydantic model on ``model_schema.TypeBoxObject`` (or a ``Literal``, or a ready JSON schema mapping);
  ``typeboxJsonSchema`` gives the JSON schema the TypeBox declaration would, as ``typeboxSchema`` does for tool
  parameters, and keeps ``null`` in a field that admits it (``TypeBoxObject._nullable``).
- The settings schema gets the bundled extensions' own sections (``_EXTENSION_SETTINGS_SECTIONS``), which core may not
  name (tests/test_extension_boundary.py).
- The bundled themes' ``$schema`` names this release's theme schema; the script rewrites it with the schemas
  (``renderBundledThemes``), so a release bump stays one command.

Adding a schema: append a ``SchemaArtifact`` to ``schemaArtifacts()``, after upstream's four (models, settings,
keybindings, theme).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic import BaseModel, TypeAdapter

from misaka.ai.providers.compat_schema import ProviderCompatSchema
from misaka.ai.providers.model_schema import (
    ModelCostSchema,
    ModelInputLimitsSchema,
    ModelPromptCacheSchema,
    ModelThinkingLevelSchema,
    ThinkingLevelMapSchema,
    TypeBoxObject,
)
from misaka.ai.utils.typebox_helpers import _typeboxNode
from misaka.config import get_themes_dir
from misaka.core.keybindings_schema import KeybindingsSchema, KeybindingValueSchema
from misaka.core.model_config import ModelsConfigSchema
from misaka.core.settings_schema import SettingsSchema
from misaka.modes.interactive.theme.theme import getBuiltinThemes
from misaka.modes.interactive.theme.theme_schema import (
    ColorValueSchema,
    ThemeJsonSchema,
)

# Where the files live, relative to the repository (and, in an install, to site-packages).
SCHEMA_DIRECTORY = "misaka/schemas"
THEME_DIRECTORY = "misaka/modes/interactive/theme"
SCHEMA_REPOSITORY = "https://raw.githubusercontent.com/Luciole-Studio/Misaka-Agent"
schemaDraft = "https://json-schema.org/draft/2020-12/schema"
generatedComment = (
    "This file is generated from Python source. Do not edit it manually; run python scripts/generate_schemas.py."
)

# MISAKA fork: the bundled extensions' settings sections, by key. Core describes only its own; an extension's section
# is otherwise left to `additionalProperties`.
_EXTENSION_SETTINGS_SECTIONS: dict[str, str] = {
    "lcm": (
        "The long-conversation context engine (bundled extension): context_threshold is the share of the context "
        "window at which a session compacts (default 0.35). Other behaviour is set with LCM_* variables."
    ),
    "auxiliary": (
        "Models for auxiliary tasks of the bundled extensions, by task: compression (provider, model) writes the "
        "conversation summaries; the default is the session's own model."
    ),
    "compression": (
        "Conversation compression, as the long-conversation context engine reads it: threshold is the share of the "
        "context window at which a session compacts when lcm.context_threshold is not set; enabled false ignores it."
    ),
}


def schemaBaseUrl() -> str:
    """MISAKA fork: the schemas as published with this version's release tag (``vX.Y.Z``)."""
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    return f"{SCHEMA_REPOSITORY}/v{version}/{SCHEMA_DIRECTORY}"


# What `schema` and `definitions` take: a TypeBoxObject model, a `Literal` (or other type), or a JSON schema mapping.
type SchemaSource = Any


@dataclass(slots=True)
class SchemaArtifact:
    filename: str
    schema: SchemaSource
    definitions: Mapping[str, SchemaSource] | None = None
    # MISAKA fork: properties added after the schema's own, by name.
    extraProperties: Mapping[str, Any] = field(default_factory=dict)


def schemaArtifacts() -> list[SchemaArtifact]:
    return [
        SchemaArtifact(
            filename="models.schema.json",
            schema=ModelsConfigSchema,
            definitions={
                "ModelCost": ModelCostSchema,
                "ModelInputLimits": ModelInputLimitsSchema,
                "ModelPromptCache": ModelPromptCacheSchema,
                "ProviderCompat": ProviderCompatSchema,
                "ThinkingLevelMap": ThinkingLevelMapSchema,
            },
        ),
        SchemaArtifact(
            filename="settings.schema.json",
            schema=SettingsSchema,
            definitions={"ModelThinkingLevel": ModelThinkingLevelSchema},
            extraProperties={
                name: {"type": "object", "description": description}
                for name, description in _EXTENSION_SETTINGS_SECTIONS.items()
            },
        ),
        SchemaArtifact(
            filename="keybindings.schema.json",
            schema=KeybindingsSchema,
            definitions={"KeybindingValue": KeybindingValueSchema},
        ),
        SchemaArtifact(
            filename="theme.schema.json",
            schema=ThemeJsonSchema,
            definitions={"ColorValue": ColorValueSchema},
        ),
    ]


def _typeBoxModels() -> dict[str, type[TypeBoxObject]]:
    """Every ``TypeBoxObject`` model by class name, the key pydantic gives its ``$defs`` entry."""
    found: dict[str, type[TypeBoxObject]] = {}
    pending: list[type[TypeBoxObject]] = [TypeBoxObject]
    while pending:
        for subclass in pending.pop().__subclasses__():
            if found.setdefault(subclass.__name__, subclass) is not subclass and subclass._nullable:
                raise RuntimeError(f"Two schema models are named {subclass.__name__}")
            pending.append(subclass)
    return found


_STASHED_DEFAULT = "__misaka_default__"


def _keepNull(node: dict[str, Any], nullable: frozenset[str]) -> None:
    """A field whose TypeBox type includes ``Type.Null()`` keeps ``null``: ``_typeboxNode`` would read pydantic's
    ``anyOf: [X, null]`` of an optional field as ``Type.Optional(X)``."""
    for name, schema in (node.get("properties") or {}).items():
        if name in nullable and "default" in schema:
            default = schema.pop("default")
            if default is not None:
                schema[_STASHED_DEFAULT] = default


def _restoreDefaults(value: Any) -> Any:
    if isinstance(value, list):
        return [_restoreDefaults(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {("default" if key == _STASHED_DEFAULT else key): _restoreDefaults(child) for key, child in value.items()}


def typeboxJsonSchema(source: SchemaSource) -> dict[str, Any]:
    """The JSON schema TypeBox emits for what ``source`` declares (see the module docstring)."""
    if isinstance(source, Mapping):
        return json.loads(json.dumps(source))
    if isinstance(source, type) and issubclass(source, BaseModel):
        schema = source.model_json_schema()
    else:
        schema = TypeAdapter(source).json_schema()
    defs = schema.pop("$defs", {})
    models = _typeBoxModels()
    for name, node in [(getattr(source, "__name__", ""), schema), *defs.items()]:
        model = models.get(name)
        if model is not None and model._nullable:
            _keepNull(node, model._nullable)
    return _restoreDefaults(_typeboxNode(schema, defs))


schemaAnnotationKeys = frozenset({"default", "deprecated", "description", "examples", "readOnly", "title", "writeOnly"})


def splitAnnotations(schema: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(shape, annotations)``."""
    shape: dict[str, Any] = {}
    annotations: dict[str, Any] = {}
    for key, value in schema.items():
        (annotations if key in schemaAnnotationKeys else shape)[key] = value
    return shape, annotations


def _shapeKey(shape: Mapping[str, Any]) -> str:
    # MISAKA fork: key order does not decide a match (pydantic and TypeBox order a schema's keys differently).
    return json.dumps(shape, sort_keys=True)


def addDefinitions(schema: dict[str, Any], definitions: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    """Replace selected repeated schemas with local references while retaining per-use annotations."""
    preparedDefinitions: list[tuple[str, dict[str, Any], str]] = []
    for name, definition in definitions.items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name):
            raise ValueError(f"Invalid schema definition name: {name}")
        shape, _annotations = splitAnnotations(definition)
        preparedDefinitions.append((name, shape, _shapeKey(shape)))

    referencedDefinitions: set[str] = set()

    def replaceDefinitions(value: Any, excludedName: str | None = None) -> Any:
        if isinstance(value, list):
            return [replaceDefinitions(item, excludedName) for item in value]
        if not isinstance(value, dict):
            return value

        shape, annotations = splitAnnotations(value)
        shapeKey = _shapeKey(shape)
        match = next(
            (name for name, _shape, key in preparedDefinitions if name != excludedName and key == shapeKey), None
        )
        if match is not None:
            referencedDefinitions.add(match)
            return {"$ref": f"#/$defs/{match}", **annotations}

        return {
            key: child if key in schemaAnnotationKeys else replaceDefinitions(child, excludedName)
            for key, child in value.items()
        }

    compactSchema = replaceDefinitions(schema)
    compactDefinitions = {name: replaceDefinitions(shape, name) for name, shape, _key in preparedDefinitions}
    unusedDefinitions = [name for name, _shape, _key in preparedDefinitions if name not in referencedDefinitions]
    if unusedDefinitions:
        raise ValueError(f"Unused schema definitions: {', '.join(unusedDefinitions)}")
    return {**compactSchema, "$defs": compactDefinitions}


def serializeSchema(artifact: SchemaArtifact) -> str:
    schema = typeboxJsonSchema(artifact.schema)
    if artifact.extraProperties:
        schema["properties"] = {**schema["properties"], **artifact.extraProperties}
    if artifact.definitions:
        schema = addDefinitions(
            schema, {name: typeboxJsonSchema(definition) for name, definition in artifact.definitions.items()}
        )
    document = {"$schema": schemaDraft, "$id": f"{schemaBaseUrl()}/{artifact.filename}", "$comment": generatedComment, **schema}
    return f"{json.dumps(document, indent=2, ensure_ascii=False)}\n"


def renderConfigSchemas() -> dict[str, str]:
    """The files by their path relative to the repository, in artifact order."""
    return {f"{SCHEMA_DIRECTORY}/{artifact.filename}": serializeSchema(artifact) for artifact in schemaArtifacts()}


def renderBundledThemes() -> dict[str, str]:
    """MISAKA fork: the bundled themes, by their path relative to the repository, with ``$schema`` naming the theme
    schema of this release. Only the built-in themes (``getBuiltinThemes``): another JSON file in their folder is
    not a theme and is never rewritten."""
    rendered: dict[str, str] = {}
    for path in [Path(get_themes_dir()) / f"{name}.json" for name in sorted(getBuiltinThemes())]:
        theme = json.loads(path.read_text(encoding="utf-8"))
        document = {
            "$schema": f"{schemaBaseUrl()}/theme.schema.json",
            **{key: value for key, value in theme.items() if key != "$schema"},
        }
        rendered[f"{THEME_DIRECTORY}/{path.name}"] = f"{json.dumps(document, indent=2, ensure_ascii=False)}\n"
    return rendered


def _renderedFiles() -> dict[str, str]:
    return {**renderConfigSchemas(), **renderBundledThemes()}


def generateSchemas(write: Callable[[str], None] = print) -> None:
    for relativePath, content in _renderedFiles().items():
        path = ROOT / relativePath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        write(f"Generated {relativePath}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write misaka/schemas/*.schema.json from the Python schemas, and the bundled themes' $schema."
    )
    parser.add_argument("--check", action="store_true", help="report stale files and write nothing")
    args = parser.parse_args(argv)
    if not args.check:
        generateSchemas()
        return 0
    stale = [
        relativePath
        for relativePath, content in _renderedFiles().items()
        if not (ROOT / relativePath).is_file() or (ROOT / relativePath).read_text(encoding="utf-8") != content
    ]
    for relativePath in stale:
        print(f"{relativePath} is stale; run python scripts/generate_schemas.py")
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
