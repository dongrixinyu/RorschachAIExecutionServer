# -*- coding: utf-8 -*-
import uuid
import pyaudio


from prompt.test_prompt import TEST_PROMPT

# 配置信息
ws_connect_config = {
    "base_url": "wss://openspeech.bytedance.com/api/v3/realtime/dialogue",
    "headers": {
        "X-Api-App-ID": "6351583448",
        "X-Api-Access-Key": "JFKHhkd1rzpsm45O-oYpJS3Jz8G9S_MO",
        "X-Api-Resource-Id": "volc.speech.dialog",  # 固定值
        "X-Api-App-Key": "PlgvMymc7f3tQnJ6",  # 固定值
        "X-Api-Connect-Id": str(uuid.uuid4()),
    }
}

start_session_req = {
    "asr": {
        "vad": {
            "silence_duration_ms": 3000,  # 静音持续时间阈值：3秒，给用户充分思考时间，避免AI过早打断
            "speech_threshold": 0.6,      # 语音检测灵敏度：提高阈值，减少环境噪音误判，避免AI过度反应
            "silence_threshold": 0.4      # 静音检测阈值：提高阈值，减少误判，让AI能识别用户正常停顿
        },
        "extra": {
            "end_smooth_window_ms": 2000,  # 平滑窗口：2000ms（范围：500ms-50s），用于调整判断用户停止说话的时间，让VAD更稳定，减少误判
            "enable_custom_vad": True,     # 启用自定义VAD参数：True=使用上方vad字段的自定义参数，False=使用默认VAD参数
            "enable_asr_twopass": False,   # 启用非流式模型识别：True=开启非流式识别能力，False=使用流式识别（默认）
        },
    },
    "tts": {
        "speaker": "zh_female_vv_jupiter_bigtts",
        # "speaker": "S_XXXXXX",  // 指定自定义的复刻音色,需要填下character_manifest
        # "speaker": "ICL_zh_female_aojiaonvyou_tob" // 指定官方复刻音色，不需要填character_manifest
        "interruptible": True,  # 启用可打断模式：用户说话时AI立即停止播报
        "audio_config": {
            "channel": 1,
            "format": "pcm",
            "sample_rate": 24000
        },
    },
    "dialog": {
        "bot_name": "测试官",
        "character_manifest": "你使用活泼灵动的女声，性格开朗，热爱生活。",
        "speaking_style": "你的说话风格简洁明了，语速适中，语调自然。",
        "system_role": TEST_PROMPT.render(),
        "location": {
          "city": "北京",
        },
        "extra": {
            "strict_audit": False,
            "audit_response": "支持客户自定义安全审核回复话术。",
            "recv_timeout": 10,
            "input_mod": "audio"
        }
    }
}

input_audio_config = {
    "chunk": 3200,
    "format": "pcm",
    "channels": 1,
    "sample_rate": 16000,
    "bit_size": pyaudio.paInt16
}
