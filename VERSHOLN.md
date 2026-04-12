# Versholn � Design Document


---

## What Is Versholn?

Versholn (pronounced like "version") is a small polyrepo coordination tool. It is **not** a shared code library � it is a **compatibility registry**. Its job is to:

1. Load a set of repos and pin their identity (SHA-based, not manual tags)
2. Record what ran and whether it passed
3. Promote known-good SHA combinations to a compat record
4. Let future runs check against those compat records

The name follows the same `-oln` construction as `nielsoln` � a family marker for this ecosystem of tools.

---

## Core Concepts

### Repo Identity
- A repo is identified by its **SHA** � not its name or path
- Canonical absolute paths are resolved at load time
- Repos are registered in an in-memory dict keyed by SHA for the duration of a session
- Duplicate loads from different paths for the same SHA are rejected

### Run Recording
- A **run** is a start + finish pair, written to `runs.jsonl`
- Finish is always written, even on exception (try/finally)
- Every run has a unique `run_id`
- Three recording levels:
  - **Minimal** (default): fast, always-on, compact exception summaries only
  - **Standard**: includes repo refresh + upstream checks
  - **Full**: includes traceback + environment detail

### Receipts
- Append-only log files � the audit trail of what ran
- Separate from run records (richer detail per run)

### Compat Promotion
- Only promoted from completed run records
- Dirty repos excluded
- Missing data rejected
- Only pass/fail runs eligible

### Virtual Tags
- Version derived from Git state, not manual tagging
- Branch must match `<major>.<minor>`
- HEAD must match upstream tip
- Repo must be clean
- Patch = first-parent commit count
- SHA always authoritative

---

## Run Config Format

TOML � human-editable, no surprises:

```toml
[repos]
nielsoln_be = "~/projects/nielsoln_site/be"
```

---

## JSONL Schema

All records share a common envelope:

```json
{
  "schema": 1,
  "seq": 42,
  "kind": "run_start",
  ...payload...
}
```

| Field | Purpose |
|-------|---------|
| `schema` | Integer version. Bump if shape changes. Readers handle multiple versions. |
| `seq` | Monotonically increasing across all records, never reused. Key to safe archiving. |
| `kind` | Record type: `run_start`, `run_finish`, `archive_marker`, etc. |

### Record Kinds

**`run_start`**
```json
{
  "schema": 1, "seq": 1, "kind": "run_start",
  "run_id": "abc123",
  "started_at": "2026-04-12T10:00:00Z",
  "repos": { "nielsoln_be": "vshn://repo/nielsoln_be" }
}
```

**`run_finish`**
```json
{
  "schema": 1, "seq": 2, "kind": "run_finish",
  "run_id": "abc123",
  "finished_at": "2026-04-12T10:01:00Z",
  "outcome": "pass",
  "traceback": { "$ref": "vshn://chunks/abc123-traceback" }
}
```

**`archive_marker`**
```json
{
  "schema": 1, "seq": 43, "kind": "archive_marker",
  "archived_up_to_seq": 41,
  "archived_at": "2026-04-12T10:00:00Z",
  "archive": { "$ref": "vshn://archive/2026-04" }
}
```

### Scaling Principle

JSONL is the **cache / latest data**. Old records are rolled off via `archive_marker` records. The archive format is TBD � the `$ref` pointer is in place from day one.

---

## The `$ref` / `vshn://` URI System

Any value in any record can be replaced with a **ref object** instead of inline data:

```json
{ "$ref": "vshn://chunks/abc123-traceback" }
```

This is a first-class concept usable anywhere in the schema � not just for archiving.

### URI Scheme: `vshn://`

Format: **`vshn://<resource-type>/<identifier>`**

| Resource type | Resolves to |
|---|---|
| `chunks` | Local file in `versholn-chunks/` |
| `archive` | Local or remote archive partition |
| `repo` | Loaded repo in in-memory registry |
| `runs` | Record lookup in JSONL |

### Why URIs (not raw paths)?

- Local-first today: `vshn://chunks/x` resolves to a file path
- Remote-ready: a future resolver can fetch from S3, git LFS, etc. � zero schema change
- Inspectable: type is immediately visible without knowing the resolution chain
- Extensible: new resource types = new resolver registrations

### Chunk Naming Convention

```
versholn-chunks/<run_id>-<field>
```

Deterministic, keyed by `run_id` + field name. No lookup table needed.

### Reader Modes

- **Shallow**: ignore refs, fast scan of JSONL only
- **Deep**: resolve refs when needed (paid cost)

---

## Minimal v0 � What Gets Built First

Three layers, in order:

1. **Repo identity** � load from path, resolve SHA + branch via `git`, registry in memory
2. **Run recording** � `start_run()` / `finish_run()`, JSONL, minimal mode only
3. **CLI** � `versholn run <config>` and `versholn list`

Deferred until justified by a second consumer:
- Compat promotion
- Virtual tags
- Standard/full recording levels
- Concurrency hardening

---

## Repo Structure (Planned)

```
versholn/
  versholn.py         ? the whole thing initially
  tests/
    test_paths.py
    test_run_records.py
  README.md
  AGENTS.md
```

---

## Philosophy

- Test invariants, not just functions
- Prefer real Git over mocks
- Capture failures as first-class cases
- Protect minimal-mode performance
- Keep tests fast where possible
- JSONL is the source of truth for recent history; archive format is deliberately deferred

---

## `importx` — Lazy Import for Own Repos

### Problem

In a polyrepo setup, modules from sibling repos (e.g. `geo_tools`) need to be importable both:
- **Locally** — from a checked-out sibling directory, without pip-installing anything
- **In production** — from site-packages (pip-installed via git URL in Dockerfile)

A bare module-level `from geo_tools.roof_footprint import measure_roof_footprint` fails locally unless
the repo is either pip-installed or manually added to `sys.path`. Neither is acceptable as a default.

### The Rule

> **Own-repo symbols are always imported via `versholn.importx()`, called inside functions, never at module level.**

```python
# WRONG — global import, breaks local dev
from geo_tools.roof_footprint import measure_roof_footprint

# RIGHT — lazy, inside function, versholn handles the path
def measure(req):
    measure_roof_footprint = versholn.importx(
        "geo_tools.roof_footprint.measure_roof_footprint"
    )
    return measure_roof_footprint(req.address, req.search_dist_m)
```

### What `importx` Does

1. **Checks a cache** — if this dotted symbol has been imported before, return it immediately (O(1), safe in hot paths)
2. **Locates the package** — using a sibling-directory convention:
   - Finds the calling repo's git root via `git rev-parse --show-toplevel`
   - Looks for `<repo-root-parent>/<top_level_package>/` as a sibling checkout
   - If found, prepends its parent to `sys.path`
   - If not found, falls back to a normal import (site-packages)
3. **Imports and returns the symbol** — caches it, then returns it

### Sibling Convention

All repos in this ecosystem live under one parent directory (e.g. `C:\analytics\projects\git\lunk\`).
`importx` exploits this: given that `versholn` is a sibling of `geo_tools`, any repo that depends on
`geo_tools` can resolve it by walking up one level from its own git root.

This means **no config, no `.env`, no hardcoded paths** — the convention is the configuration.

### `local=` Escape Hatch

If the sibling convention cannot be used (unusual layout), pass an explicit path:

```python
versholn.importx("geo_tools.roof_footprint.measure_roof_footprint", local="/some/other/path")
```

If `local=` is provided and the path does not exist, `importx` raises `ImportError` immediately (hard fail — no fallback).

### Scope: Own Repos Only

`importx` is **not** a general-purpose lazy importer. It is for **own source repos** — code you wrote and
version alongside the calling repo. For third-party packages that might not be installed, a separate
`install_and_import()` function handles that case (pip installs if missing, then imports).

| Function | Use for |
|---|---|
| `versholn.importx(...)` | Own repos (source checkouts or pip-installed via git URL) |
| `versholn.install_and_import(...)` | Third-party packages (PyPI, auto-installs if missing) |

### Caching

The cache is module-level in `versholn.py`, keyed by the full dotted symbol string. It is **not** invalidated
during a process lifetime — `importx` is designed for stable, long-lived imports (typically one per
function call site). It is idempotent: calling it 10,000 times has the same cost as calling it twice.
