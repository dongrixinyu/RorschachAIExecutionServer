import asyncio
import json
import websockets
from audio_manager import DialogSession  # 从 audio_manager.py 导入我们刚刚修改好的类
import config                          # 您的配置文件
from loguru import logger


# 用于跟踪所有活跃的客户端会话
active_sessions = set()


async def client_handler(websocket):
    """
    处理单个浏览器客户端的连接。
    为每个新连接创建一个 DialogSession 实例来管理与字节跳动API的对话。
    """
    logger.info("Client connected.")
    session = None
    try:
        # 尝试读取初始化消息以设置 speaker / mode
        init_speaker = None
        init_mode = None
        first_message = None
        try:
            first_message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
            if isinstance(first_message, str):
                try:
                    payload = json.loads(first_message)
                    if isinstance(payload, dict) and payload.get("type") in ("init", "set"):
                        if "speaker" in payload:
                            init_speaker = payload["speaker"]
                        if "mode" in payload and isinstance(payload["mode"], str):
                            init_mode = payload["mode"].lower()
                        # 消费掉该初始化消息，不再作为首条业务消息处理
                        first_message = None
                except Exception:
                    # 非 JSON 文本，作为普通业务消息后续处理
                    pass
        except asyncio.TimeoutError:
            # 没有初始化消息，忽略
            pass

        # 为此客户端实例化一个 DialogSession。
        # 传入 websocket 对象，以便 session 可以将数据回传给浏览器。
        session = DialogSession(
            ws_config=config.ws_connect_config,
            output_audio_format="pcm_s16le", # 使用 s16le 以获得更好的 Web 兼容性
            websocket=websocket,
            speaker=init_speaker,
            mod=init_mode if isinstance(init_mode, str) else "audio",
            auto_greet=False  # 启用自动打招呼，让豆包在连接时主动问候
        )
        active_sessions.add(session)

        # 启动会话 (连接到字节跳动服务并等待音频)
        await session.start()

        # 如果存在首条业务消息，先行处理
        if first_message is not None:
            if isinstance(first_message, (bytes, bytearray)):
                if session.mod != "text":
                    await session.process_client_audio(first_message)
            elif isinstance(first_message, str):
                try:
                    payload = json.loads(first_message)
                    msg_type = payload.get("type")
                    if msg_type == "tts_text":
                        await session.send_tts_text(
                            start=bool(payload.get("start", True)),
                            end=bool(payload.get("end", True)),
                            content=str(payload.get("content", "")),
                            is_user_querying=bool(payload.get("is_user_querying", False))
                        )
                    elif msg_type == "text_query":
                        await session.client.chat_text_query(str(payload.get("content", "")))
                except Exception as e:
                    logger.info(f"Failed to handle first text message: {e}")

        # 循环接收来自浏览器客户端的音频数据
        async for message in websocket:
            # 文本消息：用于纯 TTS 播报
            if isinstance(message, str):
                try:
                    payload = json.loads(message)
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    msg_type = payload.get("type")
                    if msg_type == "tts_text":
                        try:
                            await session.send_tts_text(
                                start=bool(payload.get("start", True)),
                                end=bool(payload.get("end", True)),
                                content=str(payload.get("content", "")),
                                is_user_querying=bool(payload.get("is_user_querying", False))
                            )
                        except Exception as e:
                            logger.info(f"Error sending TTS text: {e}")
                        continue
                    if msg_type == "text_query":
                        try:
                            await session.client.chat_text_query(str(payload.get("content", "")))
                        except Exception as e:
                            logger.info(f"Error sending text query: {e}")
                        continue
                # 其它文本类型忽略或扩展
                continue

            # 二进制：视为音频块透传给上游
            if session.mod != "text":
                await session.process_client_audio(message)

    except websockets.exceptions.ConnectionClosed as e:
        logger.info(f"Client connection closed: {e}")
    except Exception as e:
        logger.info(f"An error occurred in the client handler: {e}")
    finally:
        if session:
            logger.info("Cleaning up session resources.")
            await session.close()
            active_sessions.remove(session)
        logger.info("Client disconnected.")


async def main():
    """启动 WebSocket 服务器"""
    host = "localhost"
    port = 8765
    logger.info(f"Starting WebSocket server on ws://{host}:{port}")
    async with websockets.serve(client_handler, host, port):
        await asyncio.Future()  # 永久运行


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server is shutting down.")