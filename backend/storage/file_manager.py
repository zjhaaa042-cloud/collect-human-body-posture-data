import json
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any
from loguru import logger


class FileManager:
    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)
        self.sessions_dir = self.base_dir / "sessions"
        self.exports_dir = self.base_dir / "exports"
        self.config_dir = self.base_dir / "config"

        self._ensure_directories()

    def _ensure_directories(self):
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)

    def create_session(self, session_name: Optional[str] = None) -> str:
        try:
            if session_name is None:
                session_name = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

            session_path = self.sessions_dir / session_name
            session_path.mkdir(parents=True, exist_ok=True)

            (session_path / "rgb").mkdir(exist_ok=True)
            (session_path / "depth").mkdir(exist_ok=True)
            (session_path / "pointcloud").mkdir(exist_ok=True)

            logger.info(f"Session directory created: {session_path}")
            return session_name
        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            raise

    def get_session_path(self, session_name: str) -> Path:
        return self.sessions_dir / session_name

    def get_session_list(self) -> List[str]:
        try:
            if not self.sessions_dir.exists():
                return []
            return [d.name for d in self.sessions_dir.iterdir() if d.is_dir()]
        except Exception as e:
            logger.error(f"Failed to get session list: {e}")
            return []

    def get_session_metadata(self, session_name: str) -> Optional[Dict[str, Any]]:
        try:
            metadata_path = self.sessions_dir / session_name / "metadata.json"
            if metadata_path.exists():
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return None
        except Exception as e:
            logger.error(f"Failed to get session metadata: {e}")
            return None

    def save_session_metadata(self, session_name: str, metadata: Dict[str, Any]):
        try:
            metadata_path = self.sessions_dir / session_name / "metadata.json"
            with open(metadata_path, 'w', encoding='utf-8') as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save session metadata: {e}")

    def export_session(self, session_name: str) -> Optional[str]:
        try:
            session_path = self.sessions_dir / session_name
            if not session_path.exists():
                logger.error(f"Session not found: {session_name}")
                return None

            export_path = self.exports_dir / f"{session_name}.zip"
            shutil.make_archive(str(export_path.with_suffix('')), 'zip', session_path)

            logger.info(f"Session exported: {export_path}")
            return str(export_path)
        except Exception as e:
            logger.error(f"Failed to export session: {e}")
            return None

    def delete_session(self, session_name: str) -> bool:
        try:
            session_path = self.sessions_dir / session_name
            if session_path.exists():
                shutil.rmtree(session_path)
                logger.info(f"Session deleted: {session_name}")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to delete session: {e}")
            return False

    def get_storage_stats(self) -> Dict[str, Any]:
        try:
            total_size = 0
            session_count = 0

            if self.sessions_dir.exists():
                for session_dir in self.sessions_dir.iterdir():
                    if session_dir.is_dir():
                        session_count += 1
                        for file in session_dir.rglob('*'):
                            if file.is_file():
                                total_size += file.stat().st_size

            return {
                "session_count": session_count,
                "total_size_bytes": total_size,
                "total_size_mb": round(total_size / (1024 * 1024), 2)
            }
        except Exception as e:
            logger.error(f"Failed to get storage stats: {e}")
            return {"session_count": 0, "total_size_bytes": 0, "total_size_mb": 0}

    def load_config(self, config_name: str) -> Optional[Dict[str, Any]]:
        try:
            config_path = self.config_dir / f"{config_name}.json"
            if config_path.exists():
                with open(config_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return None
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            return None

    def save_config(self, config_name: str, config: Dict[str, Any]):
        try:
            config_path = self.config_dir / f"{config_name}.json"
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
