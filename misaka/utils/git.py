"""Git source parsing helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse


@dataclass(slots=True)
class GitSource:
    type: str
    repo: str
    host: str
    path: str
    ref: str | None = None
    pinned: bool = False


_SCP_LIKE = re.compile(r"^git@([^:]+):(.+)$")
_HOSTED_PATTERNS: dict[str, re.Pattern[str]] = {
    "github.com": re.compile(r"^(?:github:|(?:https?://)?github\.com/)([^/]+)/([^/@#]+?)(?:\.git)?(?:[#@](.+))?$"),
    "gitlab.com": re.compile(r"^(?:gitlab:|(?:https?://)?gitlab\.com/)([^/]+)/([^/@#]+?)(?:\.git)?(?:[#@](.+))?$"),
    "bitbucket.org": re.compile(
        r"^(?:bitbucket:|(?:https?://)?bitbucket\.org/)([^/]+)/([^/@#]+?)(?:\.git)?(?:[#@](.+))?$"
    ),
}


def parse_git_url(source: str) -> GitSource | None:
    trimmed = source.strip()
    has_git_prefix = trimmed.startswith("git:")
    url = trimmed[4:].strip() if has_git_prefix else trimmed

    if not has_git_prefix and not re.match(r"^(https?|ssh|git)://", url, re.IGNORECASE):
        return None

    repo_without_ref, ref = _split_ref(url)
    hosted = _parse_hosted_git_url(url, repo_without_ref, ref)
    if hosted is not None:
        return hosted

    if not url.startswith(("https://", "http://", "ssh://", "git://", "git@")):
        hosted = _parse_hosted_git_url(f"https://{url}", repo_without_ref, ref)
        if hosted is not None:
            return hosted

    return _parse_generic_git_url(repo_without_ref, ref)


def _split_ref(url: str) -> tuple[str, str | None]:
    scp_like_match = _SCP_LIKE.match(url)
    if scp_like_match is not None:
        path_with_maybe_ref = scp_like_match.group(2) or ""
        ref_separator = path_with_maybe_ref.find("@")
        if ref_separator < 0:
            return url, None
        repo_path = path_with_maybe_ref[:ref_separator]
        ref = path_with_maybe_ref[ref_separator + 1 :]
        if not repo_path or not ref:
            return url, None
        return f"git@{scp_like_match.group(1)}:{repo_path}", ref

    if "://" in url:
        try:
            parsed = urlparse(url)
        except ValueError:
            return url, None
        path_with_maybe_ref = parsed.path.lstrip("/")
        ref_separator = path_with_maybe_ref.find("@")
        if ref_separator < 0:
            return url, None
        repo_path = path_with_maybe_ref[:ref_separator]
        ref = path_with_maybe_ref[ref_separator + 1 :]
        if not repo_path or not ref:
            return url, None
        rebuilt = parsed._replace(path=f"/{repo_path}")
        return urlunparse(rebuilt).rstrip("/"), ref

    slash_index = url.find("/")
    if slash_index < 0:
        return url, None
    host = url[:slash_index]
    path_with_maybe_ref = url[slash_index + 1 :]
    ref_separator = path_with_maybe_ref.find("@")
    if ref_separator < 0:
        return url, None
    repo_path = path_with_maybe_ref[:ref_separator]
    ref = path_with_maybe_ref[ref_separator + 1 :]
    if not repo_path or not ref:
        return url, None
    return f"{host}/{repo_path}", ref


def _parse_generic_git_url(repo_without_ref: str, ref: str | None) -> GitSource | None:
    repo = repo_without_ref.rstrip("/")
    host = ""
    path = ""

    scp_like_match = _SCP_LIKE.match(repo_without_ref)
    if scp_like_match is not None:
        host = scp_like_match.group(1) or ""
        path = scp_like_match.group(2) or ""
    elif repo_without_ref.startswith(("https://", "http://", "ssh://", "git://")):
        try:
            parsed = urlparse(repo_without_ref)
        except ValueError:
            return None
        host = parsed.hostname or ("local" if parsed.scheme == "file" else "")
        path = parsed.path.lstrip("/")
        repo = repo_without_ref.rstrip("/")
    else:
        slash_index = repo_without_ref.find("/")
        if slash_index < 0:
            return None
        host = repo_without_ref[:slash_index]
        path = repo_without_ref[slash_index + 1 :]
        if "." not in host and host != "localhost":
            return None
        repo = f"https://{repo_without_ref}"

    normalized_path = path.lstrip("/").removesuffix(".git")
    if not host or not normalized_path or len(normalized_path.split("/")) < 2:
        return None

    return GitSource(
        type="git",
        repo=repo,
        host=host,
        path=normalized_path,
        ref=ref,
        pinned=bool(ref),
    )


def _parse_hosted_git_url(url: str, repo_without_ref: str, ref: str | None) -> GitSource | None:
    for domain, pattern in _HOSTED_PATTERNS.items():
        match = pattern.match(url)
        if match is None:
            continue
        user = match.group(1)
        project = match.group(2).removesuffix(".git")
        committish = match.group(3) or ref
        repo = _normalize_hosted_repo(repo_without_ref, domain, user, project)
        return GitSource(
            type="git",
            repo=repo,
            host=domain,
            path=f"{user}/{project}",
            ref=committish or None,
            pinned=bool(committish),
        )
    return None


def _normalize_hosted_repo(repo_without_ref: str, domain: str, user: str, project: str) -> str:
    if repo_without_ref.startswith("git@"):
        return repo_without_ref
    if repo_without_ref.startswith(("http://", "https://", "ssh://", "git://")):
        try:
            parsed = urlparse(repo_without_ref)
        except ValueError:
            return repo_without_ref
        normalized = parsed._replace(path=f"/{user}/{project}", params="", query="", fragment="")
        return urlunparse(normalized).rstrip("/")
    return f"https://{domain}/{user}/{project}"


parseGitUrl = parse_git_url

__all__ = [
    "GitSource",
    "parseGitUrl",
]
