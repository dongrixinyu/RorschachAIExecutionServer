import asyncio
import queue
import signal
import uuid
from dataclasses import dataclass
from typing import Optional, Dict, Any
import websockets
from loguru import logger

import config
from realtime_dialog_client import RealtimeDialogClient


# The AudioConfig and AudioDeviceManager classes are no longer needed here,
# as audio is handled by the browser.


class DialogSession:
    """
    Manages a single conversation session between a browser client and the
    ByteDance service. Each browser connection gets its own DialogSession instance.
    """
    def __init__(self, ws_config: Dict[str, Any], websocket, 
                 output_audio_format: str = "pcm", mod: str = "audio", 
                 recv_timeout: int = 10, speaker: Optional[str] = None, 
                 auto_greet: bool = False, diag_phase: Optional[str] = None):
        self.client_websocket = websocket  # WebSocket connection to the browser
        self.mod = (mod or "audio").lower()
        self.session_id = str(uuid.uuid4())
        self.speaker = speaker
        self.auto_greet = auto_greet
        self.diag_phase = diag_phase

        # This client connects to the ByteDance service
        self.client = RealtimeDialogClient(
            config=ws_config, 
            session_id=self.session_id,
            output_audio_format=output_audio_format, 
            mod=mod, 
            recv_timeout=recv_timeout,
            diag_phase=self.diag_phase
        )

        self.is_running = True
        self.is_session_finished = False
        
        # A queue to hold audio data received from ByteDance 
        # before sending to the browser
        self.audio_output_queue = asyncio.Queue()

    def handle_server_response(self, response: Dict[str, Any]) -> None:
        """Handles responses from the ByteDance service."""
        if not response:
            return

        message_type = response.get('message_type')
        
        # 兼容性：有些实现可能用 ACK 下发音频，也可能直接在 FULL_RESPONSE 中下发原始字节
        payload = response.get('payload_msg')
        if isinstance(payload, (bytes, bytearray)):
            audio_len = len(payload)
            try:
                self.audio_output_queue.put_nowait(payload)
                logger.info(f"[Audio] Enqueued {audio_len} bytes from {message_type}")
            except Exception as e:
                logger.info(f"[Audio] Failed to enqueue audio ({audio_len} bytes): {e}")

        elif message_type == 'SERVER_FULL_RESPONSE':
            logger.info(f"Server full response: {response}")
            # You can handle other events (450, 459, etc.) here if needed
            event = response.get('event')
            if event == 450:
                # 该事件下，模型正在输出播放音频，但被用户打断，立即停止播放，接收用户说话
                logger.info(f"清空缓存音频: {response['session_id']}")
                while not self.audio_output_queue.empty():
                    try:
                        self.audio_output_queue.get_nowait()
                    except queue.Empty:
                        continue
            #     self.is_user_querying = True
            
        elif message_type == 'SERVER_ERROR':
            logger.info(f"Server error: {response['payload_msg']}")
            # Consider closing the session on error
            self.is_running = False

    async def receive_from_bytedance_loop(self):
        """Receives messages from the ByteDance service."""
        try:
            while self.is_running:
                response = await self.client.receive_server_response()
                self.handle_server_response(response)
                if response.get('event') in [152, 153]: # Session finished events
                    logger.info(f"Receive session finished event: {response['event']}")
                    self.is_session_finished = True
                    break

        except Exception as e:
            logger.info(f"Error receiving from ByteDance: {e}")
        finally:
            self.is_running = False

    async def send_to_client_loop(self):
        """Sends audio from the output queue to the browser client."""
        try:
            while self.is_running or not self.audio_output_queue.empty():
                try:
                    audio_data = await asyncio.wait_for(
                        self.audio_output_queue.get(), timeout=1.0)
                    await self.client_websocket.send(audio_data)
                    logger.info(f"[Audio] Sent {len(audio_data)} bytes to browser")
                except asyncio.TimeoutError:
                    continue  # No data, just continue waiting

        except Exception as e:
            logger.info(f"Error sending to client: {e}")
        finally:
            self.is_running = False
    
    async def send_tts_text(
            self, start: bool, end: bool, content: str, 
            is_user_querying: bool = False):
        """Sends a TTS text request via the upstream client."""
        if not self.is_running or not self.client:
            return
        await self.client.chat_tts_text(is_user_querying=is_user_querying, start=start, end=end, content=content)
    
    async def process_client_audio(self, audio_data: bytes):
        """Processes an audio chunk received from the browser client."""
        if self.is_running and self.client:
            await self.client.task_request(audio_data)

    async def start(self) -> None:
        """Starts the dialog session."""
        try:
            # apply per-session speaker if provided
            if self.speaker:
                try:
                    self.client.start_session_req["tts"]["speaker"] = self.speaker
                except Exception as e:
                    logger.info(f"Failed to set speaker '{self.speaker}': {e}")
            try:
                self.client.start_session_req["dialog"]["extra"]["input_mod"] = self.mod
            except Exception as e:
                logger.info(f"Failed to set input_mod '{self.mod}': {e}")

            await self.client.connect()
            # 可选问候：仅当明确开启时才发送，避免覆盖前端首段播报
            if self.auto_greet:
                await self.client.say_hello()

            # Create concurrent tasks for receiving from ByteDance and sending to the client
            self.receive_task = asyncio.create_task(self.receive_from_bytedance_loop())
            self.send_task = asyncio.create_task(self.send_to_client_loop())

            logger.info("Session started. Ready for audio.")
        except Exception as e:
            logger.info(f"Session failed to start: {e}")
            self.is_running = False

    async def close(self):
        """Gracefully closes the session and cleans up resources."""
        self.is_running = False
        try:
            if self.client and not self.is_session_finished:
                await self.client.finish_session()
            
            # Wait for loops to finish
            if hasattr(self, 'receive_task'):
                await self.receive_task
            if hasattr(self, 'send_task'):
                await self.send_task

            if self.client:
                await self.client.close()
            logger.info(f"Session {self.session_id} closed.")

        except Exception as e:
            logger.info(f"Error during session cleanup: {e}")