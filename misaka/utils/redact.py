"""Regex-based secret redaction for text the board keeps (ported from Hermes agent/redact.py, MIT).

A Sister's summary, a card note, a block reason or a failure message is written into the
board's ledger, the card file and, through it, git history. Whatever credential a tool
echoed into that text would otherwise be kept for good. ``redact`` masks the shapes a
credential takes: vendor-prefixed tokens, ``KEY=value`` and ``"key": "value"`` assignments
whose name says secret, Authorization and API-key headers, private key blocks, database
connection-string passwords, bare-token URL userinfo and JWTs. Long tokens keep a head and
tail for debugging; short ones are masked whole.

Deliberately not ported: Hermes's phone-number pass (a research note may cite a real phone
number), its opt-in query-string redaction (round-trip links must survive) and its env
kill-switch (board writes are a safety boundary, so this always runs).
"""

from __future__ import annotations

import re

# Known API key prefixes: the prefix plus contiguous token chars. Every pattern starts with a
# literal prefix; ``_PREFIX_SUBSTRINGS`` (the cheap pre-screen) is derived from those literals.
_PREFIX_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{10,}",           # OpenAI / OpenRouter / Anthropic (sk-ant-*)
    r"ghp_[A-Za-z0-9]{10,}",            # GitHub PAT (classic)
    r"github_pat_[A-Za-z0-9_]{10,}",    # GitHub PAT (fine-grained)
    r"gho_[A-Za-z0-9]{10,}",            # GitHub OAuth access token
    r"ghu_[A-Za-z0-9]{10,}",            # GitHub user-to-server token
    r"ghs_[A-Za-z0-9]{10,}",            # GitHub server-to-server token
    r"ghr_[A-Za-z0-9]{10,}",            # GitHub refresh token
    r"xapp-\d+-[A-Za-z0-9-]{10,}",      # Slack app-level token
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
    r"glpat-[A-Za-z0-9_\-]{10,}",       # GitLab personal access token
    r"gloas-[A-Za-z0-9_\-]{10,}",       # GitLab OAuth application secret
    r"gldt-[A-Za-z0-9_\-]{10,}",        # GitLab deploy token
    r"glrt-[A-Za-z0-9_.\-]{10,}",       # GitLab runner authentication token
    r"glrtr-[A-Za-z0-9_.\-]{10,}",      # GitLab runner registration token
    r"glcbt-[A-Za-z0-9_\-]{10,}",       # GitLab CI/CD job token
    r"glptt-[A-Za-z0-9_\-]{10,}",       # GitLab pipeline trigger token
    r"glft-[A-Za-z0-9_\-]{10,}",        # GitLab feed token
    r"glimt-[A-Za-z0-9_\-]{10,}",       # GitLab incoming mail token
    r"glagent-[A-Za-z0-9_\-]{10,}",     # GitLab agent (KAS) token
    r"glsoat-[A-Za-z0-9_\-]{10,}",      # GitLab service-account access token
    r"glffct-[A-Za-z0-9_\-]{10,}",      # GitLab feature-flags client token
    r"glwt-[A-Za-z0-9_\-]{10,}",        # GitLab workspace token
    r"GR1348941[A-Za-z0-9_\-]{10,}",    # GitLab legacy runner registration token
    r"pk-lf-[A-Za-z0-9\-]{8,}",         # Langfuse public key (sk-lf- is covered by sk-)
]
_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(" + "|".join(_PREFIX_PATTERNS) + r")(?![A-Za-z0-9_-])"
)


def _literal_prefix(pattern):
    for index, char in enumerate(pattern):
        if char in "[(\\.?*+|{^$":
            return pattern[:index]
    return pattern


_PREFIX_SUBSTRINGS = tuple(_literal_prefix(p) for p in _PREFIX_PATTERNS)

# ``KEY=value`` where the key says secret. An all-caps key is almost never prose, so the
# keyword may sit anywhere in it; a lowercase key needs the keyword at an underscore boundary,
# since bare ``password=`` / ``token=`` also appear in prose, URLs and form bodies.
_SECRET_ENV_NAMES = r"(?:API_?KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|PW|CREDENTIAL|AUTH)"
_ENV_ASSIGN_RE = re.compile(
    rf"([A-Z0-9_]{{0,50}}{_SECRET_ENV_NAMES}[A-Z0-9_]{{0,50}})\s*=\s*(['\"]?)(\S+)\2"
)
_ENV_ASSIGN_LOWER_RE = re.compile(
    r"(?<![a-z0-9_])([a-z0-9_]+(?:_|^)(?:key|pass|pw|token|secret|password|passwd|credential|auth)"
    r"(?=[^a-z0-9_]|$))\s*=\s*(['\"]?)(\S+)\2",
    re.IGNORECASE,
)
# Dotted config keys (``spring.datasource.password=x``) and line-start bare keys
# (``password=x``, ``export api_key=x``); values stop at whitespace and ``&``.
_SECRET_CFG_NAMES = r"(?:api[ _.\-]?key|token|secret|passwd|password|credential|auth)"
_CFG_VALUE = r"(['\"]?)([^\s&]+?)\2(?=[\s&]|$)"
_CFG_SECRET_WORD_RE = re.compile(_SECRET_CFG_NAMES, re.IGNORECASE)
_CFG_DOTTED_RE = re.compile(
    rf"(?<![A-Za-z0-9_.\-])"
    rf"([A-Za-z0-9_\-]++\.[A-Za-z0-9_.\-]*{_SECRET_CFG_NAMES}[A-Za-z0-9_.\-]*+"
    rf"|[A-Za-z0-9_.\-]*{_SECRET_CFG_NAMES}[A-Za-z0-9_.\-]*\.[A-Za-z0-9_.\-]++)"
    rf"={_CFG_VALUE}",
    re.IGNORECASE,
)
_CFG_ANCHORED_RE = re.compile(
    rf"(^[ \t]*(?:export[ \t]+)?[A-Za-z0-9_\-]*{_SECRET_CFG_NAMES}[A-Za-z0-9_\-]*)={_CFG_VALUE}",
    re.IGNORECASE | re.MULTILINE,
)
# Unquoted YAML (``password: secret``): a keyword in a line-start key and one bare value, so
# ``note: secret meeting`` is left alone. Bare ``auth`` is excluded so ``author:`` never matches.
_YAML_CFG_NAMES = r"(?:api[ _.\-]?key|token|secret|passwd|password|credential)"
_YAML_ASSIGN_RE = re.compile(
    rf"(^[ \t]*+[A-Za-z0-9_.\-]*{_YAML_CFG_NAMES}[A-Za-z0-9_.\-]*+)(:[ \t]*+)(?!['\"])([^\s&]++)",
    re.IGNORECASE | re.MULTILINE,
)
# A keyword counts only at a word boundary (``client_secret``, ``clientSecret``, ``APIToken``),
# so prose that merely contains one (``Secretary``, ``tokenizer``, ``authored``) is left alone.
_KEY_KEYWORD_RE = re.compile(
    r"(?:api|auth|access|refresh|session|secret)[ _.\\-]?(?:key|token)"
    r"|token|secret|passwd|password|pass|pw|credential|auth|key",
    re.IGNORECASE,
)
# Keys that are credentials even when the value is short; bare ``token``/``key`` are absent
# because they also name model limits and cache keys, so those depend on the value's shape.
_STRONG_KEY_KEYWORD_RE = re.compile(
    r"(?:api|auth|access|refresh|session|id|bearer)[ _.\\-]?(?:key|token)"
    r"|key[ _.\\-]?material|secret|passwd|password|pass|pw|credential|auth|bearer",
    re.IGNORECASE,
)
_ENV_LOOKUP_VALUE_RE = re.compile(r"^(?:os\.(?:getenv|environ)|process\.env|\$ENV\{)")
_JSON_KEY_NAMES = (
    r"(?:api_?[Kk]ey|token|secret|password|access_token|refresh_token|auth_token|bearer"
    r"|secret_value|raw_secret|secret_input|key_material)"
)
_JSON_FIELD_RE = re.compile(rf'("{_JSON_KEY_NAMES}")\s*:\s*"([^"]+)"', re.IGNORECASE)
# Header credentials; the value class excludes quotes so a mask never eats a closing quote.
_AUTH_HEADER_RE = re.compile(
    r"((?:Proxy-)?Authorization:\s*)([A-Za-z][\w.+-]*\s+)?([^\s\"']+)", re.IGNORECASE
)
_SECRET_HEADER_NAMES = (
    r"(?:x-api-key|x-goog-api-key|api-key|apikey|x-api-token|x-auth-token|x-access-token)"
)
_SECRET_HEADER_RE = re.compile(rf"({_SECRET_HEADER_NAMES}\s*:\s*)(\S+)", re.IGNORECASE)
_TELEGRAM_RE = re.compile(r"(bot)?(\d{8,}):([-A-Za-z0-9_]{30,})")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"
)
# ``scheme://user:PASSWORD@host``; the password never spans whitespace, so a match never runs
# on to a decorator's ``@`` on the next line.
_DB_CONNSTR_RE = re.compile(
    r"((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:\s]+:)([^@\s]+)(@)",
    re.IGNORECASE,
)
# ``scheme://TOKEN@host``: a bare credential in userinfo is never a round-trip link token.
# ``user:pass@`` passes through (the class forbids ``:``), as web query strings do.
_URL_BARE_TOKEN_RE = re.compile(
    r"((?:https?|wss?|git|ssh|ftp|ftps|sftp)://)([^\s:@/]{8,})(@[^\s]+)", re.IGNORECASE
)
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_=-]{4,}){0,2}")
_FORM_BODY_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.-]*=[^&\s]*(?:&[A-Za-z_][A-Za-z0-9_.-]*=[^&\s]*)+$"
)
_SENSITIVE_QUERY_PARAMS = frozenset({
    "access_token", "refresh_token", "id_token", "token", "api_key", "apikey",
    "client_secret", "password", "auth", "jwt", "session", "secret", "key",
    "code", "signature", "x-amz-signature",
})
# Control and zero-width characters that can split a token body (``sk-abc\x1bdef``) and slip
# past the contiguous prefix patterns.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u2028-\u202f\u2060\ufeff]")
_TOKEN_BODY_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-."
)
_DISPLAY_CONTROL_RE = re.compile(
    r"[\x00-\x1f\x7f\x80-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2064]"
)


def mask_secret(value, *, head=4, tail=4, floor=12, placeholder="***", empty=""):
    """``sk-p...7890``; shorter than ``floor`` becomes ``placeholder``; falsy becomes ``empty``."""
    value = _DISPLAY_CONTROL_RE.sub("", value) if value else value
    if not value:
        return empty
    return placeholder if len(value) < floor else f"{value[:head]}...{value[-tail:]}"


def _mask_token(token):
    if not token:
        return "***"
    return mask_secret(token, head=6, tail=4, floor=18)


def _is_word_start(text, index):
    if index == 0:
        return True
    prev, cur = text[index - 1], text[index]
    if not prev.isalpha() or (cur.isupper() and prev.islower()):
        return True
    return cur.isupper() and prev.isupper() and index + 1 < len(text) and text[index + 1].islower()


def _is_word_end(text, index, *, allow_plural=True):
    if index >= len(text):
        return True
    cur = text[index]
    if not cur.isalpha() or (cur.isupper() and text[index - 1].islower()):
        return True
    return allow_plural and cur in "sS" and _is_word_end(text, index + 1, allow_plural=False)


def _has_word_bounded_keyword(key, keyword_re):
    return any(
        _is_word_start(key, m.start()) and _is_word_end(key, m.end())
        for m in keyword_re.finditer(key)
    )


def _looks_like_opaque_credential(value):
    """Credential-shaped values, so a short technical scalar (``CPU``, ``local``) is not masked
    merely for its key name."""
    if value == "***":
        return True
    if len(value) >= 16 and re.fullmatch(r"[A-Fa-f0-9]+", value):
        return True
    if len(value) >= 20 and re.fullmatch(r"[A-Za-z0-9_./+=-]+", value):
        return True
    if len(value) < 12:
        return False
    return sum(bool(re.search(p, value)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]")) >= 2


def _should_redact_assignment(key, value, *, check_keyword):
    if _ENV_LOOKUP_VALUE_RE.match(value):       # ``KEY=os.getenv('X')`` names a variable
        return False
    if check_keyword and not _has_word_bounded_keyword(key, _KEY_KEYWORD_RE):
        return False
    return (_has_word_bounded_keyword(key, _STRONG_KEY_KEYWORD_RE)
            or _looks_like_opaque_credential(value))


def _assignment_sub(render, *, check_keyword):
    def substitute(match):
        groups = match.groups()
        if not _should_redact_assignment(groups[0], groups[-1], check_keyword=check_keyword):
            return match.group(0)
        return render(groups)
    return substitute


def _redact_assignments(text):
    if "=" in text:
        env = _assignment_sub(
            lambda g: f"{g[0]}={g[1]}{_mask_token(g[2])}{g[1]}", check_keyword=True
        )
        text = _ENV_ASSIGN_RE.sub(env, text)
        if "://" not in text:                   # lowercase names would match URL params
            text = _ENV_ASSIGN_LOWER_RE.sub(env, text)
        # The keyword pre-gate is exact: every config pattern needs one, and the dotted pattern
        # backtracks quadratically on long unbroken runs (base64, hex) in secret-free text.
        if "://" not in text and _CFG_SECRET_WORD_RE.search(text):
            text = _CFG_DOTTED_RE.sub(env, text)
            text = _CFG_ANCHORED_RE.sub(env, text)
    if ":" in text and '"' in text:
        text = _JSON_FIELD_RE.sub(
            _assignment_sub(lambda g: f'{g[0]}: "{_mask_token(g[1])}"', check_keyword=False),
            text,
        )
    if ":" in text and "://" not in text:       # YAML after JSON: quoted values are handled there
        text = _YAML_ASSIGN_RE.sub(
            _assignment_sub(lambda g: f"{g[0]}{g[1]}{_mask_token(g[2])}", check_keyword=True),
            text,
        )
    return text


def _mask_control_split_tokens(text):
    """Mask tokens whose body is split by control or zero-width characters, matching on a
    stripped copy and masking the span in the original only when that span holds nothing but
    token-body and control characters."""
    stripped = _CONTROL_CHARS_RE.sub("", text)
    if stripped == text:
        return text
    original_index = [i for i, c in enumerate(text) if not _CONTROL_CHARS_RE.match(c)]
    out, matches = list(text), []
    for match in _PREFIX_RE.finditer(stripped):
        start = original_index[match.start(1)]
        end = original_index[match.end(1) - 1] + 1
        span = text[start:end]
        if ("\n" in span or "\r" in span) and _PREFIX_RE.search(span):
            continue                            # a whole token before the line break: leave the rest
        if (all(c in _TOKEN_BODY_CHARS or _CONTROL_CHARS_RE.match(c) for c in span)
                and (end >= len(text) or text[end] != "=")):
            matches.append((start, end, _mask_token(match.group(1))))
    for start, end, replacement in reversed(matches):
        out[start:end] = list(replacement)
    return "".join(out)


def _redact_query_string(query):
    return "&".join(
        f"{key}=***" if sep and key.lower() in _SENSITIVE_QUERY_PARAMS else pair
        for pair in query.split("&") for key, sep, _ in (pair.partition("="),)
    )


def _redact_form_body(text):
    """Sensitive values when the whole text is one ``k=v&k=v`` body."""
    if not text or "\n" in text or "&" not in text or not _FORM_BODY_RE.match(text.strip()):
        return text
    return _redact_query_string(text.strip())


def redact(text):
    """Mask every credential shape in ``text``; safe on any value, None stays None.

    Every pattern sits behind a substring its match needs, so secret-free text costs a few
    scans and nothing else.
    """
    if text is None:
        return None
    text = text if isinstance(text, str) else str(text)
    if not text:
        return text
    if any(prefix in text for prefix in _PREFIX_SUBSTRINGS):
        text = _mask_control_split_tokens(text)
        text = _PREFIX_RE.sub(lambda m: _mask_token(m.group(1)), text)
    text = _redact_assignments(text)
    if "uthorization" in text or "UTHORIZATION" in text:
        text = _AUTH_HEADER_RE.sub(
            lambda m: m.group(1) + (m.group(2) or "") + _mask_token(m.group(3)), text
        )
    if ":" in text:
        text = _SECRET_HEADER_RE.sub(lambda m: m.group(1) + _mask_token(m.group(2)), text)
        text = _TELEGRAM_RE.sub(lambda m: f"{m.group(1) or ''}{m.group(2)}:***", text)
    if "BEGIN" in text and "-----" in text:
        text = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)
    if "://" in text:
        text = _DB_CONNSTR_RE.sub(lambda m: f"{m.group(1)}***{m.group(3)}", text)
        text = _URL_BARE_TOKEN_RE.sub(
            lambda m: f"{m.group(1)}{_mask_token(m.group(2))}{m.group(3)}", text
        )
    if "eyJ" in text:
        text = _JWT_RE.sub(lambda m: _mask_token(m.group(0)), text)
    if "&" in text and "=" in text:
        text = _redact_form_body(text)
    return text


def redact_payload(payload):
    """``redact`` applied through a JSON-shaped payload: strings in place, structure kept."""
    if isinstance(payload, str):
        return redact(payload)
    if isinstance(payload, dict):
        return {key: redact_payload(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [redact_payload(value) for value in payload]
    return payload
