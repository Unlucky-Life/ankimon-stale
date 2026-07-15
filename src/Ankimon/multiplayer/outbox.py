"""Persistent outbox for multiplayer review events.

Review hooks push events here (main thread, in-memory append + cheap disk
write). A background flusher drains events in batches; events are only
removed after the server acknowledges the batch, and each event carries a
stable UUID so server-side deduplication makes retries safe.
"""

import json
import threading
import uuid
from datetime import datetime, timezone

from ..resources import user_path

OUTBOX_PATH = user_path / "multiplayer_outbox.json"
MAX_QUEUED_EVENTS = 2000
MAX_BATCH_SIZE = 50


class Outbox:
    def __init__(self):
        self._lock = threading.Lock()
        self._events = self._load()

    def _load(self) -> list:
        try:
            with open(OUTBOX_PATH, "r", encoding="utf-8") as f:
                events = json.load(f)
            if isinstance(events, list):
                return events
        except (OSError, json.JSONDecodeError):
            pass
        return []

    def _save(self):
        try:
            with open(OUTBOX_PATH, "w", encoding="utf-8") as f:
                json.dump(self._events, f)
        except OSError:
            # Disk persistence is best-effort; the in-memory queue still works.
            pass

    def push(self, event_type: str, payload: dict):
        event = {
            "id": str(uuid.uuid4()),
            "type": event_type,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        event.update(payload)
        with self._lock:
            self._events.append(event)
            # Drop the oldest events rather than growing without bound.
            if len(self._events) > MAX_QUEUED_EVENTS:
                self._events = self._events[-MAX_QUEUED_EVENTS:]
            self._save()

    def pending_count(self) -> int:
        with self._lock:
            return len(self._events)

    def peek_batch(self) -> list:
        """Snapshot the next batch without removing it."""
        with self._lock:
            return list(self._events[:MAX_BATCH_SIZE])

    def ack(self, events: list):
        """Remove acknowledged events (matched by id) after a server 2xx."""
        acked_ids = {event["id"] for event in events}
        with self._lock:
            self._events = [e for e in self._events if e["id"] not in acked_ids]
            self._save()
