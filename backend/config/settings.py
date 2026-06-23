import os
from pathlib import Path
from typing import Optional
from pydantic import BaseModel
from loguru import logger


class CameraSettings(BaseModel):
    width: int = 640
    height: int = 480
    fps: int = 30
    align_mode: str = "D2C_HW"
    params_file: Optional[str] = "config/camera_params.json"


class VoiceSettings(BaseModel):
    enabled: bool = True
    model_path: str = "models/vosk-model-small-cn-0.22"
    language: str = "zh"
    tts_voice: str = "zh-CN-XiaoxiaoNeural"
    tts_rate: str = "+0%"
    tts_volume: str = "+0%"


class StorageSettings(BaseModel):
    output_dir: str = "data"
    save_rgb: bool = True
    save_depth: bool = True
    save_pointcloud: bool = True
    colored_pointcloud: bool = True
    quality_check: bool = True
    min_depth_coverage: float = 0.3
    min_color_brightness: int = 30
    max_color_brightness: int = 220


class DistanceSettings(BaseModel):
    target_distance_mm: float = 1000
    tolerance_mm: float = 200
    roi_ratio: float = 0.3


class GUISettings(BaseModel):
    preview_fps: int = 20
    preview_width: int = 320
    preview_height: int = 240
    jpeg_quality: int = 50


class Settings(BaseModel):
    camera: CameraSettings = CameraSettings()
    voice: VoiceSettings = VoiceSettings()
    storage: StorageSettings = StorageSettings()
    distance: DistanceSettings = DistanceSettings()
    gui: GUISettings = GUISettings()

    websocket_host: str = "localhost"
    websocket_port: int = 8765

    log_level: str = "INFO"
    log_file: Optional[str] = "logs/app.log"


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def load_settings(config_path: str) -> Settings:
    global _settings
    try:
        import json
        with open(config_path, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
        _settings = Settings(**config_data)
        logger.info(f"Settings loaded from {config_path}")
        return _settings
    except Exception as e:
        logger.error(f"Failed to load settings: {e}")
        _settings = Settings()
        return _settings


def save_settings(settings: Settings, config_path: str):
    try:
        import json
        config_path = Path(config_path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(settings.model_dump(), f, indent=2, ensure_ascii=False)
        logger.info(f"Settings saved to {config_path}")
    except Exception as e:
        logger.error(f"Failed to save settings: {e}")
