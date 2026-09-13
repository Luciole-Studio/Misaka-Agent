# Hermes f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140 / hermes_cli/skills_hub.py; see PROVENANCE.json and LICENSE.
from pathlib import Path

_BROWSE_LIMITS = {
    "hermes-index": 1000000, "official": 200, "skills-sh": 200, "well-known": 50,
    "github": 200, "clawhub": 500, "lobehub": 500, "browse-sh": 500}


_BROWSE_API_LIMITS = {
    "hermes-index": 5000, "official": 100, "skills-sh": 100, "well-known": 25,
    "github": 100, "clawhub": 50, "lobehub": 50, "browse-sh": 500}


def _try(fn, *args):
    """`fn(*args)`, or None when it raises (adapter probes are best-effort)."""
    try:
        return fn(*args)
    except Exception:
        return None


def _resolve_source_meta_and_bundle(identifier: str, sources):
    """(meta, bundle, source) from ONE adapter — mixing skills.sh metadata with a ClawHub zip of
    a same-named skill once showed the wrong SKILL.md. Falls back to the first meta-only hit.
    """
    first_meta = None
    first_meta_source = None
    for src in sources:
        meta = _try(src.inspect, identifier)
        bundle = _try(src.fetch, identifier)
        if bundle:
            if meta is None:
                meta = _try(src.inspect, identifier)
            return meta, bundle, src
        if first_meta is None and meta:
            first_meta, first_meta_source = meta, src
    return first_meta, None, first_meta_source


def _github_publish(skill_path: Path, skill_name: str, target_repo: str, auth) -> tuple:
    """Fork, branch, upload, and open a PR with the skill. Returns (success, message)."""
    import base64
    import httpx
    headers = auth.get_headers()
    api = "https://api.github.com/repos"

    def call(method: str, path: str, timeout: int = 15, **kw):
        response = getattr(httpx, method)(f"{api}/{path}", headers=headers, timeout=timeout, **kw)
        if method == "put" or (method == "post" and path.endswith("/git/refs")):
            response.raise_for_status()
        return response

    try:
        resp = call("post", f"{target_repo}/forks", timeout=30)
        if resp.status_code in {200, 202}:
            fork_repo = resp.json()["full_name"]
        elif resp.status_code == 403:
            return False, "GitHub token lacks permission to fork repos"
        else:
            return False, f"Failed to fork {target_repo}: {resp.status_code}"
    except httpx.HTTPError as e:
        return False, f"Network error forking repo: {e}"

    try:
        default_branch = call("get", target_repo).json().get("default_branch", "main")
    except Exception:
        default_branch = "main"
    try:
        ref = call("get", f"{fork_repo}/git/refs/heads/{default_branch}").json()
        base_sha = ref["object"]["sha"]
    except Exception as e:
        return False, f"Failed to get base branch: {e}"

    branch_name = f"add-skill-{skill_name}"
    try:
        call("post", f"{fork_repo}/git/refs",
             json={"ref": f"refs/heads/{branch_name}", "sha": base_sha})
    except Exception as e:
        return False, f"Failed to create branch: {e}"

    for f in skill_path.rglob("*"):
        if f.is_symlink() or getattr(f, "is_junction", lambda: False)():
            return False, "Publishing a linked file is not supported."
        if not f.is_file():
            continue
        rel = f.relative_to(skill_path).as_posix()
        try:
            call("put", f"{fork_repo}/contents/skills/{skill_name}/{rel}",
                 json={"message": f"Add {skill_name} skill: {rel}",
                       "content": base64.b64encode(f.read_bytes()).decode(), "branch": branch_name})
        except Exception as e:
            return False, f"Failed to upload {rel}: {e}"

    try:
        resp = call("post", f"{target_repo}/pulls", json={
            "title": f"Add skill: {skill_name}",
            "body": f"Submitting the `{skill_name}` skill via Hermes Skills Hub.\n\n"
                    f"This skill was scanned by the Hermes Skills Guard before submission.",
            "head": f"{fork_repo.split('/')[0]}:{branch_name}", "base": default_branch})
        if resp.status_code == 201:
            return True, f"PR created: {resp.json().get('html_url', '')}"
        return False, f"Failed to create PR: {resp.status_code} {resp.text[:200]}"
    except httpx.HTTPError as e:
        return False, f"Network error creating PR: {e}"

