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

---

## Runtime Bootstrap — Cloud Deploy Architecture

### Problem

The current production Dockerfile bakes own-repo deps (`versholn`, `geo_tools`) into the image via
`pip install git+https://...`. This means **every dep change requires a full Docker rebuild and redeploy**,
adding several minutes of friction. The image is also tightly coupled to specific repo states that were
current at build time.

### Vision

> The Docker image ships only a minimal baked versholn. On startup, versholn fetches a compat record,
> clones all deps at their pinned SHAs (including a fresh versholn if needed), and hands control to the
> updated system. **Updating a dep requires no image rebuild — only a compat record update.**

---

### versholn-db Repo

A new public GitHub repo — `versholn-db` — is the single source of truth for known-good SHA combinations.

```
versholn-db/
  compat.json        ← the current promoted compat set
  history/           ← archived compat records (future)
```

`compat.json` format:
```json
{
  "schema": 1,
  "promoted_at": "2026-04-13T10:00:00Z",
  "repos": {
    "versholn":  { "sha": "4c63ed9", "url": "https://github.com/davidnoz123/versholn.git" },
    "geo_tools": { "sha": "9c94dce", "url": "https://github.com/davidnoz123/geo_tools.git", "private": true }
  }
}
```

**Public repo** — compat records contain only SHAs and metadata, no secrets. Any consumer can fetch
`compat.json` via raw GitHub URL with no auth.

---

### `versholn.bootstrap()` Function

New function in `versholn.py`. Called at container startup (from `entrypoint.py`).

```python
versholn.bootstrap(
    compat_url="https://raw.githubusercontent.com/davidnoz123/versholn-db/main/compat.json",
    clone_root="/app/deps",
    pat=os.environ.get("GITHUB_PAT"),
)
```

**What it does:**

1. Fetches `compat.json` via `urllib` (no extra deps)
2. For each repo in the compat record:
   - Shallow-clones to `<clone_root>/<repo>/` at the specified SHA
   - For private repos: injects PAT into clone URL
   - Prepends the cloned repo dir to `sys.path`
3. If versholn itself is in the compat record and its SHA differs from the baked version:
   - Clones the new versholn, reloads the module (`importlib.reload`)
   - Returns the reloaded versholn module so the caller can rebind it

**git clone mechanics note:** `git clone --depth 1` on a specific SHA doesn't work directly. Approach:
```bash
git init <dir>
git remote add origin <url>
git fetch --depth 1 origin <sha>
git checkout FETCH_HEAD
```
SHA is verified after checkout.

---

### `entrypoint.py` — Two-Stage Startup

New file in `be/`. Replaces `uvicorn main:app ...` as the container `CMD`.

```
Stage 1 (baked versholn):
  → fetch compat.json
  → if versholn SHA differs: clone new versholn, reload module
  → rebind versholn to updated version

Stage 2 (fresh versholn):
  → call versholn.bootstrap() for all remaining repos
  → exec uvicorn main:app --host 0.0.0.0 --port $PORT
```

`exec` is used (not subprocess) so uvicorn becomes PID 1 — Cloud Run signal handling works correctly.

---

### Dockerfile (Simplified)

Once bootstrap is in place, `GITHUB_TOKEN` is no longer needed at build time:

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel
RUN pip install --no-cache-dir -r requirements.txt

# Bake in the versholn bootstrapper only
RUN pip install --no-cache-dir "git+https://github.com/davidnoz123/versholn.git#egg=versholn"

COPY . .

ENV PORT=8080
CMD ["python", "entrypoint.py"]
```

No `ARG GITHUB_TOKEN`. No `secretEnv` in `cloudbuild.yaml`. PAT moves to Cloud Run runtime env.

---

### PAT — Build Time → Runtime

| | Current | After bootstrap |
|---|---|---|
| PAT needed at | Docker build | Container startup |
| PAT lives in | Cloud Build Secret Manager | Cloud Run secret env var |
| Secret name | `github-pat` | `github-pat` (same, already exists) |
| Exposed to | Cloud Build SA | Cloud Run identity only |

Cloud Run already has Secret Manager access — `GITHUB_PAT` can be mounted as an env var directly from
`github-pat` secret in the Cloud Run service config.

---

### Compat Promotion (Deferred)

How and who updates `compat.json` is deferred. Options:
- **Manual** — developer runs a versholn CLI command after validating, which writes and pushes `compat.json`
- **Automated** — CI promotes after a test suite passes

The versholn design (run recording → compat promotion) is already architected for this. Promotion logic
will be added to versholn when a second consumer exists.

---

### Local Dev — Unchanged

Locally, `importx` and the sibling convention continue to work as-is. `bootstrap()` is never called
locally — `entrypoint.py` is only the production container entry point. The local server command
(in AGENTS.md) remains the same.

---

## Runtime Configuration — Making the Image Generic

### Problem

If the per-service config (which repos to clone, what to run) is baked into the image, changing the
config requires a rebuild. The goal is an image that is **completely generic** — same image, different
behaviour, depending only on what is passed in or fetched at startup.

### Approach 1 — Environment Variables

The simplest form. Pass config to `docker run` or set it in the Cloud Run revision:

```
VERSHOLN_COMPAT_URL   = URL to fetch compat.json from
VERSHOLN_SERVICE_ID   = logical name of this service (e.g. "nielsoln-be")
VERSHOLN_ENTRYPOINT   = command to exec after bootstrap (e.g. "uvicorn main:app --host 0.0.0.0 --port 8080")
GITHUB_PAT            = PAT for private repo cloning
```

`entrypoint.py` reads these vars. The image itself has no hardcoded opinion about which repos it needs
or what it runs — that is all supplied at deploy time.

**Locally:**
```powershell
docker run -e VERSHOLN_COMPAT_URL=... -e VERSHOLN_SERVICE_ID=nielsoln-be -e GITHUB_PAT=... nielsoln-be
```

**Cloud Run:**
Env vars set in Cloud Run service config; `GITHUB_PAT` mounted from Secret Manager.

### Approach 2 — Config Service (Call-Out on Startup)

Instead of passing config as env vars, the container calls a versholn config endpoint at startup:

```
GET <VERSHOLN_CONFIG_URL>/config/<VERSHOLN_SERVICE_ID>
→ returns: repos to clone, version constraints, entrypoint command
```

The container only needs two env vars: `VERSHOLN_CONFIG_URL` and `VERSHOLN_SERVICE_ID`.
Everything else — which repos, which SHAs, what to run — is served dynamically by the config service.

```json
{
  "schema": 1,
  "service": "nielsoln-be",
  "compat_url": "https://raw.githubusercontent.com/davidnoz123/versholn-db/main/compat.json",
  "entrypoint": "uvicorn main:app --host 0.0.0.0 --port 8080",
  "version_constraints": {
    "geo_tools": { "min_sha": "9c94dce", "branch": "1.0" }
  }
}
```

The config service itself can be a simple Cloud Run service or a raw file served from versholn-db.

### Version Constraints

The compat record pins SHAs absolutely. Version constraints (in the service config) express requirements
that the compat resolver must satisfy before cloning:

| Constraint | Meaning |
|---|---|
| `min_sha` | The cloned SHA must be a descendant of this SHA |
| `branch` | The SHA must be on this branch (maps to virtual tag `<major>.<minor>`) |
| `exact_sha` | Override compat record — use this SHA regardless |

If the compat record cannot satisfy the constraints, bootstrap fails loudly with a clear error.
This is the **compatibility check** — the core of the versholn design.

### Precedence

```
exact_sha in service config  →  highest priority (hard override)
compat.json SHA              →  normal case
min_sha constraint           →  validation only, does not select
```

### Relation to versholn-db

The config service and versholn-db are complementary:

| | versholn-db (`compat.json`) | Config service |
|---|---|---|
| Stores | Known-good SHA combinations | Per-service identity and requirements |
| Updated by | Compat promotion (after testing) | Developer when service config changes |
| Read by | `bootstrap()` at startup | `entrypoint.py` before bootstrap |
| Scope | Ecosystem-wide | Per service |

### Combined Startup Sequence (Full Picture)

```
1. Container starts
2. entrypoint.py reads VERSHOLN_CONFIG_URL + VERSHOLN_SERVICE_ID
3. Fetches service config → gets: compat_url, entrypoint, version_constraints
4. Fetches compat.json from compat_url → gets: repo SHAs
5. Validates SHAs against version_constraints → fail fast if not satisfied
6. versholn.bootstrap() clones all repos at validated SHAs
7. versholn self-updates if its SHA in compat differs from baked
8. exec <entrypoint>  ← uvicorn/other becomes PID 1
```
