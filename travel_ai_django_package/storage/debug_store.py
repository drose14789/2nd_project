"""Simple in-memory debug event store (ring buffer).
- Stores recent requests + errors so UI can show what is failing.
- Does NOT store secrets (API key values).
"""
from __future__ import annotations
from collections import deque
from typing import Deque, Dict, Any, Optional, List
import time
import traceback

MAX_EVENTS = 200
_events: Deque[Dict[str, Any]] = deque(maxlen=MAX_EVENTS)

def add_event(kind: str, payload: Dict[str, Any]) -> None:
    evt = {"ts": int(time.time()*1000), "kind": kind, **payload}
    _events.append(evt)

def add_error(where: str, err: Exception, extra: Optional[Dict[str, Any]] = None) -> None:
    payload: Dict[str, Any] = {
        "where": where,
        "error_type": type(err).__name__,
        "error": str(err),
        "trace": traceback.format_exc(limit=8),
    }
    if extra:
        payload.update(extra)
    add_event("error", payload)

def list_events(limit: int = 80) -> List[Dict[str, Any]]:
    items = list(_events)
    if limit and len(items) > limit:
        items = items[-limit:]
    return items
