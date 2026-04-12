import subprocess
from dataclasses import dataclass
from pathlib import Path


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
