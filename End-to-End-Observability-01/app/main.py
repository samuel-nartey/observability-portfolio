import logging
import os
import time
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from starlette.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware

# ── Prometheus ────────────────────────────────────────────────────────────────
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

# ── OpenTelemetry ─────────────────────────────────────────────────────────────
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor

# ── Logging setup — writes to BOTH stdout and a file Promtail can read ────────
LOG_DIR = "/var/log/app"
os.makedirs(LOG_DIR, exist_ok=True)

log_formatter = logging.Formatter(
    '%(asctime)s %(levelname)s [%(name)s] '
    '[trace_id=%(otelTraceID)s span_id=%(otelSpanID)s] %(message)s'
)

file_handler = RotatingFileHandler(
    f"{LOG_DIR}/app.log", maxBytes=10_000_000, backupCount=3
)
file_handler.setFormatter(log_formatter)

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(log_formatter)

logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler])
logger = logging.getLogger("notes-app")

# ── OpenTelemetry setup ───────────────────────────────────────────────────────
resource = Resource(attributes={"service.name": "notes-app", "service.version": "1.0.0"})
provider = TracerProvider(resource=resource)
otlp_exporter = OTLPSpanExporter(endpoint="http://otel-collector:4317", insecure=True)
provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("notes-app")
LoggingInstrumentor().instrument(set_logging_format=True)

# ── Prometheus metrics ────────────────────────────────────────────────────────
REQUEST_COUNT = Counter(
    "http_requests_total", "Total HTTP requests",
    ["method", "endpoint", "status_code"]
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP request latency",
    ["method", "endpoint"]
)
NOTES_TOTAL     = Counter("notes_total", "Total notes created")
NOTES_DELETED   = Counter("notes_deleted_total", "Total notes deleted")
ERRORS_404      = Counter("http_404_errors_total", "Total 404 Not Found errors")
ERRORS_5XX      = Counter("http_5xx_errors_total", "Total 5xx server errors")

# ── Middleware — records every request ────────────────────────────────────────
class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.time()
        response = await call_next(request)
        duration = time.time() - start
        endpoint = request.url.path
        status   = str(response.status_code)

        REQUEST_COUNT.labels(request.method, endpoint, status).inc()
        REQUEST_LATENCY.labels(request.method, endpoint).observe(duration)

        if response.status_code == 404:
            ERRORS_404.inc()
            logger.warning(f"404 Not Found: {request.method} {endpoint}")
        elif response.status_code >= 500:
            ERRORS_5XX.inc()
            logger.error(f"5xx Error {status}: {request.method} {endpoint}")

        return response

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Notes App")
app.add_middleware(MetricsMiddleware)
FastAPIInstrumentor.instrument_app(app)

# ── In-memory store ───────────────────────────────────────────────────────────
notes: dict[int, dict] = {}
next_id = 1

class NoteIn(BaseModel):
    title: str
    content: str

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    logger.info("Health check OK")
    return {"status": "ok"}

@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.post("/notes", status_code=201)
def create_note(note: NoteIn):
    global next_id
    with tracer.start_as_current_span("create_note") as span:
        span.set_attribute("note.title", note.title)
        note_id = next_id
        notes[note_id] = {"id": note_id, "title": note.title, "content": note.content}
        next_id += 1
        NOTES_TOTAL.inc()
        logger.info(f"Created note id={note_id} title='{note.title}'")
        return notes[note_id]

@app.get("/notes")
def list_notes():
    with tracer.start_as_current_span("list_notes") as span:
        span.set_attribute("notes.count", len(notes))
        logger.info(f"Listed {len(notes)} notes")
        return list(notes.values())

@app.get("/notes/{note_id}")
def get_note(note_id: int):
    with tracer.start_as_current_span("get_note") as span:
        span.set_attribute("note.id", note_id)
        if note_id not in notes:
            logger.warning(f"Note id={note_id} not found — 404")
            raise HTTPException(status_code=404, detail="Note not found")
        logger.info(f"Fetched note id={note_id}")
        return notes[note_id]

@app.delete("/notes/{note_id}", status_code=204)
def delete_note(note_id: int):
    with tracer.start_as_current_span("delete_note") as span:
        span.set_attribute("note.id", note_id)
        if note_id not in notes:
            logger.warning(f"Delete failed — note id={note_id} not found — 404")
            raise HTTPException(status_code=404, detail="Note not found")
        del notes[note_id]
        NOTES_DELETED.inc()
        logger.info(f"Deleted note id={note_id}")
        return None