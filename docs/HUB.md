# Alfredo Workflow Hub

Local-first registry for sharing portable workflow packages (`.alfredo.json`).

## Privacy: what is shared vs not

**Shared (logic only):** agents, tasks, specializations, tool *names*, inputs/outputs,
expected exports, DAG graph, suggested models (`provider` + `model_name`), env var *names*
(e.g. `GEMINI_API_KEY`).

**Never shared:** API key *values*, hub tokens, app `api_key`, DB passwords, connection strings,
vector embeddings, workspace files. Each coworker configures their own `.env`.

Packages are sanitized on export and again on hub publish (`sanitize_package_for_share`).

Graph shape for packages follows the function-block model (`input` / `task` / `batch_loop` / `hitl` / `export` + optional `inputs_map`). See [WORKFLOW_BLOCKS.md](WORKFLOW_BLOCKS.md).

## Modes (`HUB_MODE` in `.env`)

| Value | Behavior |
|-------|----------|
| `off` | Only local file export/import (default) — USB / email / chat of `.alfredo.json` |
| `local` | Company hub on your LAN / VPN / Docker |
| `remote` | Point `HUB_API_URL` at a shared/global hub |

```env
HUB_MODE=local
HUB_API_URL=http://hub.internal.company:8010
HUB_USERNAME=alice
HUB_TOKEN=...          # from /hub/register — never commit this
HUB_ORG=mycompany      # for visibility=org
```

## Option A — Private company hub (coworkers only)

1. On a machine reachable only inside the company network (or VPN):

```bash
docker compose --profile hub up -d hub_postgres hub
```

2. Do **not** expose port `8010` / `5433` to the public internet. Firewall / VPN only.

3. Every coworker sets in their `.env`:

```env
HUB_MODE=local
HUB_API_URL=http://<company-hub-host>:8010
HUB_ORG=mycompany
```

4. Each person registers once (UI Share/Hub tab, or curl) and saves `HUB_TOKEN`.

5. When publishing, choose visibility:
   - **`org`** — all users with the same `HUB_ORG` / `org_slug` can see it (best default for the company)
   - **`private`** — only you + usernames listed in “Share with”
   - **`public`** — anyone who can reach *this* hub (still private if the hub is intranet-only)

6. Coworkers: Search → Install. Import reconstructs agents/tasks/workflow locally; they plug in their own API keys.

Hub API: `http://<host>:8010/hub/health`  
Hub DB: Postgres on host port **5433** (`alfredo_hub`) — separate from runtime `alfredo_db`.

Without Docker Postgres, the hub falls back to `db/hub.sqlite` if `HUB_DATABASE_URL` is unset.

## Option B — Global / community hub

1. Run the same hub stack on a public server (or use a hosted URL).
2. Coworkers set:

```env
HUB_MODE=remote
HUB_API_URL=https://hub.example.com
```

3. Prefer **`public`** for open workflows; use **`private` + share** for limited collaboration even on a global hub.

Same package format — only the URL changes.

## Option C — No hub (file only)

`HUB_MODE=off`. Export `.alfredo.json` from Share/Hub or “Export Package”, send the file, Import on the other PC. Maximum privacy; no central catalog.

## PSID company hub (LAN)

For the PSID semi-private company server on LAN (`psid.us`), see **[PSID_HUB.md](PSID_HUB.md)**.

Quick start on the server PC:

```bat
run_psid_hub.bat
```

## Server-side access control (hub container)

| Variable | Default | Description |
|----------|---------|-------------|
| `HUB_REGISTRATION` | `open` | `open` \| `invite` \| `closed` |
| `HUB_INVITE_TOKEN` | — | Required when `HUB_REGISTRATION=invite` |
| `HUB_REQUIRED_ORG` | — | Force all new users into this org slug |
| `HUB_ALLOW_PUBLIC` | `true` | Set `false` to block `visibility=public` publishes |

## Register

```bash
curl -X POST http://localhost:8010/hub/register \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","display_name":"Alice","org_slug":"mycompany"}'
```

Save the returned `token` as `HUB_TOKEN`. Never commit tokens to git.

## Visibility summary

| Visibility | Who can install |
|------------|-----------------|
| `private` | Author + explicit username shares |
| `org` | Same `org_slug` (company) |
| `public` | Anyone authenticated/anonymous who can reach the hub |

## Package format

See `core/workflow_package.py` (`format_version: "1.0"`).
