# Observability 101 — Setup Guide

## Prerequisites
- Docker Desktop (Windows) — make sure it is running
- Git Bash or PowerShell

---

## Project Structure

```
observability101/
├── app/
│   ├── main.py              ← FastAPI app with OTel + Prometheus metrics
│   ├── requirements.txt
│   └── Dockerfile
├── prometheus/
│   ├── prometheus.yml       ← Scrape config + AlertManager address
│   └── alert_rules.yml      ← Alert definitions (AppDown, HighErrorRate, etc.)
├── alertmanager/
│   └── alertmanager.yml     ← Alert routing and notification config
├── otel/
│   └── otel-collector-config.yml  ← OTel Collector pipeline
├── grafana/
│   └── provisioning/
│       ├── datasources/datasources.yml
│       └── dashboards/
│           ├── dashboards.yml
│           └── notes-app.json   ← Pre-built dashboard
├── tempo-config.yml
├── promtail-config.yml
└── docker-compose.yml
```

---

## Step 1 — Clean up any previous broken state

If you ran `docker compose up` before and got errors, always clean up first:

```bash
docker compose down -v
```

The `-v` flag removes the named volumes so you start completely fresh.

---

## Step 2 — Start the stack

```bash
docker compose up --build
```

Wait about 30 seconds for all containers to become healthy. You should see all
8 containers listed as `Up` when you run:

```bash
docker ps
```

---

## Step 3 — Verify each service

| Service | URL | What you should see |
|---------|-----|---------------------|
| **Your App** | http://localhost:8000/health | `{"status":"ok"}` |
| **Your App metrics** | http://localhost:8000/metrics | Prometheus text output |
| **Prometheus** | http://localhost:9090 | Prometheus UI |
| **AlertManager** | http://localhost:9093 | AlertManager UI |
| **Grafana** | http://localhost:3000 | Dashboard (no login needed) |
| **Loki** | http://localhost:3100/ready | `ready` |
| **Tempo** | http://localhost:3200/ready | `ready` |

---

## Step 4 — Generate some traffic

Open a second terminal and run these commands to create data you can see in Grafana:

```bash
# Create a few notes
curl -X POST http://localhost:8000/notes \
  -H "Content-Type: application/json" \
  -d '{"title": "First note", "content": "Hello observability!"}'

curl -X POST http://localhost:8000/notes \
  -H "Content-Type: application/json" \
  -d '{"title": "Second note", "content": "Traces are working"}'

# List all notes
curl http://localhost:8000/notes

# Get note 1
curl http://localhost:8000/notes/1

# Trigger a 404 (note 99 does not exist)
curl http://localhost:8000/notes/99
```

---

## Step 5 — View in Grafana

1. Open http://localhost:3000
2. Click **Dashboards** in the left sidebar
3. Open **"Notes App — Observability"**
4. You will see request rates, latency, memory and live logs

### View Traces
1. Go to **Explore** (compass icon, left sidebar)
2. Select **Tempo** as the datasource
3. Click **Search** → **Run query** — you will see all recent traces
4. Click any trace to see its spans

### View Alerts
1. Open http://localhost:9093 — this is the AlertManager UI
2. Open http://localhost:9090/alerts — this shows Prometheus alert states

---

## Troubleshooting — Windows Docker Desktop

### "Are you trying to mount a directory onto a file?"

**Cause:** Docker Desktop on Windows sometimes creates an empty *directory* at the
mount destination if the source file did not exist when Docker first ran.

**Fix:**
```bash
# 1. Stop everything and remove volumes
docker compose down -v

# 2. Make sure all config files exist (check your folder structure matches above)

# 3. Start again
docker compose up --build
```

### OTel Collector exits immediately

Check its logs:
```bash
docker compose logs otel-collector
```

The config file must be at `./otel/otel-collector-config.yml` on your machine.

### A container stays in "Created" state (never starts)

```bash
# See why it failed
docker compose logs <service-name>

# Example
docker compose logs alertmanager
docker compose logs prometheus
```

### Prometheus shows "connection refused" for notes-app target

The app container may still be building. Wait 30 seconds and refresh
http://localhost:9090/targets — the `notes-app` target should show **UP** in green.

---

## How the pieces connect

```
Your App (FastAPI)
    │
    ├─── /metrics endpoint ──────────────────► Prometheus (scrapes every 15s)
    │                                               │
    │                                               ├─► evaluates alert_rules.yml
    │                                               │
    │                                               └─► fires alerts ──► AlertManager
    │                                                                          │
    │                                                                          └─► (email/Slack/log)
    │
    ├─── OTLP traces ──► OTel Collector ──────────► Tempo (trace storage)
    │
    └─── stdout logs ──► Promtail ────────────────► Loki (log storage)

Grafana reads from Prometheus + Loki + Tempo and shows everything in one place.
```