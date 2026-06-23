from enum import Enum
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
            "开始": VoiceCommand.START_CAPTURE,
            "采集": VoiceCommand.START_CAPTURE,
            "拍照": VoiceCommand.START_CAPTURE,
            "停止采集": VoiceCommand.STOP_CAPTURE,
            "停止": VoiceCommand.STOP_CAPTURE,
            "下一个": VoiceCommand.NEXT_POSE,
            "下一张": VoiceCommand.NEXT_POSE,
            "下一个姿势": VoiceCommand.NEXT_POSE,
            "重复": VoiceCommand.REPEAT,
            "再来一次": VoiceCommand.REPEAT,
            "重拍": VoiceCommand.REPEAT,
            "取消": VoiceCommand.CANCEL,
            "算了": VoiceCommand.CANCEL,
            "完成": VoiceCommand.FINISH,
            "结束": VoiceCommand.FINISH,
            "好了": VoiceCommand.FINISH,
            "完毕": VoiceCommand.FINISH,
        }

        self.callbacks: Dict[VoiceCommand, Callable] = {}

    def parse(self, text: str) -> VoiceCommand:
        try:
            text = text.strip().lower()
            if not text:
                return VoiceCommand.UNKNOWN

            for keyword, command in self.command_map.items():
                if keyword in text:
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
            VoiceCommand.STOP_CAPTURE: "停止采集",
            VoiceCommand.NEXT_POSE: "下一个姿势",
            VoiceCommand.REPEAT: "重新采集",
            VoiceCommand.CANCEL: "取消操作",
            VoiceCommand.FINISH: "完成采集",
            VoiceCommand.UNKNOWN: "未知指令"
        }
        return descriptions.get(command, "未知指令")
