# PSID Company Hub (LAN)

Guide to run this Windows PC as the **PSID** company server for semi-private workflow sharing and shared local model inference.

See also: [HUB.md](HUB.md) for general hub concepts.

## What runs where

| Component | Where | Port | Purpose |
|-----------|-------|------|---------|
| Alfredo Hub | This PC (Docker) | 8010 | Publish / search / install workflow packages |
| Ollama | This PC (Windows service) | 11434 | GPU inference for colleagues |
| LM Studio (optional) | This PC | 1234 | OpenAI-compatible local models |
| Alfredo dashboard | Each colleague's PC | 8501 | Build and run workflows locally |

Workflows are **not** executed on the hub server. The hub only stores portable logic (`.alfredo.json`). Each colleague runs agents on their own machine and may point `OLLAMA_API_BASE` at `http://psid.us:11434` to use this server's GPU.

## 1. Server setup (this PC)

### 1.1 LAN hostname `psid.us`

Add to `C:\Windows\System32\drivers\etc\hosts` (as Administrator):

```
<YOUR_LAN_IP>  psid.us
```

Template: [deploy/psid/hosts.example.txt](../deploy/psid/hosts.example.txt)

Find your LAN IP: `ipconfig` → IPv4 Address (e.g. `192.168.1.50`).

Repeat the same hosts entry on every colleague PC.

### 1.2 Hub environment

Copy settings from [deploy/psid/.env.hub.example](../deploy/psid/.env.hub.example) into the project root `.env`:

```env
HUB_REGISTRATION=invite
HUB_INVITE_TOKEN=<shared-secret-for-colleagues>
HUB_REQUIRED_ORG=psid
HUB_ALLOW_PUBLIC=false
```

Generate a strong invite token (e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`).

### 1.3 Start the hub

```bat
run_psid_hub.bat
```

Or manually:

```bash
docker compose --profile hub up -d hub_postgres hub
```

Verify: [http://psid.us:8010/hub/health](http://psid.us:8010/hub/health) should return `{"status":"ok",...}`.

### 1.4 Ollama on LAN

Ollama must listen on all interfaces, not only localhost.

1. Set Windows system environment variable: `OLLAMA_HOST=0.0.0.0:11434`
2. Restart the Ollama service (Services → Ollama → Restart)
3. Test from another PC: `curl http://psid.us:11434/api/tags`

### 1.5 LM Studio (optional)

LM Studio → Developer → Local Server → enable network access on port `1234`.

### 1.6 Windows Firewall

Run PowerShell **as Administrator** (replace profile if needed):

```powershell
New-NetFirewallRule -DisplayName "Alfredo PSID Hub" -Direction Inbound -Protocol TCP -LocalPort 8010 -Action Allow -Profile Private
New-NetFirewallRule -DisplayName "Ollama LAN" -Direction Inbound -Protocol TCP -LocalPort 11434 -Action Allow -Profile Private
New-NetFirewallRule -DisplayName "LM Studio LAN" -Direction Inbound -Protocol TCP -LocalPort 1234 -Action Allow -Profile Private
```

**Do not** expose ports 8010 or 5433 to the public internet. LAN or VPN only.

## 2. Colleague onboarding

### 2.1 Install Alfredo locally

Each colleague clones Alfredo and runs it (Docker or venv). See [README.md](../README.md).

### 2.2 Client `.env`

Copy [deploy/psid/.env.client.example](../deploy/psid/.env.client.example):

```env
HUB_MODE=local
HUB_API_URL=http://psid.us:8010
HUB_ORG=psid
OLLAMA_API_BASE=http://psid.us:11434
```

### 2.3 Register on the hub

1. Open Alfredo → Workflow Assembler → Share / Hub
2. Hub connection settings: Mode=`local`, URL=`http://psid.us:8010`, Org=`psid`
3. Register: enter username, display name, org `psid`, and the **invite token** (shared by admin)
4. Token is saved to `.env` as `HUB_TOKEN`

### 2.4 Use shared workflows

1. Share / Hub → Hub registry → Search hub
2. Install a package (rebuilds agents/tasks locally)
3. Fill in own API keys in `.env` for any cloud models referenced by the workflow

### 2.5 Use server GPU models

API Vault → Load Models → Configure Local Providers → Ollama:

- Base URL: `http://psid.us:11434`
- Connect & Refresh

Register models in the local registry. Execution will call the server's Ollama.

## 3. Publishing (admin / author)

1. Build workflow in Workflow Assembler
2. Share / Hub → Publish
3. Visibility: **org** (visible to everyone with `HUB_ORG=psid`)
4. Colleagues search and install

## 4. Security model

| Control | Setting |
|---------|---------|
| Registration | `HUB_REGISTRATION=invite` + `HUB_INVITE_TOKEN` |
| Org lock | `HUB_REQUIRED_ORG=psid` |
| No public packages | `HUB_ALLOW_PUBLIC=false` |
| Network | LAN/VPN only; firewall blocks WAN |
| Secrets | Never in packages; each user keeps own `.env` |

## 5. Troubleshooting

| Problem | Fix |
|---------|-----|
| `Hub not reachable` | Hub running? `docker ps`. Hosts file correct? `ping psid.us` |
| Registration rejected | Wrong invite token; hub `HUB_REGISTRATION=invite` |
| Ollama unreachable | `OLLAMA_HOST=0.0.0.0:11434`, firewall rule, Ollama service running |
| No packages in search | Publish with `org` visibility; colleague has same `HUB_ORG=psid` and valid token |
| Models missing after install | API Vault → Connect Ollama on server URL; check compatibility notes on import |

## 6. Hub API reference

Base URL: `http://psid.us:8010`

| Endpoint | Auth | Description |
|----------|------|-------------|
| `GET /hub/health` | No | Status + registration mode |
| `POST /hub/register` | No | Create user (`invite_token` if invite mode) |
| `POST /hub/packages` | Yes | Publish workflow package |
| `GET /hub/packages` | Optional | Search catalog |
| `GET /hub/packages/{id}` | Optional | Download package |

Auth headers: `X-Hub-Username`, `X-Hub-Token`.
