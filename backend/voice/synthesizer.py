import asyncio
import os
import tempfile
import threading
import uuid
from typing import Optional
from loguru import logger

try:
    import edge_tts
    import pygame
    HAS_TTS = True
except ImportError:
    HAS_TTS = False
    logger.warning("edge-tts or pygame not found, voice synthesis disabled")


class VoiceSynthesizer:
    def __init__(self, voice: str = "zh-CN-XiaoxiaoNeural", rate: str = "+0%", volume: str = "+0%"):
        self.voice = voice
        self.rate = rate
        self.volume = volume
        self.is_speaking = False
        # Use project directory for temp files to avoid permission issues
        self.temp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "temp", "tts")
        os.makedirs(self.temp_dir, exist_ok=True)

        if HAS_TTS:
            try:
                pygame.mixer.init()
            except Exception as e:
                logger.error(f"Failed to init pygame mixer: {e}")

    async def _synthesize(self, text: str) -> Optional[str]:
        try:
            filename = f"tts_{uuid.uuid4().hex[:8]}.mp3"
            output_file = os.path.join(self.temp_dir, filename)
            communicate = edge_tts.Communicate(text, self.voice, rate=self.rate, volume=self.volume)
            await communicate.save(output_file)
            return output_file
        except Exception as e:
            logger.error(f"TTS synthesis failed: {e}")
            return None

    def speak(self, text: str, blocking: bool = True):
        if not HAS_TTS:
            logger.warning(f"TTS not available, text: {text}")
            return

        if blocking:
            self._speak_sync(text)
        else:
            threading.Thread(target=self._speak_sync, args=(text,), daemon=True).start()

    def _speak_sync(self, text: str):
        output_file = None
        try:
            self.is_speaking = True
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            output_file = loop.run_until_complete(self._synthesize(text))
            loop.close()

            if output_file and os.path.exists(output_file):
                pygame.mixer.music.load(output_file)
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    pygame.time.Clock().tick(10)
                pygame.mixer.music.unload()

            self.is_speaking = False
        except Exception as e:
            logger.error(f"Failed to speak: {e}")
            self.is_speaking = False
        finally:
            if output_file and os.path.exists(output_file):
                try:
                    os.remove(output_file)
                except Exception:
                    pass

    def stop(self):
        if HAS_TTS and pygame.mixer.music.get_busy():
            pygame.mixer.music.stop()
        self.is_speaking = False

    def set_voice(self, voice: str):
        self.voice = voice

    def set_rate(self, rate: str):
        self.rate = rate

    def set_volume(self, volume: str):
        self.volume = volume
