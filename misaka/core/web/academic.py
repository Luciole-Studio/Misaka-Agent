"""Send an academic URL to the entry point that actually serves the text.

A publisher's landing page is not the paper. ScienceDirect and its peers answer an
anonymous GET with a sign-in prompt, a PubMed record is the abstract only, and an
arXiv ``/pdf/`` URL is a binary a text fetcher cannot read -- while the same work
frequently sits in an open repository under a URL a plain fetch reads fine. This
module is the lookup table between the two.

Pure and I/O-free on purpose. Two things follow from that: it is cheap enough for
the fetcher to call again on the URL a redirect chain actually landed on (a
``doi.org`` link only reveals its publisher after the hop), and every decision here
is a guess about a URL shape, never a claim that the rewritten target exists. The
caller fetches the rewrite and falls back to reporting the failure; nothing here
promises success.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import httpx

RouteKind = Literal["none", "arxiv", "biorxiv", "paywall", "pmc", "pubmed"]


@dataclass(frozen=True, slots=True)
class AcademicRoute:
    """Where to fetch, plus what the model needs to know about that choice.

    ``kind == "none"`` means the URL is not one this table recognises: ``url`` is
    the input unchanged and ``note`` is empty. Otherwise ``note`` is one paragraph
    to show alongside the fetched body -- it names the fallback to try when the
    rewrite comes back empty, which is the part the model cannot work out alone.
    """

    url: str
    kind: RouteKind
    note: str


# borrowed from FrontierAgent (plugins/tools/_academic_fetch.py PAYWALL_DOMAINS), a
# list its operators accumulated from fetches that came back as login walls; pending
# W21 local data. nature.com and link.springer.com are added here, not from that list.
#
# ponytail: entries are matched as domain suffixes, so one `onlinelibrary.wiley.com`
# replaces the six Wiley subdomains FA enumerates and covers the ones it has not met
# yet. The cost is that a genuinely open subdomain of a listed publisher gets the
# warning too -- the warning is advisory (the fetch still happens), so that is cheap.
_PAYWALL_DOMAINS: frozenset[str] = frozenset({
    "academic.oup.com",
    "ashpublications.org",
    "cell.com",
    "journals.aps.org",
    "jneurosci.org",
    "link.aps.org",
    "link.springer.com",
    "linkinghub.elsevier.com",
    "nature.com",
    "onlinelibrary.wiley.com",
    "pubs.acs.org",
    "pubs.aip.org",
    "rsc.org",
    "sagepub.com",
    "science.org",
    "sciencedirect.com",
    "tandfonline.com",
})

_PMCID_RE = re.compile(r"(PMC\d+)", re.IGNORECASE)
_PUBMED_ID_RE = re.compile(r"^/(\d+)")
_ARXIV_RE = re.compile(r"^/(abs|pdf|html|format)/(.+)$")
# An arXiv id is `2401.12345v2` or `math/0309136`. Validated as an allowlist because
# the id is spliced back into a URL that will then be fetched, and httpx.URL.path
# hands back percent-decoded text: `%3F` in the input arrives here as a real `?`.
# The length bound is the second half of that guard: a real id is under 30 characters,
# and the id is echoed into a note that goes into the model's context.
_ARXIV_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9./_-]{0,63}")
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s?#]+")

# The BioC endpoint is NCBI's machine-readable view of the same article. It answers
# programmatic clients that the article page's bot defences turn away.
_BIOC_URL = (
    "https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/pmcoa.cgi/BioC_json/{pmcid}/unicode"
)

# A DOI has no length limit, and this one goes into the model's context.
_MAX_DOI_CHARS = 200

# Bounds on the two other pieces of remote text this module echoes into a note.
# The fetcher routes again on the URL a redirect landed on, so an attacker picks
# that string: without a bound, `Location: https://<15KB of a>.nature.com/` buys
# 15KB of the model's context in a position the model reads as ours, not as page
# content. 253 is the longest legal DNS name; a PMC id is 7-8 digits today.
_MAX_HOST_CHARS = 253
_MAX_PMCID_CHARS = 16

# bioRxiv view suffixes, longest first so `.full.pdf` is stripped before `.pdf`.
# Bytes because the rewrite works on the encoded path; see _preprint.
_PREPRINT_VIEWS = (b".full.pdf", b".full-text", b".full", b".pdf", b".article-info")


def _is_domain(host: str, domain: str) -> bool:
    """Whether *host* is *domain* or one of its subdomains."""
    return host == domain or host.endswith("." + domain)


def _extract_doi(url: str) -> str:
    """The DOI embedded in *url*, or ``""``. Best-effort: a DOI's suffix is opaque."""
    match = _DOI_RE.search(url)
    if not match:
        return ""
    return match.group(0).rstrip("/.,;)")[:_MAX_DOI_CHARS]


def _pmc(parsed: httpx.URL, url: str) -> AcademicRoute:
    match = _PMCID_RE.search(parsed.path)
    if not match:
        return AcademicRoute(url, "none", "")
    pmcid = match.group(1).upper()
    if len(pmcid) > _MAX_PMCID_CHARS:
        return AcademicRoute(url, "none", "")
    target = str(
        parsed.copy_with(
            scheme="https",
            host="pmc.ncbi.nlm.nih.gov",
            port=None,
            path=f"/articles/{pmcid}/",
            query=None,
            fragment=None,
            # This is the one rewrite that changes host, so it is the one that would
            # carry a "user:pass@" across origins. vet_public_url refuses credentials
            # outright, but a rewriter must not be the thing that moves them.
            userinfo=b"",
        )
    )
    return AcademicRoute(
        target,
        "pmc",
        "PubMed Central serves the full text of open-access articles. If this page comes back "
        "empty, blocked, or as a bot check, the same article is available as machine-readable "
        f"text at {_BIOC_URL.format(pmcid=pmcid)} -- for the open-access subset only, so an "
        "error there means the full text is not free, not that the fetch failed.",
    )


def _pubmed(parsed: httpx.URL, url: str) -> AcademicRoute:
    if not _PUBMED_ID_RE.match(parsed.path):
        return AcademicRoute(url, "none", "")
    return AcademicRoute(
        url,
        "pubmed",
        "A PubMed record is the abstract and metadata only -- never quote it as the paper's "
        "full text. The page links onward to the full text: a PMC link means a free copy "
        "exists, and following it is the way to reach the body of the article.",
    )


def _preprint(parsed: httpx.URL, url: str) -> AcademicRoute:
    """bioRxiv/medRxiv: the landing page is the abstract, ``.full`` is the article."""
    # raw_path (encoded, carries the query) rather than path (percent-decoded): the
    # stem is spliced straight back into a URL, and decoded text does not survive
    # that trip. A decoded "?" or "#" is not a legal path character and makes
    # copy_with raise; a decoded "%2F" silently becomes a segment separator, so the
    # fetch would go somewhere other than the URL the caller asked about.
    stem = parsed.raw_path.split(b"?", 1)[0].rstrip(b"/")
    for view in _PREPRINT_VIEWS:
        if stem.endswith(view):
            stem = stem[: -len(view)]
            break
    # Requiring a non-empty remainder after "/content/" is what keeps the bare section
    # index (".../content/") from being rewritten to a ".../content.full" that is not a
    # URL of anything.
    _, _, article = stem.partition(b"/content/")
    if not article:
        return AcademicRoute(url, "none", "")
    target = str(parsed.copy_with(raw_path=stem + b".full", fragment=None))
    return AcademicRoute(
        target,
        "biorxiv",
        "Rewritten to the preprint's full-text HTML; the URL as given shows the abstract only. "
        "This is a preprint: it has not been peer reviewed, and any claim taken from it should "
        "be reported as such.",
    )


def _arxiv(parsed: httpx.URL, url: str) -> AcademicRoute:
    match = _ARXIV_RE.match(parsed.path)
    if not match:
        return AcademicRoute(url, "none", "")
    section = match.group(1)
    identifier = match.group(2).rstrip("/").removesuffix(".pdf")
    # httpx resolves dot segments when it parses, so a literal "/abs/x/../y" never
    # reaches here -- but ".path" percent-decodes, so "%2E%2E" arrives as ".." and is
    # inside the allowlist's character set. Splicing it back in and letting httpx
    # resolve it turns "/abs/x/%2E%2E/%2E%2E/y" into "/y", announced as this paper's
    # abstract page. (The preprint branch is immune: it never decodes.)
    if ".." in identifier or not _ARXIV_ID_RE.fullmatch(identifier):
        return AcademicRoute(url, "none", "")
    abstract = str(
        parsed.copy_with(scheme="https", path=f"/abs/{identifier}", query=None, fragment=None)
    )
    fulltext = str(
        parsed.copy_with(scheme="https", path=f"/html/{identifier}", query=None, fragment=None)
    )
    if section == "html":
        return AcademicRoute(
            fulltext,
            "arxiv",
            "arXiv renders full text as HTML only for submissions from December 2023 onward. If "
            f"this 404s, the paper predates that: the abstract is at {abstract} and the full "
            "text exists only as a PDF.",
        )
    return AcademicRoute(
        abstract,
        "arxiv",
        f"Rewritten to arXiv's abstract page, which is readable text. Full text is at {fulltext} "
        "for submissions from December 2023 onward; older papers have a PDF and nothing else.",
    )


def _paywall(host: str, url: str) -> AcademicRoute:
    doi = _extract_doi(url)
    return AcademicRoute(
        url,
        "paywall",
        f"{host} is subscriber-only: this fetch will most likely return a sign-in or purchase "
        "page rather than the article, and such a page must never be reported as the paper's "
        "content. Look for an open copy instead -- PubMed Central, arXiv or another preprint "
        "server, or the authors' own page."
        + (f" The DOI in this URL is {doi}, which is what to search for." if doi else ""),
    )


def route_academic(url: str) -> AcademicRoute:
    """Which URL to fetch for *url*, and what to tell the model about the swap.

    Unrecognised, malformed, and non-HTTP URLs pass through untouched with
    ``kind == "none"``: rejecting them is the fetcher's trust boundary, not this
    table's job, and a routing layer that raised would turn every odd URL into a
    tool error instead of a fetch attempt.
    """
    try:
        parsed = httpx.URL(url)
        host = parsed.raw_host.decode("ascii").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not host or len(host) > _MAX_HOST_CHARS:
            return AcademicRoute(url, "none", "")
        if _is_domain(host, "pmc.ncbi.nlm.nih.gov") or (
            _is_domain(host, "ncbi.nlm.nih.gov") and parsed.path.startswith("/pmc/")
        ):
            return _pmc(parsed, url)
        if _is_domain(host, "pubmed.ncbi.nlm.nih.gov"):
            return _pubmed(parsed, url)
        if _is_domain(host, "biorxiv.org") or _is_domain(host, "medrxiv.org"):
            return _preprint(parsed, url)
        if _is_domain(host, "arxiv.org"):
            return _arxiv(parsed, url)
        if any(_is_domain(host, domain) for domain in _PAYWALL_DOMAINS):
            return _paywall(host, url)
    except (httpx.InvalidURL, UnicodeDecodeError):
        # The rewrites are inside the guard, not just the parse: "returns a route for
        # any string" is what lets the fetcher route again on a redirect target it did
        # not choose, and one unroutable URL must degrade to "no rewrite", never to a
        # tool error.
        return AcademicRoute(url, "none", "")
    return AcademicRoute(url, "none", "")


__all__ = ["AcademicRoute", "RouteKind", "route_academic"]
