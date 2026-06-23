"""
后端启动脚本 - 带错误处理
"""
import sys
import os
import traceback

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def main():
    try:
        print("=" * 60)
        print("人体体态数据采集系统 - 后端服务")
        print("=" * 60)
        print()
        
        # 导入模块
        print("[1/4] 加载配置...")
        from backend.config.settings import get_settings
        settings = get_settings()
        print(f"  模型路径: {settings.voice.model_path}")
        print(f"  WebSocket: {settings.websocket_host}:{settings.websocket_port}")
        print()
        
        # 初始化语音
        print("[2/4] 初始化语音系统...")
        from backend.voice.recognizer import VoiceRecognizer
        from backend.voice.synthesizer import VoiceSynthesizer
        voice_recognizer = VoiceRecognizer(settings.voice.model_path)
        voice_synthesizer = VoiceSynthesizer(
            voice=settings.voice.tts_voice,
            rate=settings.voice.tts_rate,
            volume=settings.voice.tts_volume
        )
        print("  语音系统就绪")
        print()
        
        # 初始化相机
        print("[3/4] 初始化相机...")
        from backend.core.camera_manager import CameraManager
        camera = CameraManager()
        if camera.initialize(
            width=settings.camera.width,
            height=settings.camera.height,
            fps=settings.camera.fps,
            params_file=settings.camera.params_file
        ):
            print("  相机初始化成功")
        else:
            print("  [警告] 相机初始化失败，将使用模拟模式")
        print()
        
        # 启动服务
        print("[4/4] 启动 WebSocket 服务...")
        import asyncio
        from backend.server.ws_server import WebSocketServer
        
        server = WebSocketServer(
            host=settings.websocket_host,
            port=settings.websocket_port
        )
        
        print()
        print("=" * 60)
        print(f"服务已启动: ws://{settings.websocket_host}:{settings.websocket_port}")
        print("按 Ctrl+C 停止服务")
        print("=" * 60)
        print()
        
        asyncio.run(server.start())
        
    except KeyboardInterrupt:
        print("\n正在停止服务...")
        if 'server' in locals():
            server.stop()
        print("服务已停止")
    except Exception as e:
        print(f"\n错误: {e}")
        print()
        print("详细错误信息:")
        traceback.print_exc()
        print()
        input("按 Enter 键退出...")

if __name__ == "__main__":
    main()
