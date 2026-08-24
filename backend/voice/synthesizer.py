import asyncio
import atexit
import base64
import glob as glob_mod
import os
import subprocess
import threading
import time
import uuid
from typing import Optional

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


class VoiceSynthesizer:
    """Offline Windows speech first, with Edge TTS as a non-Windows fallback."""

    def __init__(self, voice: str = "zh-CN-XiaoxiaoNeural", rate: str = "+0%", volume: str = "+0%"):
        self.voice = voice
        self.rate = rate
        self.volume = volume
        self.is_speaking = False
        self._speak_lock = threading.Lock()
        self._local_process = None
        self._edge_available = HAS_EDGE_TTS
        self._edge_retry_after = 0.0
        self._mixer_ready = False
        self.temp_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "temp",
            "tts",
        )
        os.makedirs(self.temp_dir, exist_ok=True)
        self._cleanup_stale_files()
        atexit.register(self._cleanup_all_temp)
        if HAS_EDGE_TTS:
            logger.info(f"Voice synthesis initialized with Edge voice {self.voice}")
        elif HAS_WINDOWS_TTS:
            logger.info("Edge TTS unavailable; using Windows offline TTS")

    @staticmethod
    def _percentage(value: str, default: int = 0) -> int:
        try:
            return int(str(value).strip().rstrip("%"))
        except (TypeError, ValueError):
            return default

    def _speak_windows(self, text: str) -> bool:
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
            return self._local_process.wait(timeout=60) == 0
        except Exception as exc:
            logger.warning(f"Windows offline TTS unavailable: {exc}")
            return False
        finally:
            self._local_process = None

    async def _synthesize_edge(self, text: str) -> Optional[str]:
        if not self._edge_available or time.monotonic() < self._edge_retry_after:
            return None
        output_file = os.path.join(self.temp_dir, f"tts_{uuid.uuid4().hex[:8]}.mp3")
        try:
            communicate = edge_tts.Communicate(text, self.voice, rate=self.rate, volume=self.volume)
            await communicate.save(output_file)
            return output_file
        except Exception as exc:
            self._edge_retry_after = time.monotonic() + 30.0
            logger.warning(f"Edge voice temporarily unavailable; using local fallback: {exc}")
            try:
                if os.path.exists(output_file):
                    os.remove(output_file)
            except OSError:
                pass
            return None

    def speak(self, text: str, blocking: bool = True):
        if not HAS_TTS or not text:
            return False
        if blocking:
            return self._speak_sync(text, wait_for_lock=True)
        threading.Thread(target=self._speak_sync, args=(text, False), daemon=True).start()
        return True

    def _speak_sync(self, text: str, wait_for_lock: bool = True):
        acquired = self._speak_lock.acquire(blocking=wait_for_lock)
        if not acquired:
            return False
        output_file = None
        try:
            self.is_speaking = True
            if self._edge_available:
                loop = asyncio.new_event_loop()
                try:
                    output_file = loop.run_until_complete(self._synthesize_edge(text))
                finally:
                    loop.close()
                if output_file and os.path.exists(output_file):
                    try:
                        if not self._mixer_ready:
                            pygame.mixer.init()
                            self._mixer_ready = True
                        pygame.mixer.music.load(output_file)
                        pygame.mixer.music.play()
                        while pygame.mixer.music.get_busy():
                            pygame.time.Clock().tick(20)
                        pygame.mixer.music.unload()
                        return True
                    except Exception as exc:
                        logger.warning(f"Edge voice playback unavailable: {exc}")

            # Keep the application audible when Edge TTS can't reach the network.
            if HAS_WINDOWS_TTS:
                return self._speak_windows(text)
            return False
        except Exception as exc:
            logger.warning(f"Voice playback unavailable: {exc}")
            return False
        finally:
            self.is_speaking = False
            self._speak_lock.release()
            if output_file and os.path.exists(output_file):
                try:
                    os.remove(output_file)
                except OSError:
                    pass

    def stop(self):
        process = self._local_process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        if self._mixer_ready and pygame.mixer.music.get_busy():
            pygame.mixer.music.stop()
        self.is_speaking = False

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

    def _cleanup_all_temp(self):
        self._cleanup_stale_files()
