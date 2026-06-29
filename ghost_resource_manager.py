"""
GhostNodes Resource Manager — GPU detection, VRAM budgeting, model lifecycle,
and model load balancing.

Manages GPU/CPU/MLX device selection, tracks loaded models with smart eviction,
serializes concurrent model loads to prevent OOM, tracks per-model usage
statistics, and enforces configurable memory budgets so multiple AI nodes
can coexist.

Supports: NVIDIA CUDA, Apple Silicon (MPS + MLX), CPU fallback.
"""

import gc
import json as _json
import logging
import platform
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("quinely.resource_manager")

GHOST_HOME = Path.home() / ".ghost"
MODELS_CACHE_DIR = GHOST_HOME / "models"
MODELS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_STATS_FILE = GHOST_HOME / "model_stats.json"

_LOAD_GATE_TIMEOUT_S = 300  # 5-minute safety timeout for load gate
_STATS_PERSIST_INTERVAL_S = 60  # persist stats to disk every 60s


@dataclass
class LoadedModel:
    model_id: str
    device: str
    estimated_vram_gb: float
    last_used: float = field(default_factory=time.time)
    model_obj: Any = None
    metadata: dict = field(default_factory=dict)


@dataclass
class ModelStats:
    """Per-model lifetime statistics that persist across load/evict cycles."""
    model_id: str
    load_count: int = 0
    use_count: int = 0
    eviction_count: int = 0
    error_count: int = 0
    total_load_time_s: float = 0.0
    first_seen: float = 0.0
    last_used: float = 0.0
    estimated_vram_gb: float = 0.0

    @property
    def avg_load_time_s(self) -> float:
        return self.total_load_time_s / max(1, self.load_count)

    @property
    def use_frequency_per_hour(self) -> float:
        if self.first_seen <= 0:
            return 0.0
        hours = max(0.01, (time.time() - self.first_seen) / 3600)
        return self.use_count / hours

    def to_dict(self) -> dict:
        d = asdict(self)
        d["avg_load_time_s"] = round(self.avg_load_time_s, 2)
        d["use_frequency_per_hour"] = round(self.use_frequency_per_hour, 2)
        return d


@dataclass
class QueueEntry:
    """Tracks a pending acquire request for visibility."""
    model_id: str
    estimated_vram_gb: float
    node_id: str
    enqueued_at: float = field(default_factory=time.time)


class DeviceInfo:
    """Detect available compute devices and their capabilities."""

    def __init__(self):
        self.has_cuda = False
        self.has_mps = False
        self.has_mlx = False
        self.mlx_version = ""
        self.cuda_device_name = ""
        self.cuda_vram_total_gb = 0.0
        self.cuda_vram_free_gb = 0.0
        self.apple_silicon = False
        self.unified_memory_gb = 0.0
        self._detect()

    def _detect(self):
        if platform.system() == "Darwin" and platform.machine() in ("arm64", "aarch64"):
            self.apple_silicon = True

        self._detect_unified_memory()
        self._detect_mlx()
        self._detect_torch()

    def _detect_torch(self):
        try:
            import torch
            if torch.cuda.is_available():
                self.has_cuda = True
                self.cuda_device_name = torch.cuda.get_device_name(0)
                props = torch.cuda.get_device_properties(0)
                self.cuda_vram_total_gb = round(props.total_mem / (1024 ** 3), 2)
                try:
                    free, _ = torch.cuda.mem_get_info(0)
                    self.cuda_vram_free_gb = round(free / (1024 ** 3), 2)
                except (AttributeError, RuntimeError):
                    self.cuda_vram_free_gb = self.cuda_vram_total_gb * 0.8
            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                self.has_mps = True
        except ImportError:
            pass
        except Exception as e:
            log.warning("Torch device detection error: %s", e)

    def _detect_mlx(self):
        if not self.apple_silicon:
            return
        try:
            import mlx.core as mx
            self.has_mlx = True
            self.mlx_version = getattr(mx, "__version__", "unknown")
            log.info("MLX detected: v%s", self.mlx_version)
        except ImportError:
            pass
        except Exception as e:
            log.debug("MLX detection error: %s", e)

    def _detect_unified_memory(self):
        try:
            import subprocess
            sys_name = platform.system()
            if sys_name == "Darwin":
                result = subprocess.run(
                    ["sysctl", "-n", "hw.memsize"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    self.unified_memory_gb = round(int(result.stdout.strip()) / (1024 ** 3), 1)
            elif sys_name == "Linux":
                meminfo = Path("/proc/meminfo")
                if meminfo.exists():
                    for line in meminfo.read_text().splitlines():
                        if line.startswith("MemTotal:"):
                            kb = int(line.split()[1])
                            self.unified_memory_gb = round(kb / (1024 ** 2), 1)
                            break
            elif sys_name == "Windows":
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip().isdigit():
                    self.unified_memory_gb = round(int(result.stdout.strip()) / (1024 ** 3), 1)
        except Exception:
            pass

    @property
    def best_device(self) -> str:
        if self.has_cuda:
            return "cuda"
        if self.has_mlx:
            return "mlx"
        if self.has_mps:
            return "mps"
        return "cpu"

    @property
    def preferred_framework(self) -> str:
        if self.has_cuda:
            return "torch"
        if self.has_mlx:
            return "mlx"
        if self.has_mps:
            return "torch"
        return "cpu"

    def refresh_vram(self):
        if not self.has_cuda:
            return
        try:
            import torch
            free, total = torch.cuda.mem_get_info(0)
            self.cuda_vram_free_gb = round(free / (1024 ** 3), 2)
            self.cuda_vram_total_gb = round(total / (1024 ** 3), 2)
        except (ImportError, AttributeError, RuntimeError):
            pass

    def to_dict(self) -> dict:
        d = {
            "best_device": self.best_device,
            "preferred_framework": self.preferred_framework,
            "has_cuda": self.has_cuda,
            "has_mps": self.has_mps,
            "has_mlx": self.has_mlx,
            "apple_silicon": self.apple_silicon,
            "cuda_device_name": self.cuda_device_name,
            "cuda_vram_total_gb": self.cuda_vram_total_gb,
            "cuda_vram_free_gb": self.cuda_vram_free_gb,
        }
        if self.has_mlx:
            d["mlx_version"] = self.mlx_version
        if self.unified_memory_gb > 0:
            d["unified_memory_gb"] = self.unified_memory_gb
        return d


class ResourceManager:
    """Manage GPU memory, model loading/unloading, device selection, and load balancing.

    Load balancer features:
    - Serialized model loading via a semaphore (prevents OOM from concurrent loads)
    - Smart eviction scoring: frequency * recency / (load_cost * vram_weight)
    - Per-model usage statistics that persist across restarts
    - Queue visibility for pending load requests
    - Preload hints for warming up frequently-used models
    """

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}
        self.device_info = DeviceInfo()
        self._models: dict[str, LoadedModel] = {}
        self._lock = threading.RLock()

        # VRAM budget
        self._budget_gb = self.cfg.get("gpu_memory_budget_gb", 0)
        if self._budget_gb <= 0:
            if self.device_info.has_cuda:
                self._budget_gb = self.device_info.cuda_vram_total_gb * 0.85
            elif self.device_info.apple_silicon and self.device_info.unified_memory_gb > 0:
                self._budget_gb = self.device_info.unified_memory_gb * 0.6

        # --- Load balancer state ---
        self._load_gate = threading.Semaphore(1)
        self._gate_holder: Optional[str] = None
        self._gate_acquired_at: float = 0.0
        self._queue_waiters: list[QueueEntry] = []
        self._queue_lock = threading.Lock()

        # Per-model lifetime stats
        self._stats: dict[str, ModelStats] = {}
        self._stats_lock = threading.Lock()
        self._load_stats()

        # Metrics counters
        self._total_acquires = 0
        self._total_evictions = 0
        self._total_cache_hits = 0
        self._total_gate_waits = 0
        self._started_at = time.time()

        # Watchdog thread for auto-releasing stuck load gates
        self._watchdog_stop = threading.Event()
        self._watchdog = threading.Thread(
            target=self._gate_watchdog, daemon=True, name="lb-watchdog",
        )
        self._watchdog.start()

        # Periodic stats persistence
        self._persist_stop = threading.Event()
        self._persist_thread = threading.Thread(
            target=self._stats_persist_loop, daemon=True, name="lb-stats-persist",
        )
        self._persist_thread.start()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def budget_gb(self) -> float:
        return self._budget_gb

    @property
    def used_gb(self) -> float:
        with self._lock:
            return sum(m.estimated_vram_gb for m in self._models.values())

    @property
    def available_gb(self) -> float:
        return max(0, self._budget_gb - self.used_gb)

    # ------------------------------------------------------------------
    # Core API (backward-compatible)
    # ------------------------------------------------------------------

    def acquire(self, model_id: str, estimated_vram_gb: float = 0,
                model_obj: Any = None, metadata: dict | None = None) -> str:
        """Register a model as loaded. Returns the assigned device string.

        Load-balanced: If the model is already loaded, returns immediately
        (fast path). Otherwise, acquires the load gate to serialize concurrent
        model loads. The gate is held until notify_load_complete() is called
        or a 5-minute watchdog timeout releases it.
        """
        self._total_acquires += 1
        node_id = (metadata or {}).get("node_id", "unknown")

        # FAST PATH: model already loaded
        with self._lock:
            if model_id in self._models:
                entry = self._models[model_id]
                entry.last_used = time.time()
                self._record_use(model_id)
                self._total_cache_hits += 1
                return entry.device

        # SLOW PATH: new model needs loading — acquire the gate
        waiter = QueueEntry(
            model_id=model_id,
            estimated_vram_gb=estimated_vram_gb,
            node_id=node_id,
        )
        with self._queue_lock:
            self._queue_waiters.append(waiter)

        gate_wait_start = time.time()
        acquired = self._load_gate.acquire(timeout=_LOAD_GATE_TIMEOUT_S)
        gate_wait_s = time.time() - gate_wait_start

        with self._queue_lock:
            if waiter in self._queue_waiters:
                self._queue_waiters.remove(waiter)

        if not acquired:
            log.warning("Load gate timeout for %s after %.0fs — forcing acquire",
                        model_id, gate_wait_s)
            self._total_gate_waits += 1

        if gate_wait_s > 0.1:
            self._total_gate_waits += 1
            log.info("Model %s waited %.1fs for load gate", model_id, gate_wait_s)

        try:
            # Re-check after acquiring gate (may have been loaded while waiting)
            with self._lock:
                if model_id in self._models:
                    entry = self._models[model_id]
                    entry.last_used = time.time()
                    self._record_use(model_id)
                    self._total_cache_hits += 1
                    return entry.device

            # Allocate device with smart eviction
            device = (self._smart_device_for(model_id, estimated_vram_gb)
                      if estimated_vram_gb > 0 else self.device_info.best_device)

            entry = LoadedModel(
                model_id=model_id,
                device=device,
                estimated_vram_gb=estimated_vram_gb if device != "cpu" else 0,
                model_obj=model_obj,
                metadata=metadata or {},
            )
            with self._lock:
                self._models[model_id] = entry

            self._gate_holder = model_id
            self._gate_acquired_at = time.time()

            self._record_load_start(model_id, estimated_vram_gb)

            log.info("Model acquired: %s on %s (%.1f GB) — gate held",
                     model_id, device, estimated_vram_gb)
            return device

        except Exception:
            # Release gate on error so other models can proceed
            self._gate_holder = None
            if acquired:
                self._load_gate.release()
            raise

    def notify_load_complete(self, model_id: str):
        """Called after a model finishes loading. Releases the load gate
        so the next queued model can begin loading.

        Also records the load time for smart eviction scoring.
        """
        if self._gate_holder == model_id:
            elapsed = time.time() - self._gate_acquired_at
            self._record_load_end(model_id, elapsed)
            self._gate_holder = None
            self._load_gate.release()
            log.info("Load complete: %s (%.1fs) — gate released", model_id, elapsed)
        elif self._gate_holder is None:
            self._record_load_end(model_id, 0)

    def release(self, model_id: str) -> bool:
        """Unload a model and free its tracked resources."""
        with self._lock:
            entry = self._models.pop(model_id, None)
        if not entry:
            return False

        self._record_eviction(model_id)

        if entry.model_obj is not None:
            try:
                del entry.model_obj
            except Exception:
                pass

        self._try_gc(entry.device)
        self._total_evictions += 1
        log.info("Model released: %s (freed ~%.1f GB)", model_id, entry.estimated_vram_gb)

        # If this model held the gate (crashed mid-load), release it
        if self._gate_holder == model_id:
            self._gate_holder = None
            self._load_gate.release()

        return True

    def touch(self, model_id: str):
        """Update last-used timestamp for a loaded model."""
        with self._lock:
            if model_id in self._models:
                self._models[model_id].last_used = time.time()
        self._record_use(model_id)

    def get_model(self, model_id: str) -> Optional[LoadedModel]:
        with self._lock:
            entry = self._models.get(model_id)
            if entry:
                entry.last_used = time.time()
            return entry

    def list_loaded(self) -> list[str]:
        with self._lock:
            return list(self._models.keys())

    # ------------------------------------------------------------------
    # Smart eviction (replaces pure LRU)
    # ------------------------------------------------------------------

    def _eviction_score(self, model_id: str) -> float:
        """Higher score = more valuable = evict LAST.

        score = (use_frequency * recency_weight) / (load_cost * vram_weight)
        """
        with self._lock:
            entry = self._models.get(model_id)
            if not entry:
                return 0.0
            vram = max(0.1, entry.estimated_vram_gb)
            last_used = entry.last_used

        with self._stats_lock:
            st = self._stats.get(model_id)

        if not st or st.use_count == 0:
            return 0.0

        use_freq = st.use_frequency_per_hour
        minutes_idle = max(0.01, (time.time() - last_used) / 60)
        recency = 1.0 / minutes_idle
        load_cost = max(1.0, st.avg_load_time_s)

        return (use_freq * recency) / (load_cost * vram)

    def _smart_device_for(self, model_id: str, estimated_vram_gb: float) -> str:
        """Pick best device, using smart eviction if budget is exceeded."""
        device = self.device_info.best_device

        if estimated_vram_gb > self.available_gb and self._budget_gb > 0:
            shortfall = estimated_vram_gb - self.available_gb
            self._offload_smart(shortfall, exclude=model_id)

        if self.device_info.has_cuda:
            return "cuda"
        if self.device_info.has_mlx:
            return "mlx"
        if self.device_info.has_mps:
            return "mps"
        return "cpu"

    def _offload_smart(self, needed_gb: float, exclude: str = "") -> float:
        """Evict models by lowest eviction score until needed_gb is freed."""
        freed = 0.0
        while freed < needed_gb:
            with self._lock:
                candidates = [
                    mid for mid in self._models
                    if mid != exclude and mid != self._gate_holder
                ]
                if not candidates:
                    break
                victim = min(candidates, key=lambda mid: self._eviction_score(mid))
                victim_vram = self._models[victim].estimated_vram_gb

            self.release(victim)
            freed += victim_vram
            log.info("Smart-evicted: %s (score %.4f, freed %.1f GB, total freed %.1f GB)",
                     victim, self._eviction_score(victim), victim_vram, freed)
        return freed

    # Keep the old name as an alias for backward compatibility
    def best_device_for(self, estimated_vram_gb: float) -> str:
        return self._smart_device_for("", estimated_vram_gb)

    # ------------------------------------------------------------------
    # Usage statistics
    # ------------------------------------------------------------------

    def _ensure_stats(self, model_id: str, vram_gb: float = 0) -> ModelStats:
        with self._stats_lock:
            if model_id not in self._stats:
                self._stats[model_id] = ModelStats(
                    model_id=model_id,
                    first_seen=time.time(),
                    estimated_vram_gb=vram_gb,
                )
            return self._stats[model_id]

    def _record_use(self, model_id: str):
        st = self._ensure_stats(model_id)
        with self._stats_lock:
            st.use_count += 1
            st.last_used = time.time()

    def _record_load_start(self, model_id: str, vram_gb: float):
        st = self._ensure_stats(model_id, vram_gb)
        with self._stats_lock:
            st.load_count += 1
            if vram_gb > 0:
                st.estimated_vram_gb = vram_gb

    def _record_load_end(self, model_id: str, elapsed_s: float):
        st = self._ensure_stats(model_id)
        with self._stats_lock:
            st.total_load_time_s += elapsed_s

    def _record_eviction(self, model_id: str):
        st = self._ensure_stats(model_id)
        with self._stats_lock:
            st.eviction_count += 1

    def record_error(self, model_id: str):
        """Record a model load failure for stats."""
        st = self._ensure_stats(model_id)
        with self._stats_lock:
            st.error_count += 1

    def get_model_stats(self, model_id: str) -> Optional[dict]:
        with self._stats_lock:
            st = self._stats.get(model_id)
            return st.to_dict() if st else None

    def get_all_stats(self) -> list[dict]:
        with self._stats_lock:
            return [st.to_dict() for st in sorted(
                self._stats.values(),
                key=lambda s: s.use_count,
                reverse=True,
            )]

    # ------------------------------------------------------------------
    # Stats persistence
    # ------------------------------------------------------------------

    def _load_stats(self):
        if not _STATS_FILE.exists():
            return
        try:
            raw = _json.loads(_STATS_FILE.read_text(encoding="utf-8"))
            for entry in raw:
                mid = entry.get("model_id", "")
                if not mid:
                    continue
                self._stats[mid] = ModelStats(
                    model_id=mid,
                    load_count=entry.get("load_count", 0),
                    use_count=entry.get("use_count", 0),
                    eviction_count=entry.get("eviction_count", 0),
                    error_count=entry.get("error_count", 0),
                    total_load_time_s=entry.get("total_load_time_s", 0.0),
                    first_seen=entry.get("first_seen", 0.0),
                    last_used=entry.get("last_used", 0.0),
                    estimated_vram_gb=entry.get("estimated_vram_gb", 0.0),
                )
            log.info("Loaded %d model stats from disk", len(self._stats))
        except Exception as e:
            log.warning("Failed to load model stats: %s", e)

    def _persist_stats(self):
        try:
            with self._stats_lock:
                data = [st.to_dict() for st in self._stats.values()]
            _STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
            _STATS_FILE.write_text(_json.dumps(data, indent=2, default=str), encoding="utf-8")
        except Exception as e:
            log.debug("Failed to persist model stats: %s", e)

    def _stats_persist_loop(self):
        while not self._persist_stop.wait(_STATS_PERSIST_INTERVAL_S):
            self._persist_stats()

    # ------------------------------------------------------------------
    # Load gate watchdog
    # ------------------------------------------------------------------

    def _gate_watchdog(self):
        """Auto-release the load gate if held longer than timeout."""
        while not self._watchdog_stop.wait(10):
            holder = self._gate_holder
            if holder and self._gate_acquired_at > 0:
                held_s = time.time() - self._gate_acquired_at
                if held_s > _LOAD_GATE_TIMEOUT_S:
                    log.warning(
                        "Load gate held by %s for %.0fs — auto-releasing (timeout %ds)",
                        holder, held_s, _LOAD_GATE_TIMEOUT_S,
                    )
                    self._gate_holder = None
                    try:
                        self._load_gate.release()
                    except ValueError:
                        pass

    # ------------------------------------------------------------------
    # Preload hints
    # ------------------------------------------------------------------

    def preload_hint(self, model_id: str, estimated_vram_gb: float = 0):
        """Suggest preloading a model. If there's enough VRAM budget and
        the model is frequently used, this is a no-op hint that nodes can
        use to warm up models before they're needed.

        Returns True if the model is already loaded or has room to load.
        """
        with self._lock:
            if model_id in self._models:
                return True
        return estimated_vram_gb <= self.available_gb

    # ------------------------------------------------------------------
    # GC helpers
    # ------------------------------------------------------------------

    def _try_gc(self, device: str):
        try:
            gc.collect()
            if device == "cuda":
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            elif device == "mlx":
                try:
                    import mlx.core as mx
                    mx.metal.clear_cache()
                except (ImportError, AttributeError):
                    pass
        except ImportError:
            pass
        except Exception as e:
            log.debug("GC error: %s", e)

    # ------------------------------------------------------------------
    # Status and metrics
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Full resource status for dashboard / tools (backward-compatible)."""
        self.device_info.refresh_vram()
        with self._lock:
            models = [
                {
                    "model_id": m.model_id,
                    "device": m.device,
                    "vram_gb": m.estimated_vram_gb,
                    "last_used": m.last_used,
                    "metadata": m.metadata,
                }
                for m in self._models.values()
            ]
        with self._queue_lock:
            queue_depth = len(self._queue_waiters)
        return {
            "device": self.device_info.to_dict(),
            "budget_gb": self._budget_gb,
            "used_gb": self.used_gb,
            "available_gb": self.available_gb,
            "loaded_models": models,
            "models_cache_dir": str(MODELS_CACHE_DIR),
            "load_balancer": {
                "currently_loading": self._gate_holder,
                "queue_depth": queue_depth,
                "total_acquires": self._total_acquires,
                "cache_hits": self._total_cache_hits,
                "cache_hit_rate": (
                    round(self._total_cache_hits / max(1, self._total_acquires), 3)
                ),
                "total_evictions": self._total_evictions,
                "total_gate_waits": self._total_gate_waits,
            },
        }

    def get_metrics(self) -> dict:
        """Comprehensive load balancer metrics for dashboard."""
        self.device_info.refresh_vram()

        with self._lock:
            loaded = {
                mid: {
                    "device": m.device,
                    "vram_gb": m.estimated_vram_gb,
                    "last_used_ago_s": round(time.time() - m.last_used, 1),
                    "eviction_score": round(self._eviction_score(mid), 4),
                    "node_id": m.metadata.get("node_id", ""),
                }
                for mid, m in self._models.items()
            }

        with self._queue_lock:
            queue = [
                {
                    "model_id": w.model_id,
                    "vram_gb": w.estimated_vram_gb,
                    "node_id": w.node_id,
                    "waiting_s": round(time.time() - w.enqueued_at, 1),
                }
                for w in self._queue_waiters
            ]

        uptime_s = time.time() - self._started_at
        total = self._total_acquires

        return {
            "device": self.device_info.to_dict(),
            "budget_gb": round(self._budget_gb, 2),
            "used_gb": round(self.used_gb, 2),
            "available_gb": round(self.available_gb, 2),
            "loaded_models": loaded,
            "currently_loading": self._gate_holder,
            "queue": queue,
            "queue_depth": len(queue),
            "counters": {
                "total_acquires": total,
                "cache_hits": self._total_cache_hits,
                "cache_hit_rate": round(self._total_cache_hits / max(1, total), 3),
                "total_evictions": self._total_evictions,
                "gate_waits": self._total_gate_waits,
                "uptime_s": round(uptime_s, 0),
            },
            "model_stats": self.get_all_stats(),
        }

    def get_queue(self) -> list[dict]:
        """Current load queue for dashboard."""
        with self._queue_lock:
            return [
                {
                    "model_id": w.model_id,
                    "vram_gb": w.estimated_vram_gb,
                    "node_id": w.node_id,
                    "waiting_s": round(time.time() - w.enqueued_at, 1),
                }
                for w in self._queue_waiters
            ]

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self):
        """Cleanly stop background threads and persist final stats."""
        self._watchdog_stop.set()
        self._persist_stop.set()
        self._persist_stats()
        log.info("ResourceManager shut down, stats persisted")


def build_resource_manager_tools(resource_manager: "ResourceManager"):
    """Build tools for inspecting and managing GPU resources."""

    def execute_status(**_kw):
        return _json.dumps(resource_manager.get_status(), default=str)

    def execute_unload(model_id: str = "", **_kw):
        if not model_id:
            return _json.dumps({"status": "error", "error": "model_id is required"})
        ok = resource_manager.release(model_id)
        if ok:
            return _json.dumps({"status": "ok", "message": f"Unloaded {model_id}"})
        return _json.dumps({"status": "error", "error": f"Model not loaded: {model_id}"})

    def execute_metrics(**_kw):
        return _json.dumps(resource_manager.get_metrics(), default=str)

    def execute_preload(model_id: str = "", estimated_vram_gb: float = 0, **_kw):
        if not model_id:
            return _json.dumps({"status": "error", "error": "model_id is required"})
        has_room = resource_manager.preload_hint(model_id, estimated_vram_gb)
        return _json.dumps({
            "status": "ok",
            "model_id": model_id,
            "already_loaded": model_id in resource_manager.list_loaded(),
            "has_vram_room": has_room,
        })

    return [
        {
            "name": "gpu_status",
            "description": (
                "Show GPU/device status: available devices (CUDA, MPS, MLX), VRAM budget, "
                "loaded models, and memory usage. Use before running heavy AI tasks."
            ),
            "parameters": {"type": "object", "properties": {}},
            "execute": execute_status,
        },
        {
            "name": "gpu_unload_model",
            "description": "Unload a specific AI model from GPU memory to free VRAM.",
            "parameters": {
                "type": "object",
                "properties": {
                    "model_id": {
                        "type": "string",
                        "description": "ID of the model to unload (from gpu_status output)",
                    },
                },
                "required": ["model_id"],
            },
            "execute": execute_unload,
        },
        {
            "name": "gpu_metrics",
            "description": (
                "Show detailed GPU load balancer metrics: per-model usage stats, "
                "eviction scores, cache hit rate, queue depth, load times. "
                "More detailed than gpu_status."
            ),
            "parameters": {"type": "object", "properties": {}},
            "execute": execute_metrics,
        },
        {
            "name": "gpu_preload",
            "description": (
                "Check if a model can be preloaded (is there enough VRAM room?). "
                "Use before starting a heavy pipeline to verify resource availability."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "model_id": {
                        "type": "string",
                        "description": "HuggingFace model ID to check.",
                    },
                    "estimated_vram_gb": {
                        "type": "number",
                        "description": "Estimated VRAM needed in GB.",
                    },
                },
                "required": ["model_id"],
            },
            "execute": execute_preload,
        },
    ]
