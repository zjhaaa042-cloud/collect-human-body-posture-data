import threading
import queue
import struct
import math
import json
import time
from pathlib import Path
import numpy as np
from typing import Callable, Optional
from loguru import logger

try:
    import vosk
    import pyaudio
    vosk.SetLogLevel(-1)
    HAS_VOSK = True
except ImportError:
    HAS_VOSK = False
    logger.warning("Vosk or PyAudio not found, voice recognition disabled")


class VoiceRecognizer:
    def __init__(self, model_path: str, sample_rate: int = 16000):
        self.model_path = model_path
        self.sample_rate = sample_rate
        self.model = None
        self.recognizer = None
        self.audio = None
        self.stream = None
        self.is_listening = False
        self.callback = None
        self.activity_callback = None
        self.thread = None
        self.is_speaking = False
        self.rms_threshold = 200  # Lower threshold for better detection
        # 100 ms chunks keep microphone and partial recognition latency low.
        self.chunk_size = max(800, self.sample_rate // 10)
        self._last_partial = ""
        self.initialization_error = ""

        if HAS_VOSK:
            self._initialize()

    def _initialize(self):
        model_path = Path(self.model_path)
        if not model_path.is_absolute():
            project_root = Path(__file__).resolve().parents[2]
            model_path = project_root / model_path

        required_files = (
            model_path / "am" / "final.mdl",
            model_path / "conf" / "model.conf",
        )
        if not model_path.is_dir() or not all(path.is_file() for path in required_files):
            self.initialization_error = (
                f"Vosk model is not installed or incomplete: {model_path}. "
                "Extract a Chinese Vosk model so it contains am/final.mdl and conf/model.conf."
            )
            logger.warning(f"Voice recognition disabled: {self.initialization_error}")
            return

        try:
            self.model_path = str(model_path)
            self.model = vosk.Model(self.model_path)
            self.recognizer = vosk.KaldiRecognizer(self.model, self.sample_rate)
            self.audio = pyaudio.PyAudio()
            
            # List available audio devices
            info = self.audio.get_host_api_info_by_index(0)
            numdevices = info.get('deviceCount')
            logger.info(f"Found {numdevices} audio devices")
            for i in range(0, numdevices):
                device_info = self.audio.get_device_info_by_host_api_device_index(0, i)
                if device_info.get('maxInputChannels') > 0:
                    logger.info(f"  Input Device {i}: {device_info.get('name')}")
            
            logger.info(f"Voice recognizer initialized with model: {self.model_path}")
        except Exception as e:
            self.initialization_error = str(e)
            self.model = None
            logger.warning(f"Voice recognition disabled: {e}")

    def start_listening(self, callback: Callable[[str], None], activity_callback: Callable[[bool], None] = None):
        if not HAS_VOSK or not self.model:
            return False

        self.callback = callback
        self.activity_callback = activity_callback
        self.is_listening = True

        try:
            self.stream = self.audio.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=self.sample_rate,
                input=True,
                frames_per_buffer=self.chunk_size
            )

            self.thread = threading.Thread(target=self._listen_loop, daemon=True)
            self.thread.start()
            logger.info("Voice recognition started")
            return True
        except Exception as e:
            logger.error(f"Failed to start voice recognition: {e}")
            self.is_listening = False
            return False

    def stop_listening(self):
        self.is_listening = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
        logger.info("Voice recognition stopped")

    def _calculate_rms(self, data):
        """Calculate RMS (Root Mean Square) of audio data for activity detection"""
        try:
            audio_data = np.frombuffer(data, dtype=np.int16)
            if len(audio_data) == 0:
                return 0
            rms = np.sqrt(np.mean(audio_data.astype(float) ** 2))
            return rms
        except Exception:
            return 0

    def _listen_loop(self):
        logger.info("Voice listen loop started")
        while self.is_listening:
            try:
                if not self.stream:
                    break
                    
                data = self.stream.read(self.chunk_size, exception_on_overflow=False)
                
                # Audio activity detection
                rms = self._calculate_rms(data)
                is_active = rms > self.rms_threshold
                
                if self.activity_callback and is_active != self.is_speaking:
                    self.is_speaking = is_active
                    logger.debug(f"Voice activity: {is_active} (RMS: {rms:.1f})")
                    self.activity_callback(is_active)
                
                if self.recognizer.AcceptWaveform(data):
                    result = self.recognizer.Result()
                    if isinstance(result, str):
                        try:
                            result = json.loads(result)
                        except Exception:
                            result = {}
                    text = result.get('text', '') if isinstance(result, dict) else ''
                    if text:
                        self._last_partial = ""
                        logger.info(f"Voice recognized: {text}")
                        if self.callback:
                            self.callback(text)
                else:
                    # Vosk's final result waits for an end-of-speech pause.  Commands
                    # can be acted on safely from a changed partial result instead.
                    try:
                        partial = json.loads(self.recognizer.PartialResult()).get("partial", "").strip()
                    except Exception:
                        partial = ""
                    if partial and partial != self._last_partial:
                        self._last_partial = partial
                        if self.callback:
                            self.callback(partial)
            except Exception as e:
                logger.error(f"Voice recognition error: {e}")
                time.sleep(0.05)
        
        logger.info("Voice listen loop ended")

    def set_callback(self, callback: Callable[[str], None]):
        self.callback = callback

    def release(self):
        self.stop_listening()
        if self.audio:
            self.audio.terminate()
