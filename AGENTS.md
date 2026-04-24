# AGENTS.md

## New Machine Setup

`versholn` has no external repo deps — it is the bootstrap library itself. No sibling repos need to be cloned to use versholn.

To verify the sibling repos for any *calling* repo:
```powershell
C:\analytics\projects\git\lexi\demos\venv\Scripts\python.exe -c "import sys; sys.path.insert(0, r'C:\analytics\projects\git\lunk\versholn'); import versholn; versholn.doctor(r'<path-to-calling-repo>')"
```

---

## Deferred Imports — Module-Level Rule

**Rule: stdlib only at module level.** No exceptions — not versholn, not requirements.txt packages, not cross-repo deps.

### `get_versholn` bootstrap

Every module that needs versholn imports it via `get_versholn` (defined once in `utils.py`):

```python
from utils import get_versholn  # relative — stdlib-safe

def my_function():
    get_versholn(globals())       # injects versholn into this module's globals
    CDPClient = versholn.importx("chrome_tools.CDPClient")
    ...
```

`get_versholn(globals())` is idempotent — safe to call at the top of every function. After the first call, `versholn` is in the module globals and subsequent calls are instant.

### Patterns in use

| Pattern | When to use |
|---------|-------------|
| `get_versholn(globals())` + `versholn.importx(...)` inside function | Standard: loading any cross-repo symbol in a function |
| `_ensure_X()` bool-flag helper | When 5+ functions in a module all need the same set of symbols |
| `__getattr__` factory | When external code does `from this_module import SomeClass` and `SomeClass` inherits from a cross-repo base |
| `from __future__ import annotations` + `TYPE_CHECKING` block | When type annotations reference cross-repo types |

### Checking compliance

Run from any Python env that has the versholn sibling directory on sys.path:

```python
import versholn
versholn.check_imports("<path-to-repo-root>")   # prints WARNINGs for violations
versholn.check_ide_paths("<path-to-repo-root>") # warns if .vscode extraPaths is missing entries
```

### Pre-edit checklist

Before editing any `.py` file, run `versholn.check_imports()` to see current violations.
Fix violations in the file being edited first, then let ripple-out guide remaining work.

---

## Code Editing Policy

### Syntax-check after every Python edit

After editing any `.py` file, immediately verify it parses cleanly before committing:

```powershell
& "C:\analytics\projects\git\lexi\demos\venv\Scripts\python.exe" -m py_compile path\to\edited_file.py
```

A `SyntaxError` or `IndentationError` at module level causes a completely silent crash when the process runs in a background window (VBA-spawned or `subprocess.Popen`) — stderr is invisible, the process dies before any log call, and the only symptom the user sees is a timeout.

### `safe_local_imports` — blast-radius limitation

Any Python file with an `if __name__ == "__main__":` block that imports non-stdlib modules must centralise those imports in a single function named `safe_local_imports`:

```python
def safe_local_imports(g: dict) -> None:
    """Load all non-stdlib local-module imports into *g* (pass globals()).

    Centralising imports here limits blast radius: if any import raises
    (e.g. a SyntaxError or ImportError buried in an imported module), the
    exception is caught, logged with a full traceback, then re-raised —
    so the log always contains a FATAL line before the process dies.

    Call once near the top of main():
        safe_local_imports(globals())
    """
    try:
        from mymodule import MyClass         # <- replace with this file's actual imports
        g["MyClass"] = MyClass
        # ... all other non-stdlib imports ...
    except Exception:
        import traceback as _tb
        _log(f"FATAL: safe_local_imports failed:\n{_tb.format_exc()}")
        raise


def main() -> int:
    safe_local_imports(globals())
    # MyClass etc. are now available as module globals
    ...
```

**Scope:**
- Same-repo local imports only.
- Cross-repo symbols loaded via `versholn.importx()` are already wrapped separately — do not duplicate them here.
- stdlib imports do not belong here.

**Why "blast radius":** A `SyntaxError` or `ImportError` buried inside an imported module kills the process before `_log` is even defined. Without `safe_local_imports`, a hidden-window process exits with no log entry. With it, there is always at least one `FATAL` line in the log.
