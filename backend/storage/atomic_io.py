"""Durable, Unicode-safe filesystem helpers used by capture stores.

The helpers in this module deliberately avoid copy/delete fallbacks.  A
capture is either atomically promoted into place or its staging evidence is
left untouched for recovery.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

import numpy as np


WINDOWS_TRANSIENT_REPLACE_ERRORS = {5, 32, 33}
REPLACE_RETRY_DELAYS_SECONDS = (0.1, 0.25, 0.5, 1.0, 2.0)


class AtomicIOError(RuntimeError):
    """Raised when a durable filesystem operation cannot be completed."""


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_join(root: str | Path, relative_path: str | Path) -> Path:
    """Resolve a relative path and prove that it stays below *root*."""

    root_path = Path(root).resolve()
    candidate_input = Path(relative_path)
    if candidate_input.is_absolute():
        raise AtomicIOError("文件清单路径必须是相对路径")
    candidate = (root_path / candidate_input).resolve()
    try:
        candidate.relative_to(root_path)
    except ValueError as exc:
        raise AtomicIOError("文件清单路径越出采集目录") from exc
    return candidate


def replace_with_retry(
    source: str | Path,
    destination: str | Path,
    *,
    allow_existing_destination: bool = False,
) -> None:
    source_path = Path(source)
    destination_path = Path(destination)
    if destination_path.exists() and not allow_existing_destination:
        raise AtomicIOError(f"拒绝覆盖已存在路径：{destination_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt_index in range(len(REPLACE_RETRY_DELAYS_SECONDS) + 1):
        try:
            os.replace(source_path, destination_path)
            return
        except PermissionError as exc:
            winerror = getattr(exc, "winerror", None)
            error_code = winerror if winerror is not None else getattr(exc, "errno", None)
            retryable = os.name == "nt" and error_code in WINDOWS_TRANSIENT_REPLACE_ERRORS
            if not retryable or attempt_index >= len(REPLACE_RETRY_DELAYS_SECONDS):
                raise
            time.sleep(REPLACE_RETRY_DELAYS_SECONDS[attempt_index])


def _unique_temporary(destination: Path) -> Path:
    return destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")


def atomic_write_bytes(destination: str | Path, payload: bytes) -> None:
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _unique_temporary(destination_path)
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temporary, destination_path, allow_existing_destination=True)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def atomic_write_json(destination: str | Path, value: Mapping[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    atomic_write_bytes(destination, payload)


def atomic_write_npy(destination: str | Path, array: np.ndarray) -> dict[str, Any]:
    """Write and round-trip verify a two-dimensional uint16 NPY array."""

    value = np.ascontiguousarray(np.asarray(array))
    if value.ndim != 2 or value.dtype != np.uint16:
        raise AtomicIOError("深度 NPY 必须是二维 uint16 数组")
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _unique_temporary(destination_path)
    try:
        with temporary.open("xb") as handle:
            np.save(handle, value, allow_pickle=False)
            handle.flush()
            os.fsync(handle.fileno())
        loaded = np.load(temporary, allow_pickle=False)
        if (
            loaded.dtype != np.uint16
            or loaded.shape != value.shape
            or not loaded.flags.c_contiguous
            or not np.array_equal(loaded, value)
        ):
            raise AtomicIOError(f"NPY 写入回读校验失败：{destination_path.name}")
        replace_with_retry(temporary, destination_path, allow_existing_destination=False)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
    return {
        "format": "npy",
        "dtype": "uint16",
        "shape": list(value.shape),
        "order": "C",
        "allow_pickle": False,
        "size_bytes": destination_path.stat().st_size,
        "sha256": sha256_file(destination_path),
    }


class DatasetLease:
    """Process-wide shared wrapper around an OS-level exclusive file lease."""

    _guard = threading.Lock()
    _leases: dict[str, dict[str, Any]] = {}

    def __init__(self, lock_path: str | Path, *, error_message: str) -> None:
        self.path = Path(lock_path).resolve()
        self.error_message = error_message
        self._key = os.path.normcase(str(self.path))
        self._released = False
        self._acquire()

    def _acquire(self) -> None:
        with self._guard:
            existing = self._leases.get(self._key)
            if existing is not None:
                existing["count"] += 1
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            backend = ""
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                    os.fsync(handle.fileno())
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    backend = "msvcrt"
                else:  # pragma: no cover - Windows is the supported target.
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    backend = "fcntl"
            except (OSError, BlockingIOError) as exc:
                handle.close()
                raise AtomicIOError(self.error_message) from exc
            self._leases[self._key] = {
                "handle": handle,
                "backend": backend,
                "count": 1,
            }

    def release(self) -> None:
        if self._released:
            return
        with self._guard:
            entry = self._leases.get(self._key)
            self._released = True
            if entry is None:
                return
            entry["count"] -= 1
            if entry["count"] > 0:
                return
            handle = entry["handle"]
            try:
                handle.seek(0)
                if entry["backend"] == "msvcrt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                elif entry["backend"] == "fcntl":  # pragma: no cover
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
                self._leases.pop(self._key, None)

    def __enter__(self) -> "DatasetLease":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()

    def __del__(self) -> None:  # pragma: no cover - interpreter timing varies.
        try:
            self.release()
        except Exception:
            pass
