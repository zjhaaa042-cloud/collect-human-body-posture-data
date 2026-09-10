import asyncio
import atexit
import base64
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
import glob as glob_mod
import itertools
import os
import queue
import subprocess
import threading
import time
import uuid
from typing import Callable

from loguru import logger

HAS_WINDOWS_TTS = os.name == "nt"
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
try:
    import edge_tts
    import pygame
    HAS_EDGE_TTS = True
except ImportError:
    edge_tts = None
    pygame = None
    HAS_EDGE_TTS = False
HAS_TTS = HAS_WINDOWS_TTS or HAS_EDGE_TTS


@dataclass
class _SpeechRequest:
    text: str
    key: str | None
    generation: int
    future: Future


class VoiceSynthesizer:
    """Queued TTS with bounded Edge synthesis and a Windows offline fallback."""

    def __init__(
        self,
        voice: str = "zh-CN-XiaoxiaoNeural",
        rate: str = "+0%",
        volume: str = "+0%",
        online_timeout_seconds: float = 3.0,
    ):
        self.voice = voice
        self.rate = rate
        self.volume = volume
        self.online_timeout_seconds = max(0.5, float(online_timeout_seconds))
        self.is_speaking = False
        self.last_error = ""
        self._enabled = True
        self._released = False
        self._local_process = None
        self._edge_available = HAS_EDGE_TTS
        self._edge_retry_after = 0.0
        self._mixer_ready = False
        self._generation = 0
        self._activity_callback: Callable[[bool], None] | None = None
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._sequence = itertools.count()
        self._state_lock = threading.RLock()
        self._pending_keys: set[str] = set()
        self._recent_keys: dict[str, float] = {}
        self.temp_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "temp",
            "tts",
        )
        os.makedirs(self.temp_dir, exist_ok=True)
        self._cleanup_stale_files()
        self._worker = threading.Thread(
            target=self._speech_loop,
            name="voice-synthesis",
            daemon=True,
        )
        self._worker.start()
        atexit.register(self.release)
        if HAS_EDGE_TTS:
            logger.info(f"Voice synthesis initialized with Edge voice {self.voice}")
        elif HAS_WINDOWS_TTS:
            logger.info("Edge TTS unavailable; using Windows offline TTS")

    @property
    def available(self) -> bool:
        return HAS_TTS

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_activity_callback(self, callback: Callable[[bool], None] | None) -> None:
        self._activity_callback = callback

    def _notify_activity(self, active: bool) -> None:
        callback = self._activity_callback
        if callback is not None:
            try:
                callback(active)
            except Exception:
                logger.debug("Voice activity callback failed")

    @staticmethod
    def _percentage(value: str, default: int = 0) -> int:
        try:
            return int(str(value).strip().rstrip("%"))
        except (TypeError, ValueError):
            return default

    def _request_active(self, generation: int) -> bool:
        with self._state_lock:
            return self._enabled and not self._released and generation == self._generation

    def _speak_windows(self, text: str, generation: int) -> bool:
        if not self._request_active(generation):
            return False
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        sapi_rate = max(-10, min(10, round(self._percentage(self.rate) / 10)))
        sapi_volume = max(0, min(100, 100 + self._percentage(self.volume)))
        script = (
            "Add-Type -AssemblyName System.Speech;"
            f"$t=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'));"
            "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            f"$s.Rate={sapi_rate};$s.Volume={sapi_volume};$s.Speak($t);$s.Dispose()"
        )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._local_process = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            success = self._local_process.wait(timeout=60) == 0
            return success and self._request_active(generation)
        except Exception as exc:
            self.last_error = f"Windows offline TTS unavailable: {exc}"
            logger.warning(self.last_error)
            return False
        finally:
            self._local_process = None

    async def _synthesize_edge(self, text: str, output_file: str) -> None:
        communicate = edge_tts.Communicate(
            text,
            self.voice,
            rate=self.rate,
            volume=self.volume,
        )
        await communicate.save(output_file)

    def _play_edge(self, text: str, generation: int) -> bool:
        if not self._edge_available or time.monotonic() < self._edge_retry_after:
            return False
        output_file = os.path.join(self.temp_dir, f"tts_{uuid.uuid4().hex[:8]}.mp3")
        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(
                    asyncio.wait_for(
                        self._synthesize_edge(text, output_file),
                        timeout=self.online_timeout_seconds,
                    )
                )
            finally:
                loop.close()
            if not self._request_active(generation) or not os.path.exists(output_file):
                return False
            if not self._mixer_ready:
                pygame.mixer.init()
                self._mixer_ready = True
            pygame.mixer.music.load(output_file)
            pygame.mixer.music.play()
            clock = pygame.time.Clock()
            while pygame.mixer.music.get_busy():
                if not self._request_active(generation):
                    pygame.mixer.music.stop()
                    return False
                clock.tick(20)
            pygame.mixer.music.unload()
            return True
        except asyncio.TimeoutError:
            self._edge_retry_after = time.monotonic() + 30.0
            self.last_error = "Edge TTS synthesis timed out; using local fallback"
            logger.warning(self.last_error)
            return False
        except Exception as exc:
            self._edge_retry_after = time.monotonic() + 30.0
            self.last_error = f"Edge voice unavailable; using local fallback: {exc}"
            logger.warning(self.last_error)
            return False
        finally:
            if os.path.exists(output_file):
                try:
                    os.remove(output_file)
                except OSError:
                    pass

    def _play_sync(self, text: str, generation: int) -> bool:
        if not self._request_active(generation):
            return False
        if self._play_edge(text, generation):
            self.last_error = ""
            return True
        if HAS_WINDOWS_TTS and self._request_active(generation):
            success = self._speak_windows(text, generation)
            if success:
                self.last_error = ""
            return success
        if not self.last_error:
            self.last_error = "Voice playback is unavailable"
        return False

    def _speech_loop(self) -> None:
        while True:
            _priority, _sequence, request = self._queue.get()
            try:
                if request is None:
                    return
                if not self._request_active(request.generation):
                    request.future.set_result(False)
                    continue
                self.is_speaking = True
                self._notify_activity(True)
                result = self._play_sync(request.text, request.generation)
                if not request.future.done():
                    request.future.set_result(result)
            except Exception as exc:
                self.last_error = f"Voice playback unavailable: {exc}"
                logger.warning(self.last_error)
                if request is not None and not request.future.done():
                    request.future.set_result(False)
            finally:
                if request is not None:
                    with self._state_lock:
                        if request.key:
                            self._pending_keys.discard(request.key)
                            self._recent_keys[request.key] = time.monotonic()
                self.is_speaking = False
                self._notify_activity(False)
                self._queue.task_done()

    def speak(
        self,
        text: str,
        blocking: bool = True,
        *,
        key: str | None = None,
        priority: int = 10,
        timeout: float | None = None,
    ) -> bool:
        if not HAS_TTS or not str(text or "").strip():
            return False
        with self._state_lock:
            if not self._enabled or self._released:
                return False
            now = time.monotonic()
            self._recent_keys = {
                recent_key: played_at
                for recent_key, played_at in self._recent_keys.items()
                if now - played_at < 60.0
            }
            if key and (
                key in self._pending_keys
                or now - self._recent_keys.get(key, -1000.0) < 1.0
            ):
                return False
            future: Future = Future()
            request = _SpeechRequest(str(text).strip(), key, self._generation, future)
            if key:
                self._pending_keys.add(key)
            self._queue.put((int(priority), next(self._sequence), request))
        if not blocking:
            return True
        try:
            return bool(future.result(timeout=timeout))
        except FutureTimeoutError:
            self.last_error = "Voice playback exceeded its workflow timeout"
            with self._state_lock:
                self._generation += 1
            self.clear_pending()
            self.stop()
            return False

    def clear_pending(self) -> None:
        while True:
            try:
                _priority, _sequence, request = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                if request is not None:
                    if request.key:
                        with self._state_lock:
                            self._pending_keys.discard(request.key)
                    if not request.future.done():
                        request.future.set_result(False)
            finally:
                self._queue.task_done()

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        with self._state_lock:
            if self._enabled == enabled:
                return
            self._enabled = enabled
            self._generation += 1
        if not enabled:
            self.clear_pending()
            self.stop()

    def stop(self) -> None:
        process = self._local_process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        if self._mixer_ready and pygame is not None:
            try:
                if pygame.mixer.music.get_busy():
                    pygame.mixer.music.stop()
            except Exception:
                pass

    def release(self) -> None:
        with self._state_lock:
            if self._released:
                return
            self._released = True
            self._enabled = False
            self._generation += 1
        self.clear_pending()
        self.stop()
        self._queue.put((10_000, next(self._sequence), None))
        if self._worker.is_alive() and self._worker is not threading.current_thread():
            self._worker.join(timeout=2.0)
        self._cleanup_stale_files()

    def set_voice(self, voice: str):
        self.voice = voice

    def set_rate(self, rate: str):
        self.rate = rate

    def set_volume(self, volume: str):
        self.volume = volume

    def _cleanup_stale_files(self):
        try:
            for path in glob_mod.glob(os.path.join(self.temp_dir, "tts_*.mp3")):
                try:
                    os.remove(path)
                except OSError:
                    pass
        except Exception:
            pass
