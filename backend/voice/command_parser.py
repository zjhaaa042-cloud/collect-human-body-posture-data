from enum import Enum
import re
from typing import Optional, Dict, Callable
from loguru import logger


class VoiceCommand(Enum):
    START_CAPTURE = "start_capture"
    STOP_CAPTURE = "stop_capture"
    NEXT_POSE = "next_pose"
    REPEAT = "repeat"
    CANCEL = "cancel"
    FINISH = "finish"
    UNKNOWN = "unknown"


class VoiceCommandParser:
    def __init__(self):
        self.command_map: Dict[str, VoiceCommand] = {
            "开始采集": VoiceCommand.START_CAPTURE,
            "重复提示": VoiceCommand.REPEAT,
            "取消采集": VoiceCommand.CANCEL,
        }

        self.callbacks: Dict[VoiceCommand, Callable] = {}

    def parse(self, text: str) -> VoiceCommand:
        try:
            text = re.sub(r"[\s，。！？、,.!?；;：:]+", "", str(text).strip().lower())
            if not text:
                return VoiceCommand.UNKNOWN

            command = self.command_map.get(text)
            if command is not None:
                logger.info(f"Voice command recognized: {text} -> {command.value}")
                return command

            logger.debug(f"Unknown voice command: {text}")
            return VoiceCommand.UNKNOWN
        except Exception as e:
            logger.error(f"Failed to parse voice command: {e}")
            return VoiceCommand.UNKNOWN

    def register_callback(self, command: VoiceCommand, callback: Callable):
        self.callbacks[command] = callback

    def execute_command(self, text: str) -> Optional[VoiceCommand]:
        command = self.parse(text)
        if command in self.callbacks:
            try:
                self.callbacks[command]()
            except Exception as e:
                logger.error(f"Failed to execute command callback: {e}")
        return command

    def get_command_description(self, command: VoiceCommand) -> str:
        descriptions = {
            VoiceCommand.START_CAPTURE: "开始采集",
            VoiceCommand.REPEAT: "重复提示",
            VoiceCommand.CANCEL: "取消采集",
            VoiceCommand.UNKNOWN: "未知指令"
        }
        return descriptions.get(command, "未知指令")

    def accepted_phrases(self) -> tuple[str, ...]:
        return tuple(self.command_map)

    def recognition_phrases(self) -> tuple[str, ...]:
        """Return model-vocabulary tokenization for Vosk's constrained grammar."""
        return (
            "开始 采集",
            "重复 提示",
            "取消 采集",
        )
