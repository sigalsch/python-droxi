"""Lightweight observability primitives for the clinic ingestion pipeline.

PHI RULE (non-negotiable)
-------------------------
Healthcare data. Every log line -- INFO, WARNING, DEBUG -- MUST log
identifiers only (``task_id``, ``request_id``, ``patient_id``,
``department``) and counts. NEVER log ``message`` bodies, ``medications``,
patient names, or free-form content. Rule of thumb: "is this an id or a
count?" If no, don't log it.

Building blocks:

- :data:`_correlation_id` + :func:`correlation_scope`: one id per batch,
  stitched into every log line via the ContextVar + filter below.
- :class:`UpdateStats`: counters a service returns so the manager can
  emit one summary log per batch instead of each service re-logging.
- :func:`format_kv`: ``key=value`` formatter for greppable structured
  fields; no log-aggregator setup required.
- :func:`configure_logging`: idempotent root-logger setup. Call from
  ``main.py`` / entry points only; library code never calls it.
"""

from __future__ import annotations

import logging
import sys
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Any, Iterator


_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")


def new_correlation_id() -> str:
    """Short log-friendly id (12 hex chars -- enough within a rolling log
    window, short in a log line)."""
    return uuid.uuid4().hex[:12]


def current_correlation_id() -> str:
    """Current context's correlation id (``"-"`` if none)."""
    return _correlation_id.get()


@contextmanager
def correlation_scope(cid: str | None = None) -> Iterator[str]:
    """Bind a correlation id for the ``with`` block; restored on exit so
    nested scopes and concurrent tasks stay isolated. Generates one if
    ``cid`` is omitted.
    """
    token = _correlation_id.set(cid or new_correlation_id())
    try:
        yield _correlation_id.get()
    finally:
        _correlation_id.reset(token)


def format_kv(**fields: Any) -> str:
    """``key=value`` formatter for log messages. Only quotes values with
    whitespace, so ids and counts stay unquoted and greppable:

    >>> format_kv(batch_size=12, patient="patient1")
    'batch_size=12 patient=patient1'
    """
    parts = []
    for key, value in fields.items():
        text = str(value)
        if any(ch.isspace() for ch in text):
            text = repr(text)
        parts.append(f"{key}={text}")
    return " ".join(parts)


@dataclass
class UpdateStats:
    """Per-batch counters returned by ``update_requests``; the manager
    emits them as one summary log at the end of each batch.

    - ``requests_created``: new ``PatientRequest`` rows inserted.
    - ``requests_updated``: existing Open requests rewritten in place.
    - ``requests_closed_by_leftover_sweep``: Open requests closed because
      the batch left their ``(patient, dept)`` key with no Open tasks.
    - ``leftover_close_skipped_live_key``: times the partial-batch guard
      in :class:`DepartmentPatientRequestService` refused to close a
      leftover because a live Open task still exists. Must stay zero
      under the current ``ClinicManager`` contract; nonzero = upstream
      contract violation.
    """

    requests_created: int = 0
    requests_updated: int = 0
    requests_closed_by_leftover_sweep: int = 0
    leftover_close_skipped_live_key: int = 0

    def as_log_fields(self) -> dict[str, int]:
        return asdict(self)


def configure_logging(level: int = logging.INFO) -> None:
    """Configure root logging for an app entry point. Idempotent.

    Format: ``timestamp level [correlation_id] logger: message``.
    Library code (services, manager) must NOT call this -- it's for
    ``main.py`` and similar bootstraps only.
    """
    root = logging.getLogger()
    if getattr(root, "_clinic_observability_configured", False):
        return

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(correlation_id)s] %(name)s: %(message)s"
        )
    )
    handler.addFilter(_CorrelationIdFilter())
    root.addHandler(handler)
    root.setLevel(level)
    root._clinic_observability_configured = True  # type: ignore[attr-defined]


class _CorrelationIdFilter(logging.Filter):
    """Inject the current correlation id into every :class:`LogRecord` so
    the formatter can reference ``%(correlation_id)s`` unconditionally.
    Attached by :func:`configure_logging`; services never pass the id.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = _correlation_id.get()
        return True
