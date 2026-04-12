import importlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_importx_cache: dict = {}


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
