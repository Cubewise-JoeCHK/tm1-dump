# tm1-dump

Small, fast TM1py-based CLI that dumps **one TM1 instance to one zip** — and reloads it onto another server for easy migration.

## Status
v0.1 under construction — see [Issues](../../issues) and milestone **v0.1 — zip dump & reload**.

## Usage (planned)
```bash
tm1-dump dump  --address prod.tm1 --port 12354 --user admin --password *** --out project.zip
tm1-dump load  project.zip --address dev.tm1 --port 12354 --user admin --password ***
```

## What ships in the zip
Dimensions, cubes + rules, processes, chores, public views/subsets, cube data (CSV), security (users/groups/memberships/permissions) — TM1 REST JSON per object, plus a `manifest.json`.
