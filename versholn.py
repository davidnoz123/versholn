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
    """Lazy, cached import for own-repo symbols. Call inside functions, never at module level.

    Resolves dotted paths left-to-right: tries each split point from longest module
    path to shortest, chaining getattr for any remaining components. This handles
    module.submodule.func as well as module.Class.method.
    """
    if dotted in _importx_cache:
        return _importx_cache[dotted]

    parts = dotted.split(".")
    if len(parts) < 2:
        raise ImportError(f"versholn.importx: dotted must include a module and attribute, got: {dotted!r}")

    if local is not None:
        p = Path(local)
        if not p.exists():
            raise ImportError(f"versholn.importx: local path not found: {local}")
        _prepend_path(str(p))
    else:
        top_pkg = parts[0]
        caller_root = _git(Path(__file__).parent, ["rev-parse", "--show-toplevel"])
        if caller_root:
            sibling = Path(caller_root).parent / top_pkg
            if sibling.exists():
                _prepend_path(str(sibling))

    # Try splits right-to-left: longest module path first, fall back on ImportError.
    for split in range(len(parts) - 1, 0, -1):
        try:
            mod = importlib.import_module(".".join(parts[:split]))
            obj = mod
            for attr in parts[split:]:
                obj = getattr(obj, attr)
            _importx_cache[dotted] = obj
            return obj
        except (ImportError, AttributeError):
            continue

    raise ImportError(f"versholn.importx: cannot resolve {dotted!r}")


def install_and_import(package_spec: str, *, import_as: str | None = None):
    """Ensure a PyPI package is installed then import and return the module.

    Call inside functions, never at module level.

    In production, versholn.bootstrap() should have already installed the package
    via the compat.json pip section. This function is the graceful fallback for
    local dev and for the first call before bootstrap runs.

    Args:
        package_spec: pip install spec, e.g. "httpx" or "httpx>=0.25".
        import_as: module name to import if different from the package name,
                   e.g. install_and_import("Pillow", import_as="PIL").
    """
    import_name = import_as or package_spec.split("[")[0].split(">")[0].split("<")[0].split("=")[0].split("!")[0].strip()
    cache_key = f"__pip__{import_name}"
    if cache_key in _importx_cache:
        return _importx_cache[cache_key]

    try:
        mod = importlib.import_module(import_name)
    except ImportError:
        _log.info("versholn.install_and_import: installing %s", package_spec)
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", package_spec],
            stdout=subprocess.DEVNULL,
        )
        mod = importlib.import_module(import_name)

    _importx_cache[cache_key] = mod
    return mod


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


def _repo_name_from_url(url: str) -> str:
    """Derive a short directory name from a repo URL.
    e.g. https://github.com/user/geo_tools.git -> geo_tools
    """
    name = url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name or url


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
    my_url = _git(Path(__file__).parent, ["remote", "get-url", "origin"])
    repos = compat.get("repos", {})

    versholn_entry = repos.get(my_url) if my_url else None
    versholn_needs_update = (
        versholn_entry is not None
        and my_sha
        and not versholn_entry["sha"].startswith(my_sha)
        and not my_sha.startswith(versholn_entry["sha"])
    )

    # Install PyPI packages declared in the compat pip section (schema 2+).
    pip_specs = compat.get("pip", {})
    if pip_specs:
        _pip_install_from_compat(pip_specs)

    cloned: dict = {}

    for url, repo_info in repos.items():
        if url == my_url:
            continue
        sha = repo_info["sha"]
        is_private = repo_info.get("private", False)
        dest = clone_root_path / _repo_name_from_url(url)
        _log.info("versholn.bootstrap: cloning %s @ %s", _repo_name_from_url(url), sha)
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
        dest = clone_root_path / _repo_name_from_url(my_url)
        _log.info("versholn.bootstrap: self-update %s -> %s", my_sha, new_sha)
        _clone_at_sha(my_url, new_sha, dest, pat=None)
        _prepend_path(str(dest))
        import versholn as _new
        importlib.reload(_new)
        _new._bootstrap_state.update(_bootstrap_state)
        _new._bootstrap_state["repos"][my_url] = new_sha
        return _new

    if versholn_entry:
        _bootstrap_state["repos"][my_url] = versholn_entry["sha"]
    return sys.modules[__name__]


def _pip_install_from_compat(pip_specs: dict) -> None:
    """Install PyPI packages from the compat.json pip section.

    pip_specs maps pip package name -> version spec string, e.g.
        {"httpx": ">=0.25", "some-pkg": "1.2.3"}

    Skips packages already importable at the right version to avoid redundant
    installs on warm restarts.
    """
    import importlib.util
    to_install = []
    for pkg, spec in pip_specs.items():
        import_name = pkg.replace("-", "_")
        if importlib.util.find_spec(import_name) is None:
            to_install.append(f"{pkg}{spec}" if spec else pkg)
        else:
            _log.debug("versholn._pip_install_from_compat: %s already importable, skipping", pkg)
    if to_install:
        _log.info("versholn._pip_install_from_compat: installing %s", to_install)
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet"] + to_install,
            stdout=subprocess.DEVNULL,
        )


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
