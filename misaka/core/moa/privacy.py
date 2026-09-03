"""The MoA privacy filter: what an advisor's answer may carry out of the conversation.

Port of Hermes ``hermes_cli/moa_config.coerce_privacy_filter`` and the reference/trace
redactors at the top of ``agent/moa_loop.py`` (local checkout ``~/.hermes/hermes-agent``).

Advisor models are handed the conversation and answer in prose, so their answers can echo
back whatever the user pasted -- an address, a phone number, an API key -- into surfaces
the user did not expect it to reach. Hermes redacts that on the way out, in two strengths:

* ``""`` -- off, and the default. Nothing is touched.
* ``"display"`` -- the surfaces that are kept rather than consumed. In Hermes those are
  the labelled reference blocks it renders and its saved traces; MISAKA renders no
  reference blocks (advisor text is private input to the aggregator, never shown), so
  here it means the MoA trace files. The aggregator still reads the raw advice, so the
  answer is unaffected.
* ``"full"`` -- additionally the advisor block injected into the aggregator's prompt.

What gets redacted: the credential shapes Hermes's central redactor knows -- its
``_PREFIX_PATTERNS`` (below, verbatim), private-key blocks, database URLs, JWTs and E.164
numbers -- and then the two PII classes that redactor deliberately leaves alone in tool
output, e-mail addresses and formatted phone numbers, which is what the MoA-specific
patterns are for. MISAKA has no central redactor to defer to, so the shapes ride here;
the list is Hermes's and resyncs against it.

Pattern safety, in Hermes' words: advisory text is frequently code-review-shaped -- line
numbers, timestamps, git SHAs, IDs, IP addresses. A bare ten-digit match would mangle all
of those, so the phone pattern requires clearly delimited formatting. Undelimited digit
runs, dates, times, hex ids and dotted quads never match.
"""

from __future__ import annotations

import re
from typing import Any

# Hermes agent/redact.py ``_PREFIX_PATTERNS``, verbatim.
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

_PREFIX_RE = re.compile(r"\b(" + "|".join(_PREFIX_PATTERNS) + r")")
_PREFIX_SUBSTRINGS = tuple(
    sorted({re.match(r"[A-Za-z0-9_.+-]*", pattern).group(0) for pattern in _PREFIX_PATTERNS} - {""},
           key=len, reverse=True)
)

# Hermes agent/redact.py: private-key blocks, database URLs, JWTs, E.164 numbers.
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"
)
_DB_CONNSTR_RE = re.compile(
    r"((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:\s]+:)([^@\s]+)(@)",
    re.IGNORECASE,
)
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_=-]{4,}){0,2}")
_E164_RE = re.compile(r"(\+[1-9]\d{6,14})(?![A-Za-z0-9])")

# Hermes agent/moa_loop.py, the two classes the central redactor leaves alone.
_MOA_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_MOA_PHONE_RE = re.compile(
    r"(?<![\w.+-])"                    # no leading word char / dot / + / - (kills IPs, IDs, versions)
    r"(?:\+?1[ .-])?"                  # optional NA country code
    r"(?:\(\d{3}\)[ .-]?|\d{3}[.-])"   # delimited area code: (555) or 555- / 555.
    r"\d{3}[.-]\d{4}"                  # exchange-subscriber with explicit separator
    r"(?![\w-])"                       # no trailing word char / hyphen
)

MODES = ("", "display", "full")


def coerce_privacy_filter(value: Any) -> str:
    """Normalise ``moa.privacy_filter`` to ``""`` (off), ``"display"`` or ``"full"``.

    ``false``/``None``/unknown land on off so a hand-edited config degrades to the previous
    behaviour, and a bare ``true`` maps to ``full`` because that is how the toggle was
    first asked for (Hermes ``coerce_privacy_filter``).
    """
    if value is True:
        return "full"
    if value is None or value is False:
        return ""
    mode = str(value).strip().lower()
    if mode in {"display", "full"}:
        return mode
    if mode in {"true", "on", "yes", "1"}:
        return "full"
    return ""


def _mask_prefixed(token: str) -> str:
    """A credential redacted to something that cannot be mistaken for a usable key.

    Hermes ``_mask_token_nonreusable``: keep the vendor label so a reader can tell *what*
    was there, never any of the random body -- an agent that reads this out of a file and
    writes it back must not turn the stored credential into a dead truncated string.
    """
    if not token:
        return "\u00abredacted-secret\u00bb"
    for label in _PREFIX_SUBSTRINGS:
        if token.startswith(label):
            return f"\u00abredacted:{label}\u2026\u00bb"
    return "\u00abredacted-secret\u00bb"


def _mask_token(token: str) -> str:
    """Hermes ``mask_secret(head=6, tail=4, floor=18)``: enough to recognise, not to reuse."""
    if not token:
        return "***"
    return f"{token[:6]}...{token[-4:]}" if len(token) >= 18 else "***"


def _mask_e164(match: re.Match[str]) -> str:
    phone = match.group(1)
    if len(phone) <= 8:
        return phone[:2] + "****" + phone[-2:]
    return phone[:4] + "****" + phone[-4:]


def redact_advisor_text(text: Any) -> Any:
    """Redact secrets and PII from one advisor text. Non-strings pass through unchanged."""
    if not isinstance(text, str) or not text:
        return text
    text = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)
    text = _PREFIX_RE.sub(lambda m: _mask_prefixed(m.group(1)), text)
    text = _JWT_RE.sub(lambda m: _mask_token(m.group(0)), text)
    text = _DB_CONNSTR_RE.sub(lambda m: f"{m.group(1)}***{m.group(3)}", text)
    text = _E164_RE.sub(_mask_e164, text)
    text = _MOA_EMAIL_RE.sub("[redacted email]", text)
    return _MOA_PHONE_RE.sub("[redacted phone]", text)


def redact_outputs(outputs: list[tuple[str, str, Any]]) -> list[tuple[str, str, Any]]:
    """``(label, text, usage)`` advisor tuples with the text redacted; usage is untouched."""
    return [(label, redact_advisor_text(text), usage) for label, text, usage in outputs]


__all__ = [
    "MODES",
    "coerce_privacy_filter",
    "redact_advisor_text",
    "redact_outputs",
]
