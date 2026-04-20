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


def _repo_name_from_url(url: str) -> str:
    """Extract the repo directory name from a git clone URL or https URL.

    Examples:
        https://github.com/org/chrome_tools.git -> chrome_tools
        https://github.com/org/chrome_tools     -> chrome_tools
        git@github.com:org/chrome_tools.git     -> chrome_tools
    """
    name = url.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name


# ---------------------------------------------------------------------------
# Development tools — check_imports, verify, check_ide_paths
# ---------------------------------------------------------------------------

_STDLIB_MODULES: set | None = None


def _stdlib_modules() -> set:
    """Return the set of top-level stdlib module names (lazy, cached)."""
    global _STDLIB_MODULES
    if _STDLIB_MODULES is None:
        import sys
        # Always-known extras (covers modules added across 3.x versions)
        _EXTRA = {
            "abc", "ast", "asyncio", "base64", "binascii", "builtins",
            "calendar", "cgi", "cgitb", "chunk", "cmath", "cmd", "code",
            "codecs", "codeop", "colorsys", "compileall", "concurrent",
            "configparser", "contextlib", "contextvars", "copy", "copyreg",
            "csv", "ctypes", "curses", "dataclasses", "datetime", "dbm",
            "decimal", "difflib", "dis", "doctest", "email", "encodings",
            "enum", "errno", "faulthandler", "fcntl", "filecmp", "fileinput",
            "fnmatch", "fractions", "ftplib", "functools", "gc", "getopt",
            "getpass", "gettext", "glob", "grp", "gzip", "hashlib", "heapq",
            "hmac", "html", "http", "idlelib", "imaplib", "importlib",
            "inspect", "io", "ipaddress", "itertools", "json", "keyword",
            "lib2to3", "linecache", "locale", "logging", "lzma", "mailbox",
            "math", "mimetypes", "mmap", "modulefinder", "multiprocessing",
            "netrc", "nis", "nntplib", "numbers", "opcode", "operator",
            "optparse", "os", "pathlib", "pdb", "pickle", "pickletools",
            "pipes", "pkgutil", "platform", "plistlib", "poplib", "posix",
            "posixpath", "pprint", "profile", "pstats", "pty", "pwd", "py_compile",
            "pyclbr", "pydoc", "queue", "quopri", "random", "re", "readline",
            "reprlib", "resource", "rlcompleter", "runpy", "sched", "secrets",
            "select", "selectors", "shelve", "shlex", "shutil", "signal",
            "site", "smtpd", "smtplib", "sndhdr", "socket", "socketserver",
            "spwd", "sqlite3", "sre_compile", "sre_constants", "sre_parse",
            "ssl", "stat", "statistics", "string", "stringprep", "struct",
            "subprocess", "sunau", "symtable", "sys", "sysconfig", "syslog",
            "tabnanny", "tarfile", "telnetlib", "tempfile", "termios", "test",
            "textwrap", "threading", "time", "timeit", "tkinter", "token",
            "tokenize", "tomllib", "trace", "traceback", "tracemalloc",
            "tty", "turtle", "turtledemo", "types", "typing", "unicodedata",
            "unittest", "urllib", "uu", "uuid", "venv", "warnings", "wave",
            "weakref", "webbrowser", "winreg", "winsound", "wsgiref",
            "xdrlib", "xml", "xmlrpc", "zipapp", "zipfile", "zipimport",
            "zlib", "zoneinfo", "_thread", "__future__",
        }
        base = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else set()
        _STDLIB_MODULES = base | set(sys.builtin_module_names) | _EXTRA
    return _STDLIB_MODULES


def check_imports(repo_root: str | None = None) -> list:
    """Scan a project repo for module-level non-stdlib imports and report violations.

    Returns a list of violation dicts. Also prints each violation as a WARNING to stdout.

    Violations detected:
    - module-level ``import X`` or ``from X import Y`` where X is not stdlib
    - ``versholn.importx(...)`` call at module level
    - ``import versholn`` at module level

    Permitted at module level (never flagged):
    - stdlib imports
    - ``from __future__ import annotations``
    - ``from typing import TYPE_CHECKING`` and the ``if TYPE_CHECKING:`` block
    - intra-repo relative imports (``from utils import ...``, ``from .foo import ...``)
    - local filename imports (e.g. importing a sibling .py by its bare name is treated as local)

    Args:
        repo_root: path to the repo root to scan. Defaults to the caller's repo root
                   (detected via git rev-parse from __file__).

    Returns:
        List of dicts with keys: ``file``, ``line``, ``code``, ``message``.
    """
    import ast as _ast
    import os as _os

    if repo_root is None:
        caller_root = _git(Path(__file__).parent, ["rev-parse", "--show-toplevel"])
        if not caller_root:
            _log.warning("versholn.check_imports: cannot determine repo root (not in a git repo)")
            return []
        repo_root = caller_root

    stdlib = _stdlib_modules()
    violations: list = []

    # Collect all .py files under repo_root, skipping common non-source dirs
    _SKIP_DIRS = {"__pycache__", ".git", "venv", ".venv", "node_modules", ".pytest_cache"}

    def _scan_file(path: str) -> list:
        try:
            src = open(path, encoding="utf-8", errors="replace").read()
            tree = _ast.parse(src, filename=path)
        except SyntaxError:
            return []

        file_violations = []
        rel = _os.path.relpath(path, repo_root)
        # Build set of local module names (sibling .py files and dirs)
        local_dir = _os.path.dirname(path)
        local_names: set = set()
        try:
            for entry in _os.listdir(local_dir):
                if entry.endswith(".py"):
                    local_names.add(entry[:-3])
                elif _os.path.isdir(_os.path.join(local_dir, entry)) and not entry.startswith("."):
                    local_names.add(entry)
        except OSError:
            pass

        for node in tree.body:
            # --- module-level import statements ---
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in stdlib or top in local_names:
                        continue
                    file_violations.append({
                        "file": rel, "line": node.lineno,
                        "code": "module-level-import",
                        "message": f"module-level non-stdlib import: import {alias.name}",
                    })

            elif isinstance(node, _ast.ImportFrom):
                if node.level and node.level > 0:
                    continue  # relative import — always local
                module = node.module or ""
                top = module.split(".")[0] if module else ""
                if top in ("__future__", "typing") or top in stdlib or top in local_names:
                    continue
                # Allow bare names that match local sibling files
                if top in local_names:
                    continue
                # Allow 'from utils import ...' (common intra-repo pattern — non-relative but local)
                if top == "utils" or top == "project":
                    continue
                file_violations.append({
                    "file": rel, "line": node.lineno,
                    "code": "module-level-import",
                    "message": f"module-level non-stdlib import: from {module} import ...",
                })

            # --- module-level versholn.importx() calls (assignment or bare expr) ---
            elif isinstance(node, (_ast.Assign, _ast.AnnAssign, _ast.Expr)):
                value = getattr(node, "value", None)
                if value is None and isinstance(node, _ast.AnnAssign):
                    continue
                targets = getattr(node, "targets", [])

                def _is_versholn_importx(val) -> bool:
                    return (
                        isinstance(val, _ast.Call)
                        and isinstance(val.func, _ast.Attribute)
                        and val.func.attr == "importx"
                        and isinstance(val.func.value, _ast.Name)
                        and val.func.value.id == "versholn"
                    )

                if value is not None and _is_versholn_importx(value):
                    target_str = ""
                    if targets:
                        try:
                            target_str = _ast.unparse(targets[0]) + " = "
                        except Exception:
                            pass
                    file_violations.append({
                        "file": rel, "line": node.lineno,
                        "code": "module-level-importx",
                        "message": f"module-level versholn.importx call: {target_str}versholn.importx(...)",
                    })

        return file_violations

    for dirpath, dirs, files in _os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files:
            if fname.endswith(".py"):
                vv = _scan_file(_os.path.join(dirpath, fname))
                violations.extend(vv)

    # Report
    by_file: dict = {}
    for v in violations:
        by_file.setdefault(v["file"], []).append(v)

    for fpath, fvv in sorted(by_file.items()):
        for v in fvv:
            print(f"WARNING versholn.check_imports: {v['code']}")
            print(f"  {v['file']}:{v['line']}  {v['message']}")

    return violations


def verify(repo_root: str | None = None) -> None:
    """Eagerly probe all compat.json repos; raise on first missing or broken one.

    Call in project.py under a dev/local guard so misconfigured paths surface
    at startup in dev without affecting production cold-start time or stability.

    Example::

        if not IN_PRODUCTION:
            versholn.verify()

    Raises:
        ImportError: if any repo listed in compat.json cannot be found or imported.
    """
    import os as _os

    if repo_root is None:
        caller_root = _git(Path(__file__).parent, ["rev-parse", "--show-toplevel"])
        repo_root = caller_root or ""

    # Find compat.json — check environment variable first, then versholn_db sibling
    compat_url = _os.environ.get("VERSHOLN_COMPAT_URL", "")
    compat_data: dict | None = None

    if not compat_url and repo_root:
        # Try local sibling versholn_db/compat.json
        candidate = Path(repo_root).parent / "versholn_db" / "compat.json"
        if candidate.exists():
            try:
                compat_data = json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                pass

    if compat_data is None and compat_url:
        try:
            with urllib.request.urlopen(compat_url, timeout=5) as resp:
                compat_data = json.loads(resp.read())
        except Exception as exc:
            _log.warning("versholn.verify: cannot fetch compat.json from %s: %s", compat_url, exc)
            return

    if compat_data is None:
        _log.debug("versholn.verify: no compat.json found — skipping probe")
        return

    repos = compat_data.get("repos", {})
    my_root = Path(repo_root) if repo_root else None
    base = my_root.parent if my_root else Path.cwd().parent

    for url, entry in repos.items():
        repo_name = _repo_name_from_url(url)
        candidate = base / repo_name
        if not candidate.exists():
            raise ImportError(
                f"versholn.verify: repo '{repo_name}' not found at {candidate}\n"
                f"  Expected sibling of {base}\n"
                f"  URL: {url}"
            )
        # Try importing the top-level package to catch obvious breakage
        sys.path.insert(0, str(candidate))
        try:
            importlib.import_module(repo_name)
        except ImportError as exc:
            raise ImportError(
                f"versholn.verify: repo '{repo_name}' found at {candidate} but cannot be imported: {exc}"
            ) from exc
        finally:
            try:
                sys.path.remove(str(candidate))
            except ValueError:
                pass

    _log.debug("versholn.verify: all %d repos OK", len(repos))


def check_ide_paths(repo_root: str | None = None) -> list:
    """Check that .vscode/settings.json extraPaths covers all sibling repos in compat.json.

    Reads the repo paths from compat.json, then checks ``.vscode/settings.json``
    ``python.analysis.extraPaths`` against the expected sibling dirs. Prints a WARNING
    for any missing entry. Silent if correctly configured. No-ops if no ``.vscode/``
    directory is present (i.e. in production).

    Returns:
        List of missing path strings. Empty list means fully configured.
    """
    import os as _os

    if repo_root is None:
        caller_root = _git(Path(__file__).parent, ["rev-parse", "--show-toplevel"])
        repo_root = caller_root or ""

    vscode_dir = Path(repo_root) / ".vscode"
    if not vscode_dir.exists():
        return []  # production / no VS Code workspace — no-op

    settings_path = vscode_dir / "settings.json"
    if not settings_path.exists():
        _log.debug("versholn.check_ide_paths: no .vscode/settings.json found")
        return []

    # Parse settings.json (allow comments via simple strip — not full JSONC parser)
    try:
        raw = settings_path.read_text(encoding="utf-8")
        # Strip single-line // comments (simple heuristic)
        import re as _re
        raw_stripped = _re.sub(r"//[^\n]*", "", raw)
        settings = json.loads(raw_stripped)
    except Exception as exc:
        _log.warning("versholn.check_ide_paths: cannot parse .vscode/settings.json: %s", exc)
        return []

    extra_paths = settings.get("python.analysis.extraPaths", [])
    # Normalise to absolute paths
    base = Path(repo_root)
    normalised = set()
    for p in extra_paths:
        pp = Path(p)
        if not pp.is_absolute():
            pp = (base / pp).resolve()
        normalised.add(str(pp))

    # Determine expected sibling dirs from compat.json
    compat_data: dict | None = None
    candidate = Path(repo_root).parent / "versholn_db" / "compat.json"
    if candidate.exists():
        try:
            compat_data = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            pass

    if compat_data is None:
        compat_url = _os.environ.get("VERSHOLN_COMPAT_URL", "")
        if compat_url:
            try:
                with urllib.request.urlopen(compat_url, timeout=5) as resp:
                    compat_data = json.loads(resp.read())
            except Exception:
                pass

    if compat_data is None:
        _log.debug("versholn.check_ide_paths: no compat.json found — skipping")
        return []

    parent = Path(repo_root).parent
    repos = compat_data.get("repos", {})
    missing = []
    for url in repos:
        repo_name = _repo_name_from_url(url)
        expected = str((parent / repo_name).resolve())
        if expected not in normalised:
            missing.append(expected)
            print(
                f"WARNING versholn.check_ide_paths: extraPaths missing '{repo_name}'\n"
                f"  Add to .vscode/settings.json python.analysis.extraPaths:\n"
                f"  {expected}"
            )

    if not missing:
        _log.debug("versholn.check_ide_paths: extraPaths OK (%d repos)", len(repos))

    return missing

