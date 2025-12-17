import asyncio
import queue
import signal
import uuid
import time
import json
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
    def __init__(
        self,
        ws_config: Dict[str, Any],
        websocket,
        output_audio_format: str = "pcm",
        mod: str = "audio",
        recv_timeout: int = 10,
        speaker: Optional[str] = None,
        auto_greet: bool = False,
        diag_phase: Optional[str] = None,
    ):
        self.client_websocket = websocket  # WebSocket connection to the browser
        self.mod = (mod or "audio").lower()
        self.session_id = str(uuid.uuid4())
        self.speaker = speaker
        self.auto_greet = auto_greet
        self.diag_phase = diag_phase

        logger.info(
            f"[DialogSession] 初始化: session_id={self.session_id}, "
            f"mod={self.mod}, speaker={self.speaker}, diag_phase={self.diag_phase}"
        )

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
        
        # A queue to hold text transcription messages received from ByteDance
        # before sending to the browser
        self.text_output_queue = asyncio.Queue()
        
        # Track user speech recognition state for deduplication
        # Key: question_id, Value: last sent text
        self.user_speech_cache: Dict[str, str] = {}
        
        # Track assistant speech text for accumulation
        # Key: reply_id, Value: accumulated text
        self.assistant_speech_cache: Dict[str, str] = {}

    def handle_server_response(self, response: Dict[str, Any]) -> None:
        """Handles responses from the ByteDance service."""
        if not response:
            return

        message_type = response.get('message_type')
        event = response.get('event')
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
            self._extract_and_queue_text(response, event, payload)
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
    
    def _extract_and_queue_text(self, response: Dict[str, Any], event: Optional[int], payload: Any) -> None:
        """
        Extract text from ByteDance API responses and queue for sending to browser.
        
        Event types:
        - 550: Assistant TTS text (流式返回，每次一小段)
        - 451: User ASR recognition result (包含中间结果和最终结果)
        - 450: User speech start event (可选，用于标记开始)
        - 350: TTS task start (包含 reply_id，用于标记新的回复开始)
        - 359: TTS task end (no_content=True，表示回复结束)
        """
        if not isinstance(payload, dict):
            return
        
        timestamp = int(time.time() * 1000)  # milliseconds
        
        # Event 550: Assistant TTS text (豆包说的话)
        if event == 550:
            content = payload.get('content', '').strip()
            if content:
                # Get the most recent reply_id from cache (last added)
                # Event 350 should have set this before event 550 arrives
                reply_id = None
                if self.assistant_speech_cache:
                    # Get the most recently added reply_id (Python 3.7+ dicts maintain insertion order)
                    reply_id = list(self.assistant_speech_cache.keys())[-1]
                
                # If no reply_id found, create a default one
                if not reply_id:
                    reply_id = f"{self.session_id}_default"
                    self.assistant_speech_cache[reply_id] = ''
                
                # Accumulate text for the same reply
                if reply_id not in self.assistant_speech_cache:
                    self.assistant_speech_cache[reply_id] = ''
                
                self.assistant_speech_cache[reply_id] += content
                accumulated_text = self.assistant_speech_cache[reply_id]
                
                try:
                    text_msg = {
                        "type": "text_transcription",
                        "speaker": "assistant",
                        "text": content,  # Send incremental text
                        "accumulated_text": accumulated_text,  # Also send accumulated text
                        "is_final": False,  # TTS is always incremental
                        "timestamp": timestamp,
                        "reply_id": reply_id
                    }
                    self.text_output_queue.put_nowait(text_msg)
                    logger.debug(f"[Text] Enqueued assistant text: '{content}' (accumulated: '{accumulated_text}')")
                except Exception as e:
                    logger.warning(f"[Text] Failed to enqueue assistant text: {e}")
        
        # Event 451: User ASR recognition result (用户说的话)
        elif event == 451:
            try:
                results = payload.get('results', [])
                if not results or len(results) == 0:
                    return
                
                # Get the first result (usually there's only one)
                result = results[0]
                text = result.get('text', '').strip()
                is_interim = result.get('is_interim', True)
                is_soft_finished = result.get('is_soft_finished', False)
                
                if not text:
                    return
                
                # Get question_id for deduplication
                question_id = payload.get('question_id', '')
                if not question_id:
                    # Try to get from extra
                    extra = payload.get('extra', {})
                    question_id = extra.get('question_id', '')
                
                # Deduplication: only send if text changed or it's final
                cache_key = question_id or 'default'
                last_text = self.user_speech_cache.get(cache_key, '')
                
                # Send if:
                # 1. Text changed (interim update)
                # 2. It's final result (is_interim=False)
                # 3. It's soft finished (is_soft_finished=True)
                should_send = (
                    text != last_text or 
                    not is_interim or 
                    is_soft_finished
                )
                
                if should_send:
                    # Update cache
                    self.user_speech_cache[cache_key] = text
                    
                    # Determine if this is final
                    is_final = not is_interim or is_soft_finished
                    
                    text_msg = {
                        "type": "text_transcription",
                        "speaker": "user",
                        "text": text,
                        "is_final": is_final,
                        "is_interim": is_interim,
                        "timestamp": timestamp,
                        "question_id": question_id
                    }
                    
                    try:
                        self.text_output_queue.put_nowait(text_msg)
                        logger.debug(f"[Text] Enqueued user text: '{text}' (final={is_final}, interim={is_interim})")
                    except Exception as e:
                        logger.warning(f"[Text] Failed to enqueue user text: {e}")
            except Exception as e:
                logger.warning(f"[Text] Error extracting user text from event 451: {e}")
        
        # Event 350: TTS task start (新的回复开始，清空之前的累积文本)
        elif event == 350:
            reply_id = payload.get('reply_id', '')
            if reply_id:
                # Clear previous accumulated text for this reply
                self.assistant_speech_cache[reply_id] = ''
                logger.debug(f"[Text] TTS task started, reply_id: {reply_id}")
        
        # Event 359: TTS task end (回复结束，发送最终累积文本)
        elif event == 359:
            reply_id = payload.get('reply_id', '')
            if reply_id and reply_id in self.assistant_speech_cache:
                accumulated_text = self.assistant_speech_cache[reply_id]
                if accumulated_text:
                    try:
                        text_msg = {
                            "type": "text_transcription",
                            "speaker": "assistant",
                            "text": "",  # Empty for final message
                            "accumulated_text": accumulated_text,
                            "is_final": True,  # Mark as final
                            "timestamp": int(time.time() * 1000),
                            "reply_id": reply_id
                        }
                        self.text_output_queue.put_nowait(text_msg)
                        logger.debug(f"[Text] Enqueued final assistant text: '{accumulated_text}'")
                    except Exception as e:
                        logger.warning(f"[Text] Failed to enqueue final assistant text: {e}")
                
                # Clean up cache after sending final message
                del self.assistant_speech_cache[reply_id]
        
        # Event 450: User speech start (可选，用于标记用户开始说话)
        elif event == 450:
            question_id = payload.get('question_id', '')
            if question_id:
                # Clear previous text for this question
                self.user_speech_cache[question_id] = ''
                logger.debug(f"[Text] User speech started, question_id: {question_id}")

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
            logger.info(f"Error sending audio to client: {e}")
        finally:
            self.is_running = False
    
    async def send_text_to_client_loop(self):
        """Sends text transcription messages from the output queue to the browser client."""
        try:
            while self.is_running or not self.text_output_queue.empty():
                try:
                    text_msg = await asyncio.wait_for(
                        self.text_output_queue.get(), timeout=1.0)
                    
                    # Send as JSON string
                    json_msg = json.dumps(text_msg, ensure_ascii=False)
                    await self.client_websocket.send(json_msg)
                    
                    speaker = text_msg.get('speaker', 'unknown')
                    text_preview = text_msg.get('text', '')[:50]  # Preview first 50 chars
                    is_final = text_msg.get('is_final', False)
                    logger.debug(f"[Text] Sent {speaker} text to browser: '{text_preview}...' (final={is_final})")
                    
                except asyncio.TimeoutError:
                    continue  # No data, just continue waiting
                except websockets.exceptions.ConnectionClosed:
                    logger.warning("[Text] WebSocket connection closed while sending text")
                    break
                except Exception as e:
                    logger.warning(f"[Text] Error sending text to client: {e}")
                    # Continue processing other messages even if one fails

        except Exception as e:
            logger.warning(f"[Text] Error in text sending loop: {e}")
        finally:
            # Ensure we drain remaining messages before exiting
            while not self.text_output_queue.empty():
                try:
                    self.text_output_queue.get_nowait()
                except:
                    break
    
    async def send_tts_text(
            self, start: bool, end: bool, content: str, 
            is_user_querying: bool = False):
        """Sends a TTS text request via the upstream client."""
        if not self.is_running or not self.client:
            return
        preview = content[:80].replace("\n", " ")
        logger.info(
            f"[DialogSession] send_tts_text: start={start}, end={end}, "
            f"is_user_querying={is_user_querying}, content_preview='{preview}'"
        )
        await self.client.chat_tts_text(is_user_querying=is_user_querying, start=start, end=end, content=content)
    
    async def process_client_audio(self, audio_data: bytes):
        """Processes an audio chunk received from the browser client."""
        if self.is_running and self.client:
            logger.debug(f"[DialogSession] process_client_audio: {len(audio_data)} bytes from browser")
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
            self.send_text_task = asyncio.create_task(self.send_text_to_client_loop())

            logger.info("Session started. Ready for audio and text transcription.")
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
                try:
                    await self.receive_task
                except Exception as e:
                    logger.debug(f"Error waiting for receive_task: {e}")
            
            if hasattr(self, 'send_task'):
                try:
                    await self.send_task
                except Exception as e:
                    logger.debug(f"Error waiting for send_task: {e}")
            
            if hasattr(self, 'send_text_task'):
                try:
                    await self.send_text_task
                except Exception as e:
                    logger.debug(f"Error waiting for send_text_task: {e}")

            # Clean up caches
            self.user_speech_cache.clear()
            self.assistant_speech_cache.clear()

            if self.client:
                await self.client.close()
            logger.info(f"Session {self.session_id} closed.")

        except Exception as e:
            logger.info(f"Error during session cleanup: {e}")