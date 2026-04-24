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
