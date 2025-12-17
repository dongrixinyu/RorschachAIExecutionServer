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
        diag_phase = None
        first_message = None
        try:
            first_message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
            logger.info(f"[Client] Received first_message raw: {repr(first_message)}")
            if isinstance(first_message, str):
                try:
                    payload = json.loads(first_message)
                    logger.info(f"[Client] Parsed first_message payload: {payload}")
                    if isinstance(payload, dict) and payload.get("type") in ("init", "set"):
                        if "speaker" in payload:
                            init_speaker = payload["speaker"]
                        if "mode" in payload and isinstance(payload["mode"], str):
                            init_mode = payload["mode"].lower()
                        # 消费掉该初始化消息，不再作为首条业务消息处理
                        if "phase" in payload:
                            # 有三种初始化状态，它代表了对话服务，AI 将采用何种 prompt 来完成对话
                            # pretest、intest、posttest
                            diag_phase = payload["phase"]
                        first_message = None

                except Exception:
                    # 非 JSON 文本，作为普通业务消息后续处理
                    logger.warning(f"[Client] Failed to parse first_message as JSON, will treat as business message")

        except asyncio.TimeoutError:
            # 没有初始化消息，忽略
            logger.info("[Client] No init message received within timeout, continue without init")

        # 为此客户端实例化一个 DialogSession。
        # 传入 websocket 对象，以便 session 可以将数据回传给浏览器。
        logger.info(
            f"[Client] Creating DialogSession: speaker={init_speaker}, "
            f"mode={init_mode}, diag_phase={diag_phase}"
        )
        session = DialogSession(
            ws_config=config.ws_connect_config,
            output_audio_format="pcm_s16le",  # 使用 s16le 以获得更好的 Web 兼容性
            websocket=websocket,
            speaker=init_speaker,
            mod=init_mode if isinstance(init_mode, str) else "audio",
            auto_greet=False,  # 是否自动打招呼
            diag_phase=diag_phase,  # 指定不同的对话 prompt
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
                logger.info(f"[Client] Received text message from browser: {message}")
                try:
                    payload = json.loads(message)
                except Exception:
                    logger.warning("[Client] Failed to parse browser text message as JSON")
                    payload = None
                if isinstance(payload, dict):
                    msg_type = payload.get("type")
                    logger.info(f"[Client] Parsed browser payload: type={msg_type}, keys={list(payload.keys())}")
                    if msg_type == "tts_text":
                        try:
                            content_preview = str(payload.get("content", ""))[:80].replace("\n", " ")
                            logger.info(
                                f"[Client] Handling tts_text: "
                                f"start={bool(payload.get('start', True))}, "
                                f"end={bool(payload.get('end', True))}, "
                                f"is_user_querying={bool(payload.get('is_user_querying', False))}, "
                                f"content_preview='{content_preview}'"
                            )
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
                            content = str(payload.get("content", ""))
                            preview = content[:80].replace("\n", " ")
                            logger.info(f"[Client] Handling text_query: content_preview='{preview}'")
                            await session.client.chat_text_query(content)
                        except Exception as e:
                            logger.info(f"Error sending text query: {e}")
                        continue
                # 其它文本类型忽略或扩展
                continue

            # 二进制：视为音频块透传给上游
            if session.mod != "text":
                logger.debug(f"[Client] Received binary audio chunk from browser: {len(message)} bytes")
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