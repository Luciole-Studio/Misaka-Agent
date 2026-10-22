"""Contracts: the runtime description of a type (local or remote), its members, placement and fingerprint, and
how a consumer token binds to a provider.

Translated from pi's ``packages/chord/src/contract.ts``. A definition (``remote(name, {...})``, ``local(name, {...})``)
is a mapping with upstream's keys; a contract is an object with upstream's fields, except that ``def`` is a Python
keyword and is ``def_`` here. Schemas are JSON-Schema dicts (``misaka.chord._typebox``). The static types at the end
of upstream's file (``MethodImplementation``, ``Call``, ``ImplementationOf``, ``Ref``, ``InputOf``) check
implementations and references at compile time; Python has no counterpart, so they are aliases of ``Any``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal

from misaka.chord._typebox import TSchema, Type, Value
from misaka.chord.errors import ChordError, invalid
from misaka.chord.schema import canonicalJson, fits, hash64
from misaka.utils.js import jsIsInteger, jsString
from misaka.utils.js_regexp import RegExp

MEMBER: Final = "chord.member"


@dataclass(frozen=True, slots=True)
class MethodSpec:
    """A method's arguments and result; ``None``: it takes no arguments, or returns nothing (void)."""

    args: TSchema | None
    result: TSchema | None


@dataclass(frozen=True, slots=True)
class OpaqueSpec:
    id: str


def method(args: TSchema | None = None, result: TSchema | None = None) -> MethodSpec:
    """A method. ``method()``: no arguments (called as ``ref.name(ctx)``, implemented as ``lambda ctx: ...``) and no
    result. ``method(args)``: no result, the handler returns nothing (void). ``method(args, result)``, or
    ``method(None, result)`` for a method without arguments that returns something."""
    return MethodSpec(args, result)


# A JavaScript regular expression: `$` is the end of the input, `\d` is ASCII, `\s` is ECMAScript's white space.
_OPAQUE_ID = RegExp(r"^[^@\s]+@\d+$")


def opaque(id: str) -> OpaqueSpec:
    """A native in-process object identified by ``name@version`` (local contracts only)."""
    if not _OPAQUE_ID.test(jsString(id)):
        raise TypeError(f"opaque id must be name@version: {jsString(id)}")
    return OpaqueSpec(id)


# `VariantDef`: `{ state?: { name: schema }, methods?: { name: method() } }`.
type VariantDef = Mapping[str, Any]
# Where objects of a remote type live: one named host (`{ name }`).
type HostNamed = dict[str, str]
# Where objects of a remote type live: the host `prefix` + the input's string field `key` (`session:` + `session`).
type HostFamily = dict[str, str]
# A remote type's placement: data only, so a description carries it and every host can check it.
type Placement = dict[str, str]
# The placements a remote type with input `I` may have (TypeScript checks the field; here `remote` checks the shape).
type PlacementFor = Placement


def hostNamed(name: str) -> HostNamed:
    """Objects of the type live on the host named ``name`` (``hostNamed("server")``)."""
    if not isinstance(name, str) or name == "":
        raise TypeError("hostNamed needs a host name")
    return {"name": name}


def hostFamily(prefix: str, key: str) -> HostFamily:
    """Objects of the type live on ``prefix`` + the value of the input's string field ``key``:
    ``hostFamily("session:", "session")`` puts ``{"session": "S1", ...}`` on ``session:S1``. The field is required
    and must be a non-empty string."""
    if not isinstance(prefix, str) or prefix == "":
        raise TypeError("hostFamily needs a host-name prefix")
    if not isinstance(key, str) or key == "":
        raise TypeError("hostFamily needs an input field name")
    return {"prefix": prefix, "key": key}


def resolveHost(typeName: str, placement: Placement, input: Any) -> str:
    """The host an object of ``placement`` with this (normalized) input lives on. Throws ``invalid/placement``."""
    if "name" in placement:
        return placement["name"]
    value = input.get(placement["key"]) if isinstance(input, Mapping) else None
    if not isinstance(value, str) or value == "":
        raise invalid("placement", f"{typeName}: input field {placement['key']} names no host")
    return placement["prefix"] + value


def placedOn(placement: Placement, hostName: str) -> bool:
    """Whether the host named ``hostName`` may serve a type of ``placement``."""
    if "name" in placement:
        return placement["name"] == hostName
    return hostName.startswith(placement["prefix"]) and len(hostName) > len(placement["prefix"])


def placementText(placement: Placement) -> str:
    return placement["name"] if "name" in placement else f"{placement['prefix']}<{placement['key']}>"


# `RemoteDef`: `{ version, input, host, variants }`; `LocalDef`: `{ version, input, members? }`.
type RemoteDef = Mapping[str, Any]
type LocalDef = Mapping[str, Any]
# A local member: a method, an opaque native object, or a state member described by its schema.
type LocalMember = MethodSpec | OpaqueSpec | TSchema

# A description travels as a reference's contract token (wire.py), so it is declared once, as the schema that
# validates it. `TSchema` is an empty interface: any object is one, which is all a peer's schema is checked for here.
SchemaObject: Final = Type.Unsafe(Type.Object({}))

MemberDescriptionSchema: Final = Type.Union(
    [
        # Absent `args`: the method takes none. Absent `result`: it returns nothing.
        Type.Object(
            {
                "kind": Type.Literal("method"),
                "args": Type.Optional(SchemaObject),
                "result": Type.Optional(SchemaObject),
            }
        ),
        Type.Object({"kind": Type.Literal("state"), "schema": SchemaObject}),
        Type.Object({"kind": Type.Literal("opaque"), "id": Type.String()}),
    ]
)
# `{ kind: "method", args?, result? } | { kind: "state", schema } | { kind: "opaque", id }`.
type MemberDescription = dict[str, Any]

# Local contracts have one unnamed variant.
LOCAL_VARIANT: Final = ""

PlacementSchema: Final = Type.Union(
    [
        Type.Object({"name": Type.String({"minLength": 1})}, {"additionalProperties": False}),
        Type.Object(
            {"prefix": Type.String({"minLength": 1}), "key": Type.String({"minLength": 1})},
            {"additionalProperties": False},
        ),
    ]
)

DescriptionSchema: Final = Type.Object(
    {
        "name": Type.String({"minLength": 1}),
        "scope": Type.Union([Type.Literal("remote"), Type.Literal("local")]),
        "version": Type.Integer(),
        "input": SchemaObject,
        # Remote types only (required there): where their objects live.
        "host": Type.Optional(PlacementSchema),
        "variants": Type.Record(Type.String(), Type.Record(Type.String(), MemberDescriptionSchema)),
    }
)
# `{ name, scope, version, input, host?, variants }`.
type Description = dict[str, Any]


@dataclass(frozen=True, eq=False, slots=True)
class RemoteContract:
    name: str
    version: int
    input: TSchema
    description: Description
    # MISAKA fork: upstream's `def` is a Python keyword.
    def_: RemoteDef
    fingerprint: str = ""
    scope: Literal["remote"] = field(default="remote")


@dataclass(frozen=True, eq=False, slots=True)
class LocalContract:
    name: str
    version: int
    input: TSchema
    description: Description
    # MISAKA fork: upstream's `def` is a Python keyword.
    def_: LocalDef
    fingerprint: str = ""
    scope: Literal["local"] = field(default="local")


type AnyContract = RemoteContract | LocalContract

# Names a reference uses itself; members may not use them.
RESERVED_MEMBERS: Final[frozenset[str]] = frozenset(
    {
        "variant",
        "status",
        "ready",
        "shape",
        "error",
        "onShape",
        "onStatus",
        "release",
        "then",
        "contract",
    }
)


def describeMember(name: str, member: Any, scope: Literal["remote", "local"]) -> MemberDescription:
    if name in RESERVED_MEMBERS:
        raise TypeError(f"member name is reserved: {name}")
    if isinstance(member, MethodSpec):
        description: MemberDescription = {"kind": "method"}
        if member.args is not None:
            description["args"] = member.args
        if member.result is not None:
            description["result"] = member.result
        return description
    if isinstance(member, OpaqueSpec):
        if scope == "remote":
            raise TypeError(f"remote contracts cannot expose opaque members: {name}")
        return {"kind": "opaque", "id": member.id}
    return {"kind": "state", "schema": member}


def finish[C: (RemoteContract, LocalContract)](contract: C) -> C:
    return replace(contract, fingerprint=hash64(canonicalJson(contract.description)))


def checkHeader(name: str, version: Any) -> int:
    """Throws unless the name is non-empty and the version a positive integer (``Number.isInteger``: ``2.0`` is one).
    Returns the version as an ``int`` (JavaScript has one number type)."""
    if not name:
        raise TypeError("contract name is empty")
    if not jsIsInteger(version) or version < 1:
        raise TypeError("contract version must be a positive integer")
    return int(version)


def remote(name: str, def_: RemoteDef) -> RemoteContract:
    version = checkHeader(name, def_.get("version"))
    placement = def_.get("host")
    if placement is None or not Value.Check(PlacementSchema, placement):
        raise TypeError(f"remote contract {name} needs `host`: hostNamed(…) or hostFamily(…)")
    # A JSON copy: the description travels, and a placement object may carry nothing else.
    host: Placement = (
        {"name": placement["name"]} if "name" in placement else {"prefix": placement["prefix"], "key": placement["key"]}
    )
    variants: dict[str, dict[str, MemberDescription]] = {}
    if len(def_["variants"]) == 0:
        raise TypeError(f"remote contract {name} has no variants")
    for kind, variant in def_["variants"].items():
        members: dict[str, MemberDescription] = {}
        for key, schema in (variant.get("state") or {}).items():
            members[key] = describeMember(key, schema, "remote")
        for key, spec in (variant.get("methods") or {}).items():
            if key in members:
                raise TypeError(f"member {key} is both state and method in {name}.{kind}")
            members[key] = describeMember(key, spec, "remote")
            if members[key]["kind"] != "method":
                raise TypeError(f"methods.{key} is not a method()")
        variants[kind] = members
    description: Description = {
        "name": name,
        "scope": "remote",
        "version": version,
        "input": def_["input"],
        "host": host,
        "variants": variants,
    }
    return finish(RemoteContract(name, version, def_["input"], description, def_))


def local(name: str, def_: LocalDef) -> LocalContract:
    version = checkHeader(name, def_.get("version"))
    members: dict[str, MemberDescription] = {}
    for key, member in (def_.get("members") or {}).items():
        members[key] = describeMember(key, member, "local")
    description: Description = {
        "name": name,
        "scope": "local",
        "version": version,
        "input": def_["input"],
        "variants": {LOCAL_VARIANT: members},
    }
    return finish(LocalContract(name, version, def_["input"], description, def_))


def contractFromDescription(description: Description) -> AnyContract:
    """A consumer token received over a link (only its description travels). Has no ``def`` members, so it is only
    good for binding and validation."""
    name = description.get("name")
    scope = description.get("scope")
    version = description.get("version")
    input = description.get("input")
    host = description.get("host")
    if (
        not isinstance(name, str)
        or (scope != "remote" and scope != "local")
        or not jsIsInteger(version)
        or (scope == "remote") != (host is not None)
    ):
        raise ChordError("invalid", "malformed contract token", {"reason": "token"})
    # A wire `1.0` is the number 1.
    version = int(version)
    fingerprint = hash64(canonicalJson(description))
    if scope == "remote":
        return RemoteContract(
            name, version, input, description, {"version": version, "input": input, "host": host, "variants": {}}, fingerprint
        )
    return LocalContract(name, version, input, description, {"version": version, "input": input}, fingerprint)


def coverage(provider: Description, consumer: Description) -> str | None:
    """Returns why ``provider`` does not cover ``consumer``, or ``None`` when it does."""
    if provider["name"] != consumer["name"] or provider["scope"] != consumer["scope"]:
        return "different type"
    if not samePlacement(provider, consumer):
        return "host"
    if not fits(consumer["input"], provider["input"]):
        return "input"
    for kind, consumerMembers in consumer["variants"].items():
        providerMembers = provider["variants"].get(kind)
        if providerMembers is None:
            return f"variant {kind} missing"
        for name, wanted in consumerMembers.items():
            offered = providerMembers.get(name)
            if offered is None or offered["kind"] != wanted["kind"]:
                return f"{kind}.{name} missing or of another kind"
            if offered["kind"] == "method" and wanted["kind"] == "method":
                # Both take arguments or neither does; a consumer that expects no result accepts any.
                if ("args" in wanted) != ("args" in offered):
                    return f"{kind}.{name} arguments"
                if "args" in wanted and "args" in offered and not fits(wanted["args"], offered["args"]):
                    return f"{kind}.{name} arguments"
                if "result" in wanted and ("result" not in offered or not fits(offered["result"], wanted["result"])):
                    return f"{kind}.{name} result"
            elif offered["kind"] == "state" and wanted["kind"] == "state":
                if not fits(offered["schema"], wanted["schema"]):
                    return f"{kind}.{name} state"
            elif offered["kind"] == "opaque" and wanted["kind"] == "opaque" and offered["id"] != wanted["id"]:
                return f"{kind}.{name} opaque {offered['id']} != {wanted['id']}"
    return None


def samePlacement(a: Description, b: Description) -> bool:
    return canonicalJson(a.get("host")) == canonicalJson(b.get("host"))


def placementOf(description: Description) -> str:
    host = description.get("host")
    return "(local)" if host is None else placementText(host)


# Maps the provider's current implementation to the view a consumer of an older contract version expects.
type Adapter = Callable[[dict[str, Any]], dict[str, Any]]
type Accepts = Mapping[int, Adapter]


def bind(provider: AnyContract, accepts: Accepts | None, consumer: AnyContract) -> Adapter | None:
    """How a consumer token binds to a provider: directly or through an ``accepts`` adapter. Throws
    contract_mismatch."""
    if provider.fingerprint == consumer.fingerprint:
        return None
    # A moved type is another type for its consumers: no adapter reaches across hosts.
    if not samePlacement(provider.description, consumer.description):
        raise ChordError(
            "contract_mismatch",
            f"{provider.name} lives on {placementOf(provider.description)}, the consumer expects "
            f"{placementOf(consumer.description)}",
        )
    if provider.version == consumer.version:
        reason = coverage(provider.description, consumer.description)
        if reason is None:
            return None
        raise ChordError(
            "contract_mismatch",
            f"{provider.name}@{provider.version} does not cover the consumer's description ({reason}); forgotten version bump?",
        )
    adapter = accepts.get(consumer.version) if accepts is not None else None
    if adapter is None:
        raise ChordError(
            "contract_mismatch",
            f"{provider.name}: provider version {provider.version}, consumer version {consumer.version}, no accepts adapter",
        )
    return adapter


# ─── Static types ────────────────────────────────────────────────────────────
# What a handler is, what a call resolves to, what a factory returns and what `get` returns: TypeScript checks these
# at compile time; Python has no counterpart.
type MethodImplementation = Callable[..., Any]
type Call = Callable[..., Any]
type InputOf = Any
type ImplementationOf = dict[str, Any]
type Ref = Any


def takesNoArgs(contract: AnyContract, name: str) -> bool:
    """Whether ``contract``'s method ``name`` takes no arguments (it is called as ``ref.name(ctx)``)."""
    for members in contract.description["variants"].values():
        member = members.get(name)
        if member is not None and member["kind"] == "method":
            return "args" not in member
    return False


__all__ = [
    "LOCAL_VARIANT",
    "RESERVED_MEMBERS",
    "Accepts",
    "Adapter",
    "AnyContract",
    "Call",
    "Description",
    "DescriptionSchema",
    "HostFamily",
    "HostNamed",
    "ImplementationOf",
    "InputOf",
    "LocalContract",
    "LocalDef",
    "LocalMember",
    "MemberDescription",
    "MethodImplementation",
    "MethodSpec",
    "OpaqueSpec",
    "Placement",
    "PlacementFor",
    "Ref",
    "RemoteContract",
    "RemoteDef",
    "VariantDef",
    "bind",
    "contractFromDescription",
    "coverage",
    "hostFamily",
    "hostNamed",
    "local",
    "method",
    "opaque",
    "placedOn",
    "placementText",
    "remote",
    "resolveHost",
    "takesNoArgs",
]
