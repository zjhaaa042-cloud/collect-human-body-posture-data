"""
后端启动脚本 - 带错误处理
"""
import sys
import os
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def main():
    try:
        print("=" * 60)
        print("人体体态数据采集系统 - 后端服务")
        print("=" * 60)
        print()

        print("[1/3] 加载配置...")
        from backend.config.settings import load_settings, get_settings
        config_path = os.environ.get("BODY_POSTURE_CONFIG_FILE") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "config.json"
        )
        if os.path.exists(config_path):
            settings = load_settings(config_path)
            print(f"  已加载: {config_path}")
        else:
            settings = get_settings()
            print("  使用默认配置 (config.json 不存在)")
        print(f"  模型路径: {settings.voice.model_path}")
        print(f"  WebSocket: {settings.websocket_host}:{settings.websocket_port}")
        print()

        print("[2/3] 启动 WebSocket 服务...")
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
        if os.environ.get("BODY_POSTURE_PACKAGED") != "1":
            input("按 Enter 键退出...")

if __name__ == "__main__":
    main()
