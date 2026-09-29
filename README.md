# tm1-dump

Small, fast TM1py-based CLI that dumps **one TM1 instance to one zip** — and reloads it onto
another server for easy migration. Dimensions, cubes + rules, processes, chores, public
views/subsets, cube data, and security go in; the same model comes out on the target.

## Status

v0.1 — see [Issues](../../issues) and milestone **v0.1 — zip dump & reload**.

## Installation

```bash
git clone https://github.com/Cubewise-JoeCHK/tm1-dump.git
cd tm1-dump
uv sync          # or: pipx install / pip install .
uv run tm1-dump --help
```

## Quickstart

Copy a whole PROD server onto a DEV box:

```bash
# 1. dump the source (writes prod.zip)
tm1-dump dump --address prod.tm1.internal --port 12354 --user admin --password 'secret' --out prod.zip

# 2. look before you leap — what would be created/overwritten on the target?
tm1-dump load prod.zip --address dev.tm1.internal --port 12354 --user admin --password 'secret' --no-ssl --dry-run

# 3. reload
tm1-dump load prod.zip --address dev.tm1.internal --port 12354 --user admin --password 'secret' --no-ssl
```

Connection settings can also come from environment variables (`TM1_ADDRESS`, `TM1_PORT`,
`TM1_USER`, `TM1_PASSWORD`, `TM1_SSL`, `TM1_NAMESPACE`) or a TM1py-style ini file
(`--config-file`). Precedence: CLI flags > environment > config file.

## Commands

### `tm1-dump dump`

| Option | Meaning |
| --- | --- |
| `--out FILE.zip` | output zip path; default `<ServerName>_<timestamp>.zip` in the current directory |
| `--include TYPE=PATTERN` | export only matching objects; repeatable; `TYPE` is one of the [zip sections](#what-is-inside-the-zip) (`dimensions`, `cubes`, `views`, `subsets`, `processes`, `chores`, `data`, `security`); a bare `PATTERN` applies to every type |
| `--exclude TYPE=PATTERN` | skip matching objects; repeatable; exclude wins over include |
| `--no-data` | skip regular cube data; keeps `}ElementAttributes_*` attribute values; combines on top of `--include`/`--exclude` (both must pass — with `--exclude "data=..."` the zip may carry no data files at all) |
| `--workers N` | parallel export threads (default 8) |
| `--address`, `--port`, `--user`, `--password`, `--ssl` / `--no-ssl`, `--namespace`, `--config-file` | connection options (shared table below) |

Matching is case-insensitive fnmatch (`--include "dimensions=Ac*"`), filters apply per type,
and one failing object never aborts the batch — it is recorded in the manifest's `errors`.

`--no-data` skips regular cube data, keeps element-attribute values (the `}ElementAttributes_*`
control cubes' data); loading into a populated target leaves that target's cube data untouched.
Security is never affected by `--no-data` — permissions ride in `security/*.json`.

Exit codes: `0` ok · `1` some objects failed to export (see `manifest.json` → `errors`) · `2`
bad filters or connection failure.

### `tm1-dump load ZIP`

| Option | Meaning |
| --- | --- |
| `ZIP` | the dump zip to reload (integrity-checked against `manifest.json` before connecting) |
| `--dry-run` | print the create/overwrite/skip plan per type and touch nothing |
| `--clean` | delete matching objects on the target before loading (chores → processes → cubes → dimensions; views/subsets/data go with their parents; **security is never deleted**) |
| `--workers N` | parallel load threads (default 8) |
| `--address`, `--port`, `--user`, `--password`, `--ssl` / `--no-ssl`, `--namespace`, `--config-file` | connection options (shared table below) |

Without `--clean`, existing objects are overwritten in place (`update_or_create`). Loading runs
in dependency order — dimensions → cubes (+rules) → subsets → views → processes → chores →
data → security — parallel within a type, sequential across types. One failing object is
skipped and summarized at the end; everything else still loads.

Exit codes: `0` everything loaded · `1` anything failed (validation, connection, or any
individual object).

### Connection options (both commands)

| Option | Env var | Meaning |
| --- | --- | --- |
| `--address` | `TM1_ADDRESS` | TM1 admin host or IP |
| `--port` | `TM1_PORT` | TM1 HTTP API port |
| `--user` | `TM1_USER` | TM1 user name |
| `--password` | `TM1_PASSWORD` | TM1 password |
| `--ssl` / `--no-ssl` | `TM1_SSL=true/false` | HTTPS (default on, like TM1) or HTTP |
| `--namespace` | `TM1_NAMESPACE` | CAM namespace for SAML/Cognos security mode |
| `--config-file` | — | TM1py-style ini file with a `[tm1]` section |

## What is inside the zip

One zip per server, written atomically (a partial dump never replaces the previous one). The
layout lives in `src/tm1_dump/zipio.py` — that file is the source of truth.

| Zip path | Contents |
| --- | --- |
| `manifest.json` | schema version, tool version, source server/port, filters used, per-type counts, sha256 for every file, export errors |
| `dimensions/<name>.json` | REST `/Dimensions` entity, hierarchies expanded (elements, edges, element attributes) |
| `cubes/<name>.json` | REST `/Cubes` entity including the `Rules` text |
| `views/<cube>/<name>.json` | public views (native and MDX) |
| `subsets/<dimension>/<hierarchy>/<name>.json` | public subsets (static and dynamic) |
| `processes/<name>.json` | REST `/Processes` entity incl. parameters |
| `chores/<name>.json` | chore incl. start time, schedule, tasks with parameters |
| `data/<cube>.csv` | two comment headers (`# cube,<name>`, `# dimensions,<d1>,...`) then one row per non-empty cell `<e1>,...,<eN>,<value>`, sorted, `\r\n` line endings; empty cubes get a header-only CSV |
| `security/users.json` | user entities (TM1 never exposes passwords) |
| `security/groups.json` | group names |
| `security/client_groups.json` | `{client: [groups]}` memberships |
| `security/permissions.json` | `{cubes|dimensions|processes|chores: {object: {group: right}}}` read from the `}CubeSecurity`/`}DimensionSecurity`/`}ProcessSecurity`/`}ChoreSecurity` control cubes |

Object names are percent-encoded into path segments, so names containing `/`, `&` or spaces
stay a single segment (`weird/name` → `weird%2Fname`).

## Performance notes (`--workers`)

- `dump` fans export jobs out per object within each type (`--workers`, default 8) over one
  shared REST connection pool; `load` is parallel within a type and sequential across types.
- The bottleneck in practice is the TM1 server, not the CLI: large cubes export through the
  server-side blob route (`execute_mdx_csv` with `use_blob`), with an automatic plain retry.
- `--workers 1` gives you a fully serial run for troubleshooting. Raising `--workers` helps up
  to roughly the server's thread budget — past that, dumps time out or slow the server down.
  Start at 8, raise for big farms, lower for small/loaded boxes.

## Known limits (v0.1)

- **Passwords never migrate.** TM1's REST API does not expose them. New users are created
  **disabled with a random password** — an admin must set real passwords before they can log
  in (the loader prints a notice listing them). Existing users are only updated profile-only
  (password, groups and enabled state are untouched).
- **Group memberships are applied additively and security is never deleted.** A reload cannot
  remove a group or lock anyone out; `--clean` never touches security objects either.
- **Private views and subsets are out of scope in v0.1** — only public objects are dumped.
- **Chore start times follow the source server's timezone.** Times are replayed as wall-clock
  times on the target; across a DST boundary between the two servers the absolute start time
  can shift by an hour (the loader prints a notice).
- **CAM/namespace security:** pass `--namespace` (or `TM1_NAMESPACE`) to authenticate against
  IBM Cognos CAM. Migrated users still land as TM1-native users.
- **Commas in element names** depend on the TM1 server's CSV quoting for cube data. The loader
  parses RFC-4180 (quoted fields survive), but a row the server mis-quotes fails only that
  cube's load. Dimension names containing commas would break the `# dimensions` header line —
  rename such dimensions before dumping.

## Development

```bash
uv sync
uv run pytest          # 140 tests, incl. a mocked dump→load roundtrip
uv run ruff check .    # lint
uv build               # sdist + wheel
```

The test suite contains an end-to-end roundtrip test: a non-trivial model (multi-hierarchy
dimensions with element attributes, cubes with/without rules, views/subsets, processes with
parameters, a two-task chore, security, cube data) is dumped with the real dump engine and
reloaded with the real load engine against an in-memory TM1 double, then compared object class
by object class.

### Versioning

The package version is derived from git tags (`vX.Y.Z`) via [setuptools-scm](https://setuptools-scm.readthedocs.io/)
— there is no static version in `pyproject.toml`. Builds from tarballs (no `.git`, e.g. release
automation) must set the version explicitly:

```bash
SETUPTOOLS_SCM_PRETEND_VERSION=0.2.3 uv build
```
