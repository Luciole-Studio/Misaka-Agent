"""The URL-shaped safety checks that sit beside the SSRF gate rather than inside it.

Ported from Hermes' ``tools/url_safety.py``, with the credential-prefix table lifted
whole out of its ``agent/redact.py``. Only the parts MISAKA does not already own came
across: a normaliser that turns the IRI a model typed into something an HTTP client will
accept, a credential detector that refuses to put a key on the wire inside a URL, the
cloud-metadata floor that stays shut whatever the operator configured, and the operator
opt-out itself. Everything here answers a question about the *text* of a URL; the
question of what address it reaches belongs to :mod:`misaka.core.tools._web.bounded`.

Four pieces of the Hermes original were deliberately left behind:

* ``is_safe_url`` / ``async_is_safe_url`` and the SSRF-guarded httpx transports --
  ``bounded.py`` is MISAKA's SSRF gate and is strictly stronger: it pins the socket to
  the address it vetted and re-vets every redirect hop, so this module's only job is to
  hand it the always-blocked floor and the opt-out.
* the proxy DNS delegation at ``url_safety.py:447-475``, which lets a request through
  unchecked whenever DNS fails and a proxy variable is set -- that is exactly the reach
  ``bounded.open_checked_stream``'s address pinning plus ``trust_env=False``
  (``bounded.py:214``) exists to deny, and the two cannot both hold. MISAKA keeps pinning.
* ``_TRUSTED_PRIVATE_IP_HOSTS``, a Hermes-specific allowance for one QQ media domain.
* ``get_hermes_home_override`` and its ``secret_scope`` multiplexed-profile handling --
  MISAKA serves one profile per process, so the opt-out has exactly one scope to resolve.

One thing is deliberately *stricter* than the original, and it is the credential table's
body length -- see :data:`_URL_BODY_MINIMUM`.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

from misaka.core.web.config import web_config

logger = logging.getLogger(__name__)


def normalize_url_for_request(url: str) -> str:
    """Return an ASCII-safe HTTP URL for MISAKA-owned URL tools.

    Hermes' ``normalize_url_for_request``. Browsers and HTTP clients expect URIs, but
    users and models hand over IRIs such as ``https://wttr.in/Köln``. Preserve URL syntax
    and existing percent escapes -- ``%`` is in every safe set below, which is what makes
    this idempotent and keeps ``%C3%B6`` from becoming ``%25C3%25B6`` on a second pass --
    while encoding non-ASCII host/path/query/fragment text.

    Intentionally for URL *tool inputs* only. Rewriting an arbitrary string the model
    wrote would corrupt shell commands and file paths that merely look URL-shaped, which
    is why a non-``str`` and a non-http(s) scheme both come back untouched.
    """
    if not isinstance(url, str):
        return url

    raw = url.strip()
    if not raw:
        return raw

    # Models sometimes emit otherwise valid URLs with whitespace between the
    # scheme separator and authority (``https:// docs.example``). That position
    # is never meaningful in HTTP(S) URLs, and repairing it before parsing keeps
    # web tools from failing on a formatting artifact while leaving path/query
    # whitespace to the normal percent-encoding path below.
    raw = re.sub(r"^([A-Za-z][A-Za-z0-9+.-]*://)\s+", r"\1", raw)

    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw

    if parsed.scheme.lower() not in {"http", "https"}:
        return raw

    netloc = parsed.netloc
    hostname = parsed.hostname
    if hostname:
        try:
            ascii_host = hostname.encode("idna").decode("ascii")
        except UnicodeError:
            ascii_host = hostname
        if ascii_host != hostname:
            netloc = netloc.replace(hostname, ascii_host, 1)

    path = quote(parsed.path, safe="/%:@!$&'()*+,;=")
    query = quote(parsed.query, safe="/%:@!$&'()*+,;=?")
    fragment = quote(parsed.fragment, safe="/%:@!$&'()*+,;=?")

    return urlunsplit((parsed.scheme, netloc, path, query, fragment))


# Known API key prefixes -- match the prefix + contiguous token chars.
# Copied verbatim from Hermes' ``agent/redact.py`` ``_PREFIX_PATTERNS``, trailing vendor
# comments and all: it is a data table, and a table that drifts from its source is worse
# than no table. The one thing MISAKA does not inherit is the ``_PREFIX_SUBSTRINGS``
# pre-screen the GitLab note below refers to -- that is a throughput optimisation for
# redacting whole compaction payloads, and a URL is a few hundred bytes.
#
# The body lengths below are Hermes'. They are not the ones compiled into ``_PREFIX_RE``:
# the table stays as it arrived so it can be diffed against its source, and the one place
# MISAKA tightens it is :func:`_url_pattern`, immediately after.
_PREFIX_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{10,}",           # OpenAI / OpenRouter / Anthropic (sk-ant-*)
    r"ghp_[A-Za-z0-9]{10,}",            # GitHub PAT (classic)
    r"github_pat_[A-Za-z0-9_]{10,}",    # GitHub PAT (fine-grained)
    r"gho_[A-Za-z0-9]{10,}",            # GitHub OAuth access token
    r"ghu_[A-Za-z0-9]{10,}",            # GitHub user-to-server token
    r"ghs_[A-Za-z0-9]{10,}",            # GitHub server-to-server token
    r"ghr_[A-Za-z0-9]{10,}",            # GitHub refresh token
    r"xapp-\d+-[A-Za-z0-9-]{10,}",      # Slack app-Level token
    r"xox[baprs]-[A-Za-z0-9-]{10,}",    # Slack bot/app/user tokens
    r"AIza[A-Za-z0-9_-]{30,}",          # Google API keys
    r"pplx-[A-Za-z0-9]{10,}",           # Perplexity
    r"fal_[A-Za-z0-9_-]{10,}",          # Fal.ai
    r"fc-[A-Za-z0-9]{10,}",             # Firecrawl
    r"bb_live_[A-Za-z0-9_-]{10,}",      # BrowserBase
    r"gAAAA[A-Za-z0-9_=-]{20,}",        # Codex encrypted tokens
    r"AKIA[A-Z0-9]{16}",                # AWS Access Key ID
    r"sk_live_[A-Za-z0-9]{10,}",        # Stripe secret key (live)
    r"sk_test_[A-Za-z0-9]{10,}",        # Stripe secret key (test)
    r"rk_live_[A-Za-z0-9]{10,}",        # Stripe restricted key
    r"SG\.[A-Za-z0-9_-]{10,}",          # SendGrid API key
    r"hf_[A-Za-z0-9]{10,}",             # HuggingFace token
    r"r8_[A-Za-z0-9]{10,}",             # Replicate API token
    r"npm_[A-Za-z0-9]{10,}",            # npm access token
    r"pypi-[A-Za-z0-9_-]{10,}",         # PyPI API token
    r"dop_v1_[A-Za-z0-9]{10,}",         # DigitalOcean PAT
    r"doo_v1_[A-Za-z0-9]{10,}",         # DigitalOcean OAuth
    r"am_[A-Za-z0-9_-]{10,}",           # AgentMail API key
    r"sk_[A-Za-z0-9_]{10,}",            # ElevenLabs TTS key (sk_ underscore, not sk- dash)
    r"tvly-[A-Za-z0-9]{10,}",           # Tavily search API key
    r"exa_[A-Za-z0-9]{10,}",            # Exa search API key
    r"gsk_[A-Za-z0-9]{10,}",            # Groq Cloud API key
    r"syt_[A-Za-z0-9]{10,}",            # Matrix access token
    r"retaindb_[A-Za-z0-9]{10,}",       # RetainDB API key
    r"hsk-[A-Za-z0-9]{10,}",            # Hindsight API key
    r"mem0_[A-Za-z0-9]{10,}",           # Mem0 Platform API key
    r"brv_[A-Za-z0-9]{10,}",            # ByteRover API key
    r"xai-[A-Za-z0-9]{30,}",            # xAI (Grok) API key
    r"ntn_[A-Za-z0-9]{10,}",            # Notion internal integration token
    r"fw-[A-Za-z0-9]{30,}",             # Fireworks AI API key
    r"fw_[A-Za-z0-9]{30,}",             # Fireworks AI API key
    r"fpk_[A-Za-z0-9]{30,}",            # Fireworks AI project key
    # GitLab token families (each pattern keeps a full literal prefix so the
    # _PREFIX_SUBSTRINGS pre-screen stays false-negative-free). Ported from
    # openclaw/openclaw#112954; follow-up invited in #4541.
    r"glpat-[A-Za-z0-9_\-]{10,}",       # GitLab personal access token
    r"gloas-[A-Za-z0-9_\-]{10,}",       # GitLab OAuth application secret
    r"gldt-[A-Za-z0-9_\-]{10,}",        # GitLab deploy token
    r"glrt-[A-Za-z0-9_.\-]{10,}",       # GitLab runner authentication token (routable tokens are dotted)
    r"glrtr-[A-Za-z0-9_.\-]{10,}",      # GitLab runner registration token (routable)
    r"glcbt-[A-Za-z0-9_\-]{10,}",       # GitLab CI/CD job token
    r"glptt-[A-Za-z0-9_\-]{10,}",       # GitLab pipeline trigger token
    r"glft-[A-Za-z0-9_\-]{10,}",        # GitLab feed token
    r"glimt-[A-Za-z0-9_\-]{10,}",       # GitLab incoming mail token
    r"glagent-[A-Za-z0-9_\-]{10,}",     # GitLab agent (KAS) token
    r"glsoat-[A-Za-z0-9_\-]{10,}",      # GitLab service-account access token
    r"glffct-[A-Za-z0-9_\-]{10,}",      # GitLab feature-flags client token
    r"glwt-[A-Za-z0-9_\-]{10,}",        # GitLab workspace token
    r"GR1348941[A-Za-z0-9_\-]{10,}",    # GitLab legacy runner registration token
    r"pk-lf-[A-Za-z0-9\-]{8,}",         # Langfuse public key (sk-lf- already covered by sk- pattern)
]

#: Minimum token-body length this module will call a credential, raising the table's
#: open-ended floors (mostly ``{10,}``, one ``{8,}``) where they fall below it.
#:
#: The table above is a *redaction* table: upstream it masks substrings of log and
#: compaction text, where a false positive costs a few starred-out characters and the
#: recall is worth it. MISAKA uses it as a refusal gate, where a false positive costs the
#: whole fetch -- and, unlike Hermes, on first-party paths too. Hermes runs ``_PREFIX_RE``
#: over a URL only where the URL is about to be handed to somebody else's server
#: (``web_tools.py:1103-1106`` for the web_extract backends, ``browser_tool.py:4161``);
#: it has no first-party fetch tool for the check to reach. MISAKA's ``web_fetch`` and
#: ``download_file`` both screen through :func:`screening.screen_url`, so the same table
#: now decides whether ordinary browsing happens at all.
#:
#: At ``{10,}`` a two-to-four character prefix plus ten word characters is the shape of an
#: ordinary path slug, not of a key: ``/hsk-vocabulary``, ``/fc-bayernmunich``,
#: ``/npm_dependencies``, ``/pypi-package-listing``, ``/rk_live_documentation`` all read as
#: credentials and all are refused. Twenty is the number because no vendor in the table
#: issues a key with fewer than twenty characters of body -- the shortest are GitLab's
#: 20-character PATs and Stripe's 24 -- so raising the floor costs no detection at all
#: while taking every one of those slugs back below it.
_URL_BODY_MINIMUM = 20

#: Prefixes left at Hermes' floor. ``sk-`` is the prefix of the keys an agent actually
#: holds (OpenAI, Anthropic, OpenRouter, DeepSeek, and every ``sk-``-compatible gateway),
#: so it is the one an exfiltration attempt is likeliest to carry, and it is also the one
#: with the least to gain: the lookbehind already excludes the English words that end in
#: it (``ask-``, ``basket-``, ``risk-``, ``task-``), and a path segment beginning with a
#: bare ``sk-`` is not something ordinary URLs do.
_UNRAISED_PREFIXES = frozenset({"sk-"})

_BODY_FLOOR_RE = re.compile(r"\{(\d+),\}$")


def _url_pattern(pattern: str) -> str:
    """Return *pattern* with its open-ended body floor raised to the URL minimum.

    Keyed on the literal characters before the pattern's first character class, so
    ``sk_live_`` and ``sk_`` are distinct keys rather than one prefix of the other. A
    pattern with a fixed count (``AKIA[A-Z0-9]{16}``) or an already-sufficient floor
    (``AIza...{30,}``) comes back untouched, and so does one whose shape this no longer
    recognises -- a table update that outruns this leaves Hermes' behaviour, which is the
    safe direction to fail.
    """
    if pattern.split("[", 1)[0] in _UNRAISED_PREFIXES:
        return pattern
    floor = _BODY_FLOOR_RE.search(pattern)
    if floor is None or int(floor.group(1)) >= _URL_BODY_MINIMUM:
        return pattern
    return f"{pattern[: floor.start()]}{{{_URL_BODY_MINIMUM},}}"


# One alternation over the whole table, exactly as Hermes builds it at ``redact.py:542``.
# The boundary assertions are the difference between a detector and a nuisance: without
# the lookbehind, ``/ask-something-long`` reads as an ``sk-`` key, and without the
# lookahead a prefix would claim a token that merely starts the way one does.
_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_-])("
    + "|".join(_url_pattern(pattern) for pattern in _PREFIX_PATTERNS)
    + r")(?![A-Za-z0-9_-])"
)


def secret_in_url(url: str) -> bool:
    """Whether a credential-shaped token appears anywhere in *url*.

    Hermes checks four forms of the same string before handing a URL to a third-party
    backend (``web_tools.py:1085-1097``) and so does this, because each one alone has a
    hole. The raw string misses ``?k=%73k-abcdefghij`` -- percent-encoding the first byte
    of a key hides it from a pattern anchored on ``sk-``. Decoding alone misses nothing
    here but changes the string the request will actually carry, so the normalised form
    (what the fetch would put on the wire) is checked too, and then decoded in turn: a
    normaliser that re-encodes a byte can only move a key across the boundary, never
    remove it. Four cheap regex passes over a few hundred bytes is the whole cost.

    A credential in a URL is an exfiltration channel, not a configuration mistake: the
    key ends up in the target's access log, its referrer chain, and any third-party
    reader in between. Callers refuse the fetch rather than redacting and continuing --
    which is why the token bodies this answers True for are longer than the redactor's
    (:data:`_URL_BODY_MINIMUM`): a refusal has to be right about an ordinary URL, not
    merely cautious about one.
    """
    if not isinstance(url, str) or not url:
        return False
    normalized = normalize_url_for_request(url)
    return any(
        _PREFIX_RE.search(form)
        for form in (url, unquote(url), normalized, unquote(normalized))
    )


# Query parameter names that are unambiguously credential-bearing. Kept
# deliberately narrow: bare English words that double as normal page facets
# (``code`` on promo/challenge pages, ``key``/``auth``/``session``/``sig`` as
# search or routing params) are intentionally EXCLUDED to avoid blocking
# ordinary browsing. Prefix-based token redaction (``secret_in_url``) still
# catches recognizable vendor key shapes; this set is the belt-and-suspenders
# for opaque secrets that carry an explicit credential-named parameter.
_SENSITIVE_QUERY_PARAM_NAMES = frozenset({
    "access_token",
    "api_key",
    "apikey",
    "auth_token",
    "authorization",
    "awsaccesskeyid",
    "client_secret",
    "credential",
    "credentials",
    "jwt",
    "password",
    "passwd",
    "secret",
    "session_id",
    "signature",
    "token",
    "x_amz_security_token",
    "x_amz_signature",
    "x-amz-security-token",
    "x-amz-signature",
})


def sensitive_query_param_name(url: str) -> str | None:
    """Return the first sensitive query parameter name in *url*, if any.

    Hermes' ``sensitive_query_param_name`` (``url_safety.py:137-156``). Used before
    handing a URL to a third-party fetch/browser backend. :func:`secret_in_url` catches
    known credential *shapes*; this catches the opaque ones -- magic links, OAuth codes,
    signed-URL signatures, a custom ``?token=...`` with no vendor prefix -- by the name
    the parameter was given.

    A blank value is not a credential (``?token=`` is a form that was never filled in),
    so it does not trip this and cost the user a fetch that leaks nothing.
    """
    if not isinstance(url, str) or "?" not in url:
        return None
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.query:
        return None
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if value and unquote(key).lower() in _SENSITIVE_QUERY_PARAM_NAMES:
            return key
    return None


def has_sensitive_query_params(url: str) -> bool:
    """Whether *url* carries likely credential-bearing query params."""
    return sensitive_query_param_name(url) is not None


# Hostnames that are always blocked regardless of IP resolution or any config toggle.
# These are cloud metadata endpoints an attacker could use to steal instance credentials,
# and they are checked by *name* because the name is enough: a resolver that answers for
# ``metadata.google.internal`` has already told you what the caller was reaching for.
_BLOCKED_HOSTNAMES = frozenset({
    "metadata.google.internal",
    "metadata.goog",
})

# IPs and networks that should always be blocked regardless of the
# allow_private_urls toggle.  These are cloud metadata / credential
# endpoints -- the #1 SSRF target -- and the link-local range where
# they all live.
#
# IPv4-mapped IPv6 variants are included because DNS resolvers may
# return ``::ffff:x.x.x.x`` for IPv4-only hosts, and Python's
# ipaddress module treats these as distinct from the plain IPv4
# address (they won't match ``ip in frozenset`` or ``ip in network``).
_ALWAYS_BLOCKED_IPS = frozenset({
    ipaddress.ip_address("169.254.169.254"),   # AWS/GCP/Azure/DO/Oracle metadata
    ipaddress.ip_address("169.254.170.2"),     # AWS ECS task metadata (task IAM creds)
    ipaddress.ip_address("169.254.169.253"),   # Azure IMDS wire server
    ipaddress.ip_address("fd00:ec2::254"),     # AWS metadata (IPv6)
    ipaddress.ip_address("100.100.100.200"),   # Alibaba Cloud metadata
    # IPv4-mapped IPv6 variants -- same endpoints reachable via ::ffff:x.x.x.x
    ipaddress.ip_address("::ffff:169.254.169.254"),
    ipaddress.ip_address("::ffff:169.254.170.2"),
    ipaddress.ip_address("::ffff:169.254.169.253"),
    ipaddress.ip_address("::ffff:100.100.100.200"),
})
_ALWAYS_BLOCKED_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),         # Entire link-local range (no legit agent target)
    ipaddress.ip_network("::ffff:169.254.0.0/112"), # IPv4-mapped link-local range
)

#: 100.64.0.0/10 (CGNAT / Shared Address Space, RFC 6598). Exported because
#: ``ipaddress`` reports it as neither private nor global, so a caller that trusts
#: ``is_private`` alone -- which is every obvious way to write the check -- will dial
#: straight into a carrier NAT, a Tailscale/WireGuard mesh, or a cloud internal network.
#: It has to be named explicitly or it is not blocked at all.
CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def always_blocked_host(host: str) -> bool:
    """Whether *host* is a cloud-metadata hostname that no toggle may unblock.

    Normalised before comparing -- lowercased, trimmed, trailing root dot removed --
    because ``Metadata.Google.Internal.`` is the same request as the lowercase form and a
    set membership test would happily say otherwise.
    """
    return str(host or "").strip().lower().rstrip(".") in _BLOCKED_HOSTNAMES


def always_blocked_address(
    value: ipaddress.IPv4Address | ipaddress.IPv6Address | str,
) -> bool:
    """Whether *value* is a cloud-metadata or link-local address that stays blocked.

    This is the floor: it holds even when the operator has set
    :func:`allow_private_urls`, because the reasons to allow private addressing -- a
    router's admin page, a VPN-internal service, a benchmark-range corporate resolver --
    never include reading an instance's IAM credentials out of ``169.254.169.254``.

    Takes either an ``ipaddress`` object (what a caller that already parsed a resolver
    answer has) or a string (what a caller holding a URL's host has). An IPv6 scope id is
    stripped first: ``::ffff:169.254.169.254%eth0`` is the metadata endpoint, and the
    ``%`` suffix is exactly the sort of decoration that turns a blocked address into an
    unparseable one and then into an allowed one. A string that is not an address at all
    is not blocked here -- it is a *name*, which is :func:`always_blocked_host`'s
    question and, after that, the resolver's.
    """
    if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        address = value
    else:
        try:
            address = ipaddress.ip_address(str(value).strip().split("%", 1)[0])
        except ValueError:
            return False
    return address in _ALWAYS_BLOCKED_IPS or any(
        address in network for network in _ALWAYS_BLOCKED_NETWORKS
    )


# Strings a config value may spell "yes" with. Hermes' ``TRUTHY_STRINGS``; the point of
# comparing against a set rather than calling ``bool()`` is that ``bool("false")`` is
# True, and a security toggle that reads the string ``"false"`` as "allow" is the worst
# possible way to be wrong.
_TRUTHY_STRINGS = frozenset({"1", "true", "yes", "on"})


def allow_private_urls() -> bool:
    """Whether the operator has opted out of private-address blocking.

    Hermes' ``_global_allow_private_urls``. Some networks resolve perfectly ordinary
    external names to private or benchmark-range addresses -- OpenWrt routers, corporate
    split-horizon DNS, VPNs on 198.18.0.0/15 or 100.64.0.0/10 -- and for those users the
    SSRF check is not protection, it is a wall across every fetch. The floor
    (:func:`always_blocked_host`, :func:`always_blocked_address`) stays shut regardless.

    Priority: ``MISAKA_ALLOW_PRIVATE_URLS`` in the environment first, and an explicit
    false there does NOT fall through to the config file -- an operator disabling this
    for one process is answering the question, not declining to answer it. Otherwise the
    ``allow_private_urls`` key in ``~/.misaka/web.json``.

    Deliberately not cached, unlike Hermes, which memoises for the process lifetime.
    Every other read of ``web.json`` in MISAKA is uncached and takes effect on the next
    call (see :func:`misaka.core.web.config.web_config`), and a security toggle
    that alone ignores the file the user just edited -- with no way to tell from the
    outside that a restart is what is missing -- is a support nightmare. The cost is one
    stat and a few hundred bytes of JSON next to a network round trip.

    Never raises: an unreadable or malformed config means False, the safe answer.
    """
    raw = os.getenv("MISAKA_ALLOW_PRIVATE_URLS", "").strip().lower()
    if raw in _TRUTHY_STRINGS:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False

    try:
        value = web_config().get("allow_private_urls")
    except Exception as error:  # noqa: BLE001 - a missing or broken config must not break a fetch
        logger.debug("allow_private_urls: config unreadable (%s); staying closed", error)
        return False
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY_STRINGS
    return bool(value)


__all__ = [
    "CGNAT_NETWORK",
    "allow_private_urls",
    "always_blocked_address",
    "always_blocked_host",
    "has_sensitive_query_params",
    "normalize_url_for_request",
    "secret_in_url",
    "sensitive_query_param_name",
]
