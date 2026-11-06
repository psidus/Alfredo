"""
core/gpu_gate.py

Single-flight admission control for the local GPU.

Why a gate instead of a semaphore: on a 12 GB card only one LLM fits at a time.
Concurrent runs must *wait in line* (FIFO) rather than fail or hammer Ollama,
and the wait must be visible in the app ("waiting behind run #42"). The gate also
refuses a profile that cannot fit in the free VRAM, with an actionable message,
instead of letting Ollama spill to system RAM and stall the machine.

Profiles (two by design, override with env vars):

* ``fast`` — 7–8B Q4, low latency, default for chat/reviews.
* ``deep`` — 14B Q4, planner/reasoner; slower and closer to the VRAM limit.

Environment:
* ``ALFREDO_GPU_FAST_MODEL`` / ``ALFREDO_GPU_FAST_CTX`` / ``ALFREDO_GPU_FAST_VRAM_MB``
* ``ALFREDO_GPU_DEEP_MODEL`` / ``ALFREDO_GPU_DEEP_CTX`` / ``ALFREDO_GPU_DEEP_VRAM_MB``
* ``ALFREDO_GPU_RESERVE_MB``       VRAM kept free for the desktop (default 768)
* ``ALFREDO_GPU_QUEUE_TIMEOUT``    max seconds a call waits in line (default 120)
* ``ALFREDO_GPU_PRELOAD``          warm the model on profile switch (default 1)
* ``ALFREDO_GPU_KEEP_ALIVE``       Ollama ``keep_alive`` for preloaded models
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

VRAM_CACHE_SECONDS = 3.0
FIRST_TOKEN_SAMPLES = 200


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        logger.warning("Invalid %s=%r, using %s", name, raw, default)
        return default


def _env_str(name: str, default: str) -> str:
    raw = (os.getenv(name) or "").strip()
    return raw or default


@dataclass(frozen=True)
class GpuProfile:
    name: str
    model: str
    context_tokens: int = 8192
    estimated_vram_mb: int = 0
    description: str = ""

    def as_dict(self, fit: Optional[bool] = None) -> Dict[str, Any]:
        return {
            "name": self.name,
            "model": self.model,
            "context_tokens": self.context_tokens,
            "estimated_vram_mb": self.estimated_vram_mb,
            "description": self.description,
            "fits": fit,
        }


def default_profiles() -> Dict[str, GpuProfile]:
    fast = GpuProfile(
        name="fast",
        model=_env_str("ALFREDO_GPU_FAST_MODEL", "llama3:8b-instruct-q4_K_M"),
        context_tokens=_env_int("ALFREDO_GPU_FAST_CTX", 8192),
        estimated_vram_mb=_env_int("ALFREDO_GPU_FAST_VRAM_MB", 6500),
        description="low latency chat / reviews / routing",
    )
    deep = GpuProfile(
        name="deep",
        model=_env_str("ALFREDO_GPU_DEEP_MODEL", "deepseek-r1:14b"),
        context_tokens=_env_int("ALFREDO_GPU_DEEP_CTX", 8192),
        estimated_vram_mb=_env_int("ALFREDO_GPU_DEEP_VRAM_MB", 9800),
        description="planner / long reasoning, close to the VRAM limit",
    )
    return {"fast": fast, "deep": deep}


class GpuGateError(RuntimeError):
    """Base error for admission control."""


class GpuQueueTimeout(GpuGateError):
    """The call waited too long behind other GPU work."""


class GpuInsufficientVram(GpuGateError):
    """The requested profile cannot fit in the free VRAM."""


# Optional sink for "the model on the GPU changed" telemetry. The gate itself
# has no database dependency: the runner that knows how to persist events
# (``core.run_service``) installs the sink at run time.
EventSink = Callable[[int, str, Dict[str, Any]], None]
_event_sink: Optional[EventSink] = None


def set_event_sink(sink: Optional[EventSink]) -> None:
    """Install (or clear) the event sink used for model-switch telemetry."""
    global _event_sink
    _event_sink = sink


@dataclass
class GpuTicket:
    """A granted (or queued) slot. Release it when the model call is over."""

    profile: GpuProfile
    run_id: Optional[int] = None
    enqueued_at: float = 0.0
    started_at: Optional[float] = None
    started_wall: Optional[float] = None
    wait_seconds: float = 0.0
    released: bool = False


# ---------------------------------------------------------------------------
# VRAM probe
# ---------------------------------------------------------------------------


def probe_nvidia_smi(timeout: float = 2.0) -> Optional[List[Dict[str, Any]]]:
    """Read GPU memory from ``nvidia-smi``; ``None`` when no NVIDIA GPU is visible."""
    command = [
        "nvidia-smi",
        "--query-gpu=index,name,memory.total,memory.used,memory.free",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("nvidia-smi unavailable: %s", exc)
        return None
    if completed.returncode != 0:
        logger.debug("nvidia-smi failed: %s", (completed.stderr or "").strip()[:200])
        return None
    gpus: List[Dict[str, Any]] = []
    for line in (completed.stdout or "").splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 5:
            continue
        try:
            gpus.append(
                {
                    "index": int(parts[0]),
                    "name": parts[1],
                    "total_mb": int(float(parts[2])),
                    "used_mb": int(float(parts[3])),
                    "free_mb": int(float(parts[4])),
                }
            )
        except ValueError:
            continue
    return gpus or None


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class GpuGate:
    """FIFO, single-flight admission control with VRAM fit checks and metrics."""

    def __init__(
        self,
        profiles: Optional[Dict[str, GpuProfile]] = None,
        *,
        reserve_mb: Optional[int] = None,
        max_wait_seconds: Optional[float] = None,
        vram_probe: Optional[Callable[[], Optional[List[Dict[str, Any]]]]] = None,
        preloader: Optional[Callable[[GpuProfile], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.profiles: Dict[str, GpuProfile] = dict(profiles or default_profiles())
        self.reserve_mb = (
            _env_int("ALFREDO_GPU_RESERVE_MB", 768) if reserve_mb is None else int(reserve_mb)
        )
        default_wait = float(_env_int("ALFREDO_GPU_QUEUE_TIMEOUT", 120))
        self.max_wait_seconds = default_wait if max_wait_seconds is None else float(max_wait_seconds)
        self._probe = vram_probe or probe_nvidia_smi
        self._preloader = preloader if preloader is not None else default_preloader()
        self._clock = clock

        self._condition = threading.Condition()
        self._waiting: Deque[GpuTicket] = deque()
        self._current: Optional[GpuTicket] = None
        self._loaded_model: Optional[str] = None

        self._lock = threading.Lock()
        self._served = 0
        self._rejected = 0
        self._timeouts = 0
        self._evictions = 0
        self._waits: Deque[float] = deque(maxlen=FIRST_TOKEN_SAMPLES)
        self._first_token: Dict[str, Deque[float]] = {}
        self._durations: Dict[str, Deque[float]] = {}
        self._vram_cache: Optional[Dict[str, Any]] = None
        self._vram_cached_at = 0.0
        self._vram_error: Optional[str] = None
        self._last_preload_error: Optional[str] = None

    # -- profiles ---------------------------------------------------------

    def profile(self, name: Optional[str] = None) -> GpuProfile:
        key = (name or "").strip().lower() or "fast"
        profile = self.profiles.get(key)
        if profile is None:
            raise GpuGateError(
                f"Unknown GPU profile '{name}'. Available: {', '.join(sorted(self.profiles))}"
            )
        return profile

    def profile_for_model(self, model: str) -> Optional[GpuProfile]:
        """Map an Ollama model string (``ollama/name`` or ``name:tag``) to a profile."""
        raw = str(model or "").strip()
        if raw.lower().startswith("ollama/"):
            raw = raw.split("/", 1)[1]
        if not raw:
            return None
        lowered = raw.lower()
        for profile in self.profiles.values():
            if profile.model.lower() == lowered:
                return profile
        # Same family, different tag (e.g. "llama3:latest" vs "...q4_K_M").
        family = lowered.split(":", 1)[0]
        for profile in self.profiles.values():
            if profile.model.lower().split(":", 1)[0] == family:
                return profile
        return None

    def custom_profile(self, model: str, *, context_tokens: int = 8192) -> GpuProfile:
        """Profile for a local model that is not in the two configured slots."""
        return GpuProfile(
            name="custom",
            model=str(model or "").strip(),
            context_tokens=int(context_tokens),
            estimated_vram_mb=0,  # unknown: never reject
            description="unprofiled local model",
        )

    # -- VRAM -------------------------------------------------------------

    def _read_vram(self, *, force: bool = False) -> Optional[Dict[str, Any]]:
        now = self._clock()
        if (
            not force
            and self._vram_cache is not None
            and now - self._vram_cached_at < VRAM_CACHE_SECONDS
        ):
            return self._vram_cache
        try:
            gpus = self._probe()
        except Exception as exc:  # noqa: BLE001 - a probe must never break a run
            logger.debug("GPU probe failed: %s", exc)
            gpus = None
        if not gpus:
            self._vram_cache = None
            self._vram_cached_at = now
            self._vram_error = "No NVIDIA GPU detected (nvidia-smi unavailable)"
            return None
        best = max(gpus, key=lambda gpu: int(gpu.get("free_mb") or 0))
        self._vram_cache = {
            "available": True,
            "index": best.get("index"),
            "name": best.get("name", ""),
            "total_mb": best.get("total_mb"),
            "used_mb": best.get("used_mb"),
            "free_mb": best.get("free_mb"),
        }
        self._vram_cached_at = now
        self._vram_error = None
        return self._vram_cache

    def _fits(self, profile: GpuProfile, vram: Optional[Dict[str, Any]]) -> Optional[bool]:
        if vram is None or profile.estimated_vram_mb <= 0:
            return None  # cannot verify: do not block on unknown hardware
        free = int(vram.get("free_mb") or 0)
        return profile.estimated_vram_mb + self.reserve_mb <= free

    # -- admission --------------------------------------------------------

    def acquire(
        self,
        profile: Any = "fast",
        *,
        run_id: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> GpuTicket:
        """Wait for the GPU, then return a ticket. Always pair with :meth:`release`."""
        ticket = GpuTicket(
            profile=self._coerce_profile(profile),
            run_id=run_id,
            enqueued_at=self._clock(),
        )
        wait_for = self.max_wait_seconds if timeout is None else float(timeout)
        deadline = ticket.enqueued_at + max(0.0, wait_for)

        with self._condition:
            self._waiting.append(ticket)
            while self._current is not None or self._waiting[0] is not ticket:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    position = self._position(ticket)
                    self._remove_waiting(ticket)
                    with self._lock:
                        self._timeouts += 1
                    self._condition.notify_all()
                    raise GpuQueueTimeout(
                        f"GPU busy: '{ticket.profile.name}' is still waiting after "
                        f"{wait_for:.0f}s ({position} ahead). Retry later or use a "
                        f"cloud fallback."
                    )
                self._condition.wait(remaining)
            self._waiting.popleft()

            fit = self._fits(ticket.profile, self._read_vram())
            if fit is False:
                with self._lock:
                    self._rejected += 1
                self._condition.notify_all()
                vram = self._vram_cache or {}
                free = int(vram.get("free_mb") or 0)
                needed = ticket.profile.estimated_vram_mb + self.reserve_mb
                hint = self._fit_hint(ticket.profile, free)
                raise GpuInsufficientVram(
                    f"Profile '{ticket.profile.name}' ({ticket.profile.model}) needs "
                    f"~{needed} MB VRAM but only {free} MB are free "
                    f"(reserve {self.reserve_mb} MB). {hint}"
                )

            ticket.started_at = self._clock()
            ticket.started_wall = time.time()
            ticket.wait_seconds = max(0.0, ticket.started_at - ticket.enqueued_at)
            self._current = ticket
            with self._lock:
                self._waits.append(ticket.wait_seconds)

        switched = self._preload(ticket.profile)
        if switched:
            self._emit_switch(ticket)
        return ticket

    def _coerce_profile(self, profile: Any) -> GpuProfile:
        if isinstance(profile, GpuProfile):
            return profile
        return self.profile(profile)

    def _fit_hint(self, profile: GpuProfile, free_mb: int) -> str:
        alternatives = [
            other for other in self.profiles.values()
            if other.name != profile.name and other.estimated_vram_mb + self.reserve_mb <= free_mb
        ]
        names = ", ".join(other.name for other in alternatives) or "none"
        return (
            f"Use a smaller profile (fits: {names}) or free VRAM "
            f"(close other GPU apps, reduce context tokens)."
        )

    def release(self, ticket: Optional[GpuTicket]) -> None:
        """Free the slot. Safe to call twice or with ``None``."""
        if ticket is None:
            return
        with self._condition:
            if ticket.released:
                return
            ticket.released = True
            if self._current is ticket:
                self._current = None
                finished_at = self._clock()
                with self._lock:
                    self._served += 1
                    if ticket.started_at is not None:
                        self._durations.setdefault(
                            ticket.profile.name, deque(maxlen=50)
                        ).append(max(0.0, finished_at - ticket.started_at))
            self._condition.notify_all()

    def _remove_waiting(self, ticket: GpuTicket) -> None:
        try:
            self._waiting.remove(ticket)
        except ValueError:
            pass

    def _position(self, ticket: GpuTicket) -> int:
        try:
            return self._waiting.index(ticket) + 1
        except ValueError:
            return 0

    def _preload(self, profile: GpuProfile) -> bool:
        """Warm the model when the profile changes, and count the eviction.

        Returns ``True`` when the model actually changed (a real switch).
        """
        if self._preloader is None:
            return False
        if self._loaded_model == profile.model:
            return False
        if self._loaded_model:
            with self._lock:
                self._evictions += 1
        try:
            self._preloader(profile)
            self._last_preload_error = None
        except Exception as exc:  # noqa: BLE001 - preload is best-effort
            self._last_preload_error = str(exc)[:300]
            logger.warning("Could not preload %s: %s", profile.model, exc)
        self._loaded_model = profile.model
        return True

    def _emit_switch(self, ticket: GpuTicket) -> None:
        """Record the switch on the run's event log (best effort, never fatal)."""
        sink = _event_sink
        if sink is None or ticket.run_id is None:
            return
        # Only inside a real run: keeps tests and direct gate use side-effect free.
        try:
            from core import run_context

            if run_context.current_run_id() is None:
                return
        except Exception:  # noqa: BLE001
            return
        try:
            sink(
                int(ticket.run_id),
                "system.model.switch",
                {
                    "profile": ticket.profile.name,
                    "model": ticket.profile.model,
                    "estimated_vram_mb": ticket.profile.estimated_vram_mb,
                },
            )
        except Exception:  # noqa: BLE001 - telemetry must never break a run
            logger.debug("Could not emit the model switch event", exc_info=True)

    # -- metrics ----------------------------------------------------------

    def record_first_token(self, profile: Any, seconds: Optional[float]) -> None:
        """Record time-to-first-token for a profile (``GpuProfile`` or name)."""
        if seconds is None:
            return
        name = profile.name if isinstance(profile, GpuProfile) else str(profile)
        value = max(0.0, float(seconds))
        with self._lock:
            self._first_token.setdefault(name, deque(maxlen=FIRST_TOKEN_SAMPLES)).append(value)

    @staticmethod
    def _percentile(values: List[float], fraction: float) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
        return round(ordered[index], 3)

    def _avg_duration(self, profile_name: str) -> float:
        """Average slot duration for a profile (fallback for the very first runs)."""
        with self._lock:
            values = list(self._durations.get(profile_name, ()))
        if not values:
            return 30.0
        return sum(values) / len(values)

    def _first_token_stats(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            samples = {name: list(values) for name, values in self._first_token.items()}
        stats: Dict[str, Dict[str, Any]] = {}
        for name, values in samples.items():
            stats[name] = {
                "count": len(values),
                "avg_s": round(sum(values) / len(values), 3) if values else None,
                "p95_s": self._percentile(values, 0.95),
                "last_s": round(values[-1], 3) if values else None,
            }
        return stats

    # -- status -----------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """JSON-ready snapshot for ``GET /api/system/status`` and the app."""
        vram = self._read_vram() or {}
        with self._condition:
            current = self._current
            waiting = list(self._waiting)
        now = self._clock()
        with self._lock:
            waits = list(self._waits)
            metrics = {
                "served": self._served,
                "rejected": self._rejected,
                "timeouts": self._timeouts,
                "evictions": self._evictions,
                "avg_wait_s": round(sum(waits) / len(waits), 3) if waits else None,
            }

        profiles: Dict[str, Any] = {}
        for name, profile in self.profiles.items():
            fits = self._fits(profile, vram or None)
            profiles[name] = {
                **profile.as_dict(fit=fits),
                "headroom_mb": (
                    int(vram["free_mb"]) - profile.estimated_vram_mb - self.reserve_mb
                    if vram.get("free_mb") is not None
                    else None
                ),
                "avg_call_s": round(self._avg_duration(name), 1),
            }

        remaining_current = 0.0
        if current is not None and current.started_at is not None:
            remaining_current = max(
                0.0, self._avg_duration(current.profile.name) - (now - current.started_at)
            )
        eta = remaining_current
        waiting_payload: List[Dict[str, Any]] = []
        for position, ticket in enumerate(waiting, start=1):
            waiting_payload.append(
                {
                    "position": position,
                    "profile": ticket.profile.name,
                    "model": ticket.profile.model,
                    "run_id": ticket.run_id,
                    "waiting_s": round(now - ticket.enqueued_at, 3),
                    "eta_s": round(eta, 1),
                }
            )
            eta += self._avg_duration(ticket.profile.name)

        queue = {
            "current": (
                {
                    "profile": current.profile.name,
                    "model": current.profile.model,
                    "run_id": current.run_id,
                    "started_at": _format_ts(current.started_wall),
                    "elapsed_s": round(now - current.started_at, 3) if current.started_at else 0.0,
                }
                if current is not None
                else None
            ),
            "waiting": waiting_payload,
            "length": len(waiting),
        }

        return {
            "gpu": {
                "available": bool(vram.get("available")),
                "index": vram.get("index"),
                "name": vram.get("name", ""),
                "total_mb": vram.get("total_mb"),
                "used_mb": vram.get("used_mb"),
                "free_mb": vram.get("free_mb"),
                "reserve_mb": self.reserve_mb,
                "error": self._vram_error,
            },
            "profiles": profiles,
            "queue": queue,
            "metrics": {**metrics, "first_token": self._first_token_stats()},
            "loaded_model": self._loaded_model,
            "preload_error": self._last_preload_error,
        }


def _format_ts(epoch: Optional[float]) -> Optional[str]:
    if not epoch:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


# ---------------------------------------------------------------------------
# Ollama preload (best effort, never fatal)
# ---------------------------------------------------------------------------


def default_preloader() -> Optional[Callable[[GpuProfile], None]]:
    """Warm a profile in Ollama when ``ALFREDO_GPU_PRELOAD`` is not disabled."""
    if (os.getenv("ALFREDO_GPU_PRELOAD", "1").strip().lower() in ("0", "false", "no", "off")):
        return None
    keep_alive = _env_str("ALFREDO_GPU_KEEP_ALIVE", "10m")

    def _preload(profile: GpuProfile) -> None:
        import requests

        base = _env_str("OLLAMA_API_BASE", "http://127.0.0.1:11434").rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        response = requests.post(
            f"{base}/api/generate",
            json={"model": profile.model, "prompt": "", "keep_alive": keep_alive},
            timeout=(2, 30),
        )
        response.raise_for_status()

    return _preload


# Process-wide gate used by MasterAI and the system status endpoint.
GPU_GATE = GpuGate()


def system_status() -> Dict[str, Any]:
    """Snapshot of the process-wide gate (used by the API)."""
    return GPU_GATE.status()


__all__ = [
    "GpuGate",
    "GpuGateError",
    "GpuInsufficientVram",
    "GpuProfile",
    "GpuQueueTimeout",
    "GpuTicket",
    "GPU_GATE",
    "default_preloader",
    "default_profiles",
    "probe_nvidia_smi",
    "system_status",
]
