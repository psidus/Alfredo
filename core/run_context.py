"""Tiny helpers to know which run owns the current thread of execution.

The GPU gate shows "waiting behind run #42" in the app, so the execution engine
must be able to tell the gate which run is calling the model. ``contextvars``
propagate to the thread pool used by ``run_service``/CrewAI in the common case;
when they don't, the gate simply reports a call without a run id.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional

_current_run_id: ContextVar[Optional[int]] = ContextVar("alfredo_current_run_id", default=None)


def current_run_id() -> Optional[int]:
    """The run id bound to this execution context, if any."""
    return _current_run_id.get()


def set_run_id(run_id: Optional[int]) -> Any:
    """Bind ``run_id`` and return the context token (use with :func:`reset_run_id`)."""
    return _current_run_id.set(int(run_id) if run_id is not None else None)


def reset_run_id(token: Any) -> None:
    _current_run_id.reset(token)


@contextmanager
def run_context(run_id: Optional[int]) -> Iterator[None]:
    """Run a block with ``run_id`` visible to :func:`current_run_id`."""
    token = set_run_id(run_id)
    try:
        yield
    finally:
        reset_run_id(token)


__all__ = ["current_run_id", "reset_run_id", "run_context", "set_run_id"]
