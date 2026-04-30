# OBSERVABILITY.md — Reflection

## What I Built

I built a simple **Notes API** using **Python + FastAPI**. The app has four REST endpoints:

| Method | Endpoint | What it does |
|--------|----------|--------------|
| GET | `/health` | Returns `{"status": "ok"}` |
| POST | `/notes` | Creates a new note |
| GET | `/notes` | Lists all notes |
| GET | `/notes/{id}` | Gets a single note by ID |
| DELETE | `/notes/{id}` | Deletes a note |

The entire stack runs with one command: `docker compose up --build`

---

## The Observability Stack

### What is observability?

Observability means being able to understand what your app is doing from the *outside*, by looking at the data it produces. There are three main signals:

- **Metrics** — numbers over time (how many requests? how much memory?)
- **Logs** — text records of events ("Note created with id=5")
- **Traces** — a map of how a single request moved through the system

### Tools used

| Tool | Role |
|------|------|
| **Prometheus** | Scrapes and stores metrics from `/metrics` every 15 seconds |
| **AlertManager** | Receives alerts from Prometheus and routes notifications |
| **Loki** | Stores and indexes log lines |
| **Promtail** | Reads log files and ships them to Loki |
| **Tempo** | Stores distributed traces |
| **OpenTelemetry Collector** | Receives traces from the app and forwards to Tempo |
| **Grafana** | Visualises metrics, logs and traces in one dashboard |

---

## Part 1 — Metrics with Prometheus

### How metrics work

I added the `prometheus-client` Python library to the app. This library lets you define counters and histograms that Prometheus can read.

I defined two metrics:

```python
REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status_code"]
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "endpoint"]
)
```

A **Counter** only ever goes up (total number of requests). A **Histogram** records the *distribution* of values (most requests take 10ms, some take 200ms, a few take 1s).

I then added a **middleware** — a piece of code that runs around every request — that increments these metrics automatically:

```python
class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        start = time.time()
        response = await call_next(request)
        duration = time.time() - start
        REQUEST_COUNT.labels(request.method, request.url.path, response.status_code).inc()
        REQUEST_LATENCY.labels(request.method, request.url.path).observe(duration)
        return response
```

The `/metrics` endpoint exposes all these numbers in a text format that Prometheus understands.

### What I learned

I learned that Prometheus uses a **pull model** — it comes to your app and fetches metrics, rather than you pushing them out. This is the opposite of most logging systems. The advantage is that Prometheus knows if your app has gone silent (disappeared), which you cannot detect with a push model.

---

## Part 2 — Logging with Loki and Promtail

### How log shipping works

The app writes logs using Python's standard `logging` module. In Docker, these go to the container's stdout/stderr and are also written to a shared volume (`/var/log/app/`).

**Promtail** is a sidecar service that reads those log files and pushes them to **Loki**. Loki stores logs indexed by *labels* (like `job="notes-app"` or `level="ERROR"`), which makes querying fast.

In Grafana, I can query logs with:

```
{job="notes-app"}               # All logs from the app
{job="notes-app", level="ERROR"} # Only errors
```

### What I learned

I learned the difference between a log *shipper* (Promtail) and a log *store* (Loki). Unlike Elasticsearch, Loki does not index the full log content — only the labels. This makes it much cheaper to run but means you have to filter using labels first, then search within those results.

---

## Part 3 — Tracing with Tempo and OpenTelemetry

### What is a trace?

A **trace** represents the journey of a single request through your system. Each step in that journey is a **span**. Even in a simple app, a single HTTP request might create spans for: receiving the request → routing → executing business logic → returning the response.

### How I instrumented the app

I used the **OpenTelemetry SDK** for Python. First I created a tracer and pointed it at the OTel Collector:

```python
provider = TracerProvider(resource=Resource({"service.name": "notes-app"}))
otlp_exporter = OTLPSpanExporter(endpoint="http://otel-collector:4317", insecure=True)
provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("notes-app")
```

Then I wrapped each endpoint's logic in a span:

```python
@app.post("/notes")
def create_note(note: NoteIn):
    with tracer.start_as_current_span("create_note") as span:
        span.set_attribute("note.title", note.title)
        # ... business logic ...
```

I also used `FastAPIInstrumentor.instrument_app(app)` which automatically creates spans for all incoming HTTP requests without manual wrapping.

### The OTel Collector

Rather than sending traces directly from the app to Tempo, the app sends to the **OpenTelemetry Collector** (a separate container). The collector then forwards to Tempo. This pattern is useful because:

- The app only needs to know one address (the collector)
- You can add/change backends (e.g., add Jaeger) without changing app code
- The collector can batch, filter, and enrich spans before forwarding

### What I learned

I learned that the trace ID is the key that links all three signals together. A log line can include the current trace ID, so from a single Grafana log entry you can click directly to the trace for that request. This "correlation" is what makes observability much more powerful than just having separate logs and metrics.

---

## Part 4 — Alerting with AlertManager

### How alerting works

Alerting has two distinct parts:

1. **Prometheus evaluates alert rules** — it runs PromQL queries on a schedule and marks an alert as *firing* if the condition is true for a sustained period (`for: 2m`).
2. **AlertManager handles the firing alert** — it decides who to notify, when, and avoids sending duplicate notifications.

### My alert rules

I defined four rules in `prometheus/alert_rules.yml`:

| Alert | Condition | Severity |
|-------|-----------|----------|
| `AppDown` | App unreachable for 1 minute | critical |
| `HighErrorRate` | >10% of requests return 5xx | warning |
| `SlowResponses` | p95 latency > 1 second | warning |
| `HighMemoryUsage` | Memory > 200 MB | warning |

### Routing and inhibition

In `alertmanager/alertmanager.yml`, I configured:

- **Routing**: Critical alerts get sent immediately (10s group wait), warnings use the defaults.
- **Inhibition**: If `AppDown` (critical) is firing, suppress all `warning` alerts for the same app. There is no point alerting about slow responses if the app is completely down.

### What I learned

I learned that the `for` clause in alert rules is important — it prevents flapping. Without it, a brief spike in errors would fire and immediately resolve, creating noisy notifications. The `for: 2m` means the condition must be consistently true for two minutes before AlertManager is notified at all.

I also learned that AlertManager is not a notification sender on its own — it is a *deduplication and routing engine*. The same alert firing three times in a row should only produce one notification, and AlertManager handles that automatically via `group_interval` and `repeat_interval`.

---

## Part 5 — Grafana Dashboard

I built a dashboard with five panels:

| Panel | Type | Query |
|-------|------|-------|
| Total requests received | Stat | `sum(http_requests_total)` |
| Requests per second | Time series | `rate(http_requests_total[1m])` |
| Memory used | Time series | `process_resident_memory_bytes` |
| Active alerts | Alert list | AlertManager integration |
| Live log stream | Logs | `{job="notes-app"}` |

### What I learned

I learned that Grafana is just a visualisation layer — it does not store any data itself. It queries Prometheus for metrics, Loki for logs, and Tempo for traces. The power is that all three appear in one interface, and trace IDs in log lines become clickable links to the full trace.

---

## Biggest Challenges

**1. Understanding the difference between the OTel Collector and Tempo**

At first I thought OpenTelemetry *was* the storage backend. It took me a while to understand that OpenTelemetry is a *standard* for how to emit and transport telemetry, while Tempo is the *storage backend*. The Collector is the middleman that receives OTel-format data and can forward it to many different backends.

**2. Getting traces to appear in Grafana**

The datasource in Grafana needed to point to Tempo's HTTP port (3200), not the gRPC port (4317). The gRPC port is only for the Collector to write traces, not for Grafana to read them.

**3. The `for` clause in alert rules**

I initially wrote alerts without `for:` and they fired and resolved almost instantly during testing. Adding `for: 2m` made them behave realistically.

---

## What I Would Add Next

- **SLO dashboards**: Define a Service Level Objective (e.g., "99% of requests succeed") and have Grafana show a burn rate.
- **Persistent Grafana dashboards via JSON provisioning**: Currently the dashboard is built manually through the UI. The next step is to export it as JSON and provision it automatically so it appears on every fresh `docker compose up`.
- **Real AlertManager notifications**: Wire up Slack or email so alerts are actually delivered, not just logged.
- **Exemplars**: Link individual Prometheus data points to specific trace IDs, enabling one-click drill-down from a metric spike to the exact trace that caused it.