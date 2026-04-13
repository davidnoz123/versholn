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

### versholn_db Repo

A new public GitHub repo — `versholn_db` — is the single source of truth for known-good SHA combinations.

```
versholn_db/
  compat.json        ← the current promoted compat set
  history/           ← archived compat records (future)
```

`compat.json` format:
```json
{
  "schema": 1,
  "promoted_at": "2026-04-13T10:00:00Z",
  "repos": {
    "versholn":  { "sha": "4dcd31bc8fa2dba68bd9cc20c825ed9564b3053f", "url": "https://github.com/davidnoz123/versholn.git" },
    "geo_tools": { "sha": "9c94dceb90ae6928afa41ec44dfd079064573ded", "url": "https://github.com/davidnoz123/geo_tools.git", "private": true }
  }
}
```

**SHAs must be full 40-character object SHAs.** Short SHAs are not accepted — `git fetch --depth 1 origin <sha>` requires the full SHA.

**Public repo** — compat records contain only SHAs and metadata, no secrets. Any consumer can fetch
`compat.json` via raw GitHub URL with no auth.

---

### `versholn.bootstrap()` Function

New function in `versholn.py`. Called at container startup (from `entrypoint.py`).

```python
versholn.bootstrap(
    compat_url="https://raw.githubusercontent.com/davidnoz123/versholn_db/main/compat.json",
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
  "compat_url": "https://raw.githubusercontent.com/davidnoz123/versholn_db/main/compat.json",
  "entrypoint": "uvicorn main:app --host 0.0.0.0 --port 8080",
  "version_constraints": {
    "geo_tools": { "min_sha": "9c94dce", "branch": "1.0" }
  }
}
```

The config service itself can be a simple Cloud Run service or a raw file served from versholn_db.

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

### Relation to versholn_db

The config service and versholn_db are complementary:

| | versholn_db (`compat.json`) | Config service |
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

---

## Dependency Manifest — `versholn.toml`

### Problem

The current bootstrap design requires `compat.json` to enumerate every repo explicitly. But the Docker
image should only need to know its **seed repo** — versholn should walk the dependency graph and discover
everything else automatically.

### Per-Repo Manifest

Each repo declares its own dependencies in a `versholn.toml` at its root. Versholn reads this file after
cloning each repo and uses it to discover the next layer of the graph.

```toml
# nielsoln_site/versholn.toml
[repo]
name = "nielsoln_site"
url  = "https://github.com/davidnoz123/nielsoln_site.git"

[deps.geo_tools]
url     = "https://github.com/davidnoz123/geo_tools.git"
private = true
```

```toml
# geo_tools/versholn.toml
[repo]
name = "geo_tools"
url  = "https://github.com/davidnoz123/geo_tools.git"

# no [deps] — leaf node
```

```toml
# versholn/versholn.toml
[repo]
name = "versholn"
url  = "https://github.com/davidnoz123/versholn.git"

# no [deps] — leaf node
```

### Graph Resolution

The Docker image / `entrypoint.py` only specifies the seed:

```
VERSHOLN_SERVICE_ID = "nielsoln_site"
VERSHOLN_COMPAT_URL = "https://raw.githubusercontent.com/.../compat.json"
```

Bootstrap then:
1. Fetches `compat.json` → gets all known-good SHAs
2. Clones seed repo (`nielsoln_site`) at its compat SHA
3. Reads `versholn.toml` → discovers `geo_tools`
4. Clones `geo_tools` at its compat SHA
5. Reads `geo_tools/versholn.toml` → no more deps
6. Graph complete — all repos cloned

`compat.json` remains the SHA authority. The manifest describes graph *shape*; compat provides *versions*.

---

## Version Anchoring

An anchor overrides the compat record for a specific repo — use your chosen SHA regardless of what
compat says. Two anchor levels:

### Committed Anchor (in `versholn.toml`)

Stable long-term pin, committed to the repo. Survives redeployment:

```toml
[deps.geo_tools]
url    = "https://github.com/davidnoz123/geo_tools.git"
anchor = "9c94dce"   # always use this SHA, compat record ignored for this dep
```

### Runtime Anchor (env var)

One-off override for testing without a commit. Set at `docker run` or in the Cloud Run revision:

```
VERSHOLN_ANCHOR_GEO_TOOLS = 9c94dce
```

Naming convention: `VERSHOLN_ANCHOR_<REPO_NAME_UPPERCASE>`. Bootstrap reads all env vars matching
this pattern before resolution begins.

### Precedence

```
Runtime anchor (env var)         → highest — overrides everything, no compat check
Committed anchor (versholn.toml) → overrides compat, subject to on_incompatible policy
compat.json SHA                  → default for all unanchored repos
```

Runtime anchors **skip incompatibility checks** by design — they are explicit developer overrides.

---

## Incompatibility Reactions

### Scenarios

Three distinct incompatibility scenarios can arise during graph resolution:

| Scenario | Description |
|---|---|
| **Constraint violation** | A dep's compat SHA doesn't satisfy the `min_sha` or `branch` constraint in `versholn.toml` |
| **Transitive conflict** | Two repos in the graph require different versions of the same shared dep |
| **Anchor vs compat mismatch** | A committed anchor references a SHA not present in the compat record |

### Per-Dep Reaction Policy

Configured in `versholn.toml` per dep:

```toml
[deps.geo_tools]
url             = "https://github.com/davidnoz123/geo_tools.git"
on_incompatible = "fail"    # default — hard abort, container won't start
                             # "warn"   — log warning, use compat SHA anyway
                             # "anchor" — use committed anchor SHA regardless
```

### System-Wide Defaults

Set at the top of `versholn.toml`:

```toml
[versholn]
on_incompatible     = "fail"     # default for all deps that don't set it explicitly
conflict_resolution = "compat"   # transitive conflict strategy:
                                  # "compat"  — trust compat record (default)
                                  # "latest"  — newest SHA in ancestry order wins
                                  # "strict"  — fail on any transitive conflict
```

### Reaction Reference

| `on_incompatible` | Behaviour |
|---|---|
| `"fail"` (default) | Bootstrap aborts immediately with a structured error log. Container does not start. |
| `"warn"` | Logs a WARNING, continues with the compat SHA. Useful for non-critical deps. |
| `"anchor"` | Uses the committed anchor SHA, logs that compat was bypassed. |

| `conflict_resolution` | Behaviour |
|---|---|
| `"compat"` (default) | Compat record is authoritative for all transitive conflicts. |
| `"latest"` | The SHA that is a descendant of the other wins. |
| `"strict"` | Any transitive conflict aborts bootstrap regardless of `on_incompatible`. |

### Error Log on Incompatibility

```json
{
  "schema": 1, "level": "ERROR", "logger": "versholn.bootstrap",
  "msg": "incompatible dep: constraint not satisfied",
  "repo": "geo_tools",
  "constraint": "min_sha",
  "required": "9c94dce",
  "got": "abc1234",
  "action": "fail"
}
```

---

## Logging Policy

### Why Structured Logs Matter Here

The bootstrap sequence runs before the app starts — before uvicorn, before any framework logging is
active. Without structured logs from that phase, cold-start failures and SHA resolution problems are
very hard to diagnose in Cloud Run. A consistent log format from first byte to last makes the whole
pipeline queryable.

### Library

Python's stdlib `logging` module, configured to emit **JSON to stdout**. No third-party dep.
Cloud Run captures stdout/stderr automatically and makes it queryable in Cloud Logging.

```python
import json, logging, sys

class _JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "schema": 1,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **getattr(record, "extra", {}),
        })

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(_JsonFormatter())
logging.basicConfig(handlers=[handler], level=logging.INFO)
```

Log level controlled by `VERSHOLN_LOG_LEVEL` env var (default: `INFO`). Set `DEBUG` during local dev
to see full SHA resolution steps.

### Log Envelope

All versholn-emitted logs share the JSONL-compatible envelope:

```json
{"schema": 1, "level": "INFO", "logger": "versholn.bootstrap", "msg": "...", ...extra...}
```

This is consistent with the run-record schema — same `schema` field, same structural style — so
bootstrap logs and run records can be processed by the same tooling.

### What to Log Per Phase

| Phase | Level | What | Example extra fields |
|---|---|---|---|
| Bootstrap start | INFO | Service id, compat URL fetched | `service_id`, `compat_url` |
| Repo resolved | INFO | Repo name + SHA that will be cloned | `repo`, `sha` |
| Constraint check pass | DEBUG | Repo + constraint satisfied | `repo`, `constraint` |
| Constraint check fail | ERROR | Repo + which constraint failed | `repo`, `constraint`, `required`, `got` |
| Clone complete | INFO | Repo, SHA, duration ms | `repo`, `sha`, `duration_ms` |
| versholn self-update | INFO | Old SHA → new SHA | `old_sha`, `new_sha` |
| Bootstrap complete | INFO | Total duration, repos cloned | `duration_ms`, `repos` |
| Entrypoint exec | INFO | Command about to exec | `entrypoint` |
| App error | ERROR | Exception type + message only | `exc_type`, `exc_msg` |

### What Must Never Be Logged

- `GITHUB_PAT` or any credential / secret
- Full tracebacks in minimal mode (exception type + message only)
- PII — addresses, names, or any user-supplied data
- Full git URLs that contain the PAT (strip auth before logging)

### Stderr vs Stdout

- `INFO` / `DEBUG` → **stdout** (Cloud Run streams these to Cloud Logging as plain logs)
- `WARNING` / `ERROR` / `CRITICAL` → **stderr** (Cloud Run flags these as error-severity automatically)

### Run Records vs Logs

Versionholn run records (`run_start`/`run_finish` JSONL) are **not** logs — they are the audit trail
for versholn-tracked operations and live in `runs.jsonl`. Logs are ephemeral and go to stdout/stderr.
The two are complementary: logs for real-time observability, run records for post-hoc analysis.

---

## Local Dev — The Checkout-is-Truth Rule

When running locally, versholn **never consults the compat record and never clones anything**. Whatever
SHA is currently checked out in each sibling directory is what runs. This is intentional:

> **Locally, your working tree is authoritative. Compat is a production concern.**

This means:
- `importx` uses the sibling convention — `../geo_tools/` as-is, no version check
- `versholn.toml` is not read for resolution locally — only for tooling/docs purposes
- Dirty repos, unpushed commits, and experimental branches all work without ceremony
- `bootstrap()` and `entrypoint.py` are never invoked locally

The `[deps]` section in `versholn.toml` still serves a useful purpose locally: it documents which
sibling repos are expected to exist. A future `versholn check` command can warn if a declared dep is
missing from your local sibling tree.

---

## Operational Safety

### Unreachable Compat Record

If `compat.json` cannot be fetched at cold start (network error, versholn_db down):

| Behaviour | Setting | When to use |
|---|---|---|
| **Fail closed** (default) | `on_compat_unavailable = "fail"` | Production — never start with unknown deps |
| **Use baked fallback** | `on_compat_unavailable = "baked"` | High-availability services — start with baked versholn, log WARNING |

The baked fallback uses whatever was pip-installed into the image at build time. Combined with Docker
layer caching (SHA-pinned deps), this gives a safe fallback without cloning anything.

Configure in `versholn.toml`:
```toml
[versholn]
on_compat_unavailable = "fail"   # default
```

Or via env var for per-revision override:
```
VERSHOLN_ON_COMPAT_UNAVAILABLE = baked
```

### Rollback

If a bad compat record causes a container crash loop, the fix does not require an image rebuild:

1. **Fast path** — set `VERSHOLN_ANCHOR_<REPO>=<known-good-sha>` in the Cloud Run revision env vars
2. **Stable path** — point `VERSHOLN_COMPAT_URL` to a pinned historical record:
   ```
   VERSHOLN_COMPAT_URL = https://raw.githubusercontent.com/davidnoz123/versholn_db/main/history/2026-04-13-compat.json
   ```
3. **Nuclear option** — Cloud Run revision rollback to a previous revision (reverts all env vars + image)

The `history/` directory in versholn_db is kept for exactly this purpose — every promoted compat record
is archived with a timestamp before being overwritten.

### Cold Start Latency

`git clone` on every cold start adds latency (typically 2–5s depending on repo size). Mitigations:

| Mitigation | Trade-off |
|---|---|
| `min-instances = 1` in Cloud Run | Keeps one instance warm; small ongoing cost |
| `--depth 1` shallow clone | Fastest clone; SHA ancestry checks not possible (affects `min_sha` constraint) |
| Deps layer in Docker image | Bake frequently-used stable deps; only clone unstable/changing ones |
| Clone cache volume | Mount a persistent volume; re-use clones across restarts (Cloud Run not yet supported) |

Default recommendation: use `--depth 1` shallow clones + `min-instances = 1` for production services.

### Multiple PATs

If different private repos require different credentials:

```
VERSHOLN_PAT_GEO_TOOLS = ghp_...    ← repo-specific (VERSHOLN_PAT_<REPO_NAME_UPPERCASE>)
GITHUB_PAT             = ghp_...    ← fallback for all other private repos
```

Bootstrap checks for a repo-specific PAT first, falls back to `GITHUB_PAT`.

---

## CLI (Planned)

The Minimal v0 design defers the CLI. Bootstrap adds the following planned commands:

| Command | Description |
|---|---|
| `versholn promote` | Resolve current local SHA set, validate compat constraints, write + push `compat.json` to versholn_db |
| `versholn check` | Validate local checkouts against current compat record; warn on dirty/mismatched repos |
| `versholn anchor <repo> <sha>` | Set a committed anchor in `versholn.toml` for a repo |
| `versholn run <config>` | (v0) Run a configured set of repos as a versholn session |
| `versholn list` | (v0) List registered repos and their run history |

`versholn promote` is the critical path for the bootstrap architecture — it is how a developer publishes
a new set of known-good SHAs after testing locally.

---

## Local Docker Testing Workflow

Before deploying to Cloud Run, test the full bootstrap locally:

```powershell
# 1. Build the generic image
docker build -t nielsoln-be be/

# 2. Run with bootstrap config — simulates Cloud Run environment
docker run --rm `
  -e VERSHOLN_COMPAT_URL=https://raw.githubusercontent.com/davidnoz123/versholn_db/main/compat.json `
  -e VERSHOLN_SERVICE_ID=nielsoln_site `
  -e GITHUB_PAT=<your-pat> `
  -p 8080:8080 `
  nielsoln-be

# 3. Verify
curl http://localhost:8080/api/health
```

Add this to AGENTS.md alongside the local uvicorn command once `entrypoint.py` is implemented.

---

## Multi-Environment Compat Records

The same generic image can target different environments simply by pointing `VERSHOLN_COMPAT_URL` at
a different record. No image rebuild, no config change — just an env var swap.

### Structure in versholn_db

```
versholn_db/
  compat.json             ← prod (promoted, validated)
  staging/compat.json     ← staging (newer, less validated)
  dev/compat.json         ← latest main of each repo (auto-updated by CI)
  history/                ← archived prod records for rollback
    2026-04-13-compat.json
```

### Per-Environment URL Convention

| Environment | `VERSHOLN_COMPAT_URL` |
|---|---|
| Production | `.../versholn_db/main/compat.json` |
| Staging | `.../versholn_db/main/staging/compat.json` |
| Dev | `.../versholn_db/main/dev/compat.json` |

Staging can run newer SHAs than prod without any image difference. The same `nielsoln-be` image serves
all three environments — differentiated only by the compat URL in the Cloud Run service config.

---

## Health Endpoint — Bootstrap Metadata

Post-bootstrap, the `/api/health` endpoint should surface which SHA set is actually running. This gives
an instant answer to "what's deployed?" without digging into logs.

### Response Shape

```json
{
  "ok": true,
  "versholn_sha": "4c63ed9",
  "repos": {
    "geo_tools": "9c94dce",
    "versholn":  "4c63ed9"
  },
  "compat_url": "https://raw.githubusercontent.com/davidnoz123/versholn_db/main/compat.json",
  "bootstrapped_at": "2026-04-13T10:00:00Z"
}
```

Versholn populates a module-level `_bootstrap_state` dict after `bootstrap()` completes. The health
endpoint reads from it — no additional work per request.

The extended shape is **additive** — older clients that only check `ok: true` continue to work.

---

## `runs.jsonl` Persistence in Production

Run records are currently designed for a local filesystem. Cloud Run has an **ephemeral filesystem** —
records are lost on every restart, scale-out, or redeploy.

### Options

| Option | Complexity | Cost | Suitable for |
|---|---|---|---|
| **Ephemeral** (current) | None | None | Dev/local only |
| **Stream to Cloud Logging** | Low | Low | Audit trail via log sink |
| **Write to GCS bucket** | Medium | Negligible | Durable records + replay |
| **Cloud Run volume (NFS)** | Medium | Low | Shared across instances |

### Recommended path

Phase 1 — Stream minimal run records to Cloud Logging as structured JSON (same envelope as
`run_start`/`run_finish`). Cloud Logging retains for 30 days by default; export sink to GCS for
longer retention. Zero infra to set up.

Phase 2 — When run record volume or query requirements grow, write directly to GCS in JSONL. The
`$ref` / `vshn://` URI system already anticipates remote storage — just register a GCS resolver.

### Decision Needed Before Implementation

`start_run()` / `finish_run()` need to know their write target. Controlled by `versholn.toml`:

```toml
[versholn]
run_record_target = "stdout"   # default — ephemeral, goes to Cloud Logging
                                # "gcs"    — write to GCS (requires VERSHOLN_GCS_BUCKET env var)
                                # "file"   — local file (local dev default)
```

---

## Optional Dependencies

Not all deps are hard requirements. A `required = false` flag allows graceful degradation if a dep
cannot be resolved (missing from compat, clone fails, private and no PAT):

```toml
[deps.analytics_tools]
url      = "https://github.com/davidnoz123/analytics_tools.git"
required = false   # log WARNING, continue without it
```

Code that uses an optional dep must guard with `versholn.importx()` catching `ImportError`:

```python
def get_analytics():
    try:
        return versholn.importx("analytics_tools.core.Client")
    except ImportError:
        return None   # feature unavailable, degrade gracefully
```

---

## Image Tagging Convention

With a generic image, `latest` is ambiguous — "latest" image + any compat record = any behaviour.
Tag images by the **baked versholn SHA** to make the bootstrapper version auditable:

```
gcr.io/plucky-snowfall-442701-s1/nielsoln-be:vshn-4c63ed9
```

The image tag identifies the bootstrapper. The actual app and dep SHAs come from the compat record
at runtime. Together they fully define what ran:

```
image tag (bootstrapper version) + compat_url + compat SHA set = fully reproducible deploy
```

Update `cloudbuild.yaml` to tag by versholn SHA once bootstrap is implemented.

---

## Bootstrap Failure Alerting — Startup Probe

Cloud Run supports **startup probes** — if the health endpoint doesn't return 200 within the probe
window, Cloud Run marks the instance unhealthy and routes traffic to other instances.

### Recommended probe config (Cloud Run service YAML)

```yaml
containers:
  startupProbe:
    httpGet:
      path: /api/health
      port: 8080
    initialDelaySeconds: 5    # allow time for git clones
    periodSeconds: 5
    failureThreshold: 6       # 30s total before Cloud Run gives up
    timeoutSeconds: 3
```

`initialDelaySeconds` should be tuned to expected cold-start clone time. If bootstrap fails (bad
compat record, unreachable repo), the health endpoint never returns 200 → probe fails → Cloud Run
automatically rolls back to the previous revision.

This makes the startup probe the **first line of defence** against a bad compat record deploy.
