import datetime
import importlib
import json
import logging
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

_importx_cache: dict = {}
_bootstrap_state: dict = {}  # populated by bootstrap(); read by health endpoints


@dataclass
class RepoInfo:
    path: Path
    sha: str
    branch: str
    dirty: bool


def load_repo(path) -> RepoInfo:
    p = Path(path).resolve()
    if p.is_file():
        p = p.parent
    root   = _git(p, ["rev-parse", "--show-toplevel"])
    p      = Path(root) if root else p
    sha    = _git(p, ["rev-parse", "HEAD"])
    branch = _git(p, ["rev-parse", "--abbrev-ref", "HEAD"])
    dirty  = _git(p, ["status", "--porcelain"]) != ""
    return RepoInfo(path=p, sha=sha, branch=branch, dirty=dirty)


def _git(path: Path, args: list) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(path)] + args, text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def importx(dotted: str, *, local: str | None = None):
    """Lazy, cached import for own-repo symbols. Call inside functions, never at module level."""
    if dotted in _importx_cache:
        return _importx_cache[dotted]

    module_path, _, attr = dotted.rpartition(".")
    if not module_path:
        raise ImportError(f"versholn.importx: dotted must include a module and attribute, got: {dotted!r}")

    if local is not None:
        p = Path(local)
        if not p.exists():
            raise ImportError(f"versholn.importx: local path not found: {local}")
        _prepend_path(str(p))
    else:
        top_pkg = module_path.split(".")[0]
        caller_root = _git(Path(__file__).parent, ["rev-parse", "--show-toplevel"])
        if caller_root:
            sibling = Path(caller_root).parent / top_pkg
            if sibling.exists():
                _prepend_path(str(sibling))

    mod = importlib.import_module(module_path)
    symbol = getattr(mod, attr)
    _importx_cache[dotted] = symbol
    return symbol


def _prepend_path(p: str) -> None:
    if p not in sys.path:
        sys.path.insert(0, p)


def _github_head_sha(raw_url: str) -> str:
    """Given a raw.githubusercontent.com URL, return the HEAD commit SHA of that branch.

    E.g. https://raw.githubusercontent.com/owner/repo/main/file.json
    -> GET https://api.github.com/repos/owner/repo/commits/main -> sha

    Returns 'unknown' if the URL is not a GitHub raw URL or the request fails.
    """
    try:
        parts = raw_url.split("/")
        # https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{...}
        idx = parts.index("raw.githubusercontent.com")
        owner, repo, branch = parts[idx + 1], parts[idx + 2], parts[idx + 3]
        api_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{branch}"
        req = urllib.request.Request(api_url, headers={"Accept": "application/vnd.github.sha"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode().strip()
    except Exception:
        return "unknown"


def _pin_raw_url(raw_url: str, sha: str) -> str:
    """Replace the branch/tag segment of a raw.githubusercontent.com URL with a commit SHA.

    https://raw.githubusercontent.com/owner/repo/main/file.json
    -> https://raw.githubusercontent.com/owner/repo/{sha}/file.json
    """
    try:
        parts = raw_url.split("/")
        idx = parts.index("raw.githubusercontent.com")
        # parts[idx+3] is the branch; replace with sha
        parts[idx + 3] = sha
        return "/".join(parts)
    except Exception:
        return raw_url


def version_info() -> dict:
    """Return version metadata for the current runtime environment.

    Production (bootstrap has run): returns mode=production plus compat_url,
    bootstrapped_at, and per-repo SHAs from the compat record.

    Local dev (bootstrap never runs): returns mode=local plus the SHA of
    each sibling repo detected via the sibling-dirs convention.
    """
    if _bootstrap_state:
        return {"mode": "production", **_bootstrap_state}

    my_root = _git(Path(__file__).parent, ["rev-parse", "--show-toplevel"])
    if not my_root:
        return {"mode": "local", "repos": {}}

    siblings_root = Path(my_root).parent
    repos = {}
    for sibling in sorted(siblings_root.iterdir()):
        if not sibling.is_dir():
            continue
        sha = _git(sibling, ["rev-parse", "HEAD"])
        if sha:
            url = _git(sibling, ["remote", "get-url", "origin"]) or sibling.name
            repos[url] = sha

    return {
        "mode": "local",
        "repos": repos,
    }


def bootstrap(
    compat_url: str,
    clone_root: str = "/app/deps",
    pat: str | None = None,
):
    """Fetch compat.json, clone all pinned deps, prepend to sys.path.

    Call from entrypoint.py at container startup only — never in local dev.
    Returns this module, possibly reloaded if versholn self-updates.
    """
    _log.info("versholn.bootstrap: fetching compat record from %s", compat_url)

    compat_sha = _github_head_sha(compat_url)
    pinned_url = _pin_raw_url(compat_url, compat_sha) if compat_sha != "unknown" else compat_url
    _log.info("versholn.bootstrap: compat SHA %s, fetching from %s", compat_sha, pinned_url)

    with urllib.request.urlopen(pinned_url, timeout=30) as resp:
        compat = json.loads(resp.read().decode())

    clone_root_path = Path(clone_root)
    clone_root_path.mkdir(parents=True, exist_ok=True)

    my_sha = _git(Path(__file__).parent, ["rev-parse", "HEAD"])
    repos = compat.get("repos", {})

    versholn_entry = repos.get("versholn")
    versholn_needs_update = (
        versholn_entry is not None
        and my_sha
        and not versholn_entry["sha"].startswith(my_sha)
        and not my_sha.startswith(versholn_entry["sha"])
    )

    cloned: dict = {}

    for repo_name, repo_info in repos.items():
        if repo_name == "versholn":
            continue
        sha = repo_info["sha"]
        url = repo_info["url"]
        is_private = repo_info.get("private", False)
        dest = clone_root_path / repo_name
        _log.info("versholn.bootstrap: cloning %s @ %s", repo_name, sha)
        _clone_at_sha(url, sha, dest, pat=pat if is_private else None)
        _prepend_path(str(dest))
        cloned[url] = sha

    _bootstrap_state.update({
        "compat_url": compat_url,
        "compat_sha": compat_sha,
        "clone_root": clone_root,
        "bootstrapped_at": datetime.datetime.utcnow().isoformat() + "Z",
        "repos": cloned,
    })

    if versholn_needs_update:
        new_sha = versholn_entry["sha"]
        dest = clone_root_path / "versholn"
        _log.info("versholn.bootstrap: self-update %s -> %s", my_sha, new_sha)
        _clone_at_sha(versholn_entry["url"], new_sha, dest, pat=None)
        _prepend_path(str(dest))
        import versholn as _new
        importlib.reload(_new)
        _new._bootstrap_state.update(_bootstrap_state)
        _new._bootstrap_state["repos"][versholn_entry["url"]] = new_sha
        return _new

    if versholn_entry:
        _bootstrap_state["repos"][versholn_entry["url"]] = versholn_entry["sha"]
    return sys.modules[__name__]


def _clone_at_sha(url: str, sha: str, dest: Path, *, pat: str | None = None) -> None:
    """Shallow-clone a repo at a specific SHA into dest."""
    clone_url = _inject_pat(url, pat) if pat else url

    if dest.exists():
        actual = _git(dest, ["rev-parse", "HEAD"])
        if actual and (actual == sha or actual.startswith(sha) or sha.startswith(actual)):
            _log.debug("versholn._clone_at_sha: %s already at %s, skipping", dest.name, sha)
            return
        shutil.rmtree(dest)

    dest.mkdir(parents=True, exist_ok=True)
    _run(["git", "-C", str(dest), "init"])
    _run(["git", "-C", str(dest), "remote", "add", "origin", clone_url])
    _run(["git", "-C", str(dest), "fetch", "--depth", "1", "origin", sha])
    _run(["git", "-C", str(dest), "checkout", "FETCH_HEAD"])

    actual = _git(dest, ["rev-parse", "HEAD"])
    if not actual:
        raise RuntimeError(f"_clone_at_sha: could not verify SHA after clone of {url}")
    if not (actual == sha or actual.startswith(sha) or sha.startswith(actual)):
        raise RuntimeError(f"_clone_at_sha: SHA mismatch — expected {sha!r}, got {actual!r}")


def _inject_pat(url: str, pat: str) -> str:
    """Insert a PAT into a GitHub HTTPS URL: https://github.com/... -> https://<pat>@github.com/..."""
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    return f"{scheme}://{pat}@{rest}"


def _run(cmd: list) -> None:
    """Run a subprocess, raising CalledProcessError on failure. Stdout suppressed; stderr passes through."""
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL)
