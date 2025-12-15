# 文本转录功能实现说明

## 概述

在 `audio_manager.py` 中实现了独立的文本发送循环，使 WebSocket 可以同时返回音频流和文本转录。

## 实现方案

### 方案B：独立的文本发送循环

- **优点**：代码清晰，职责分离，易于维护
- **改动范围**：仅修改 `audio_manager.py` 文件
- **向后兼容**：不影响现有音频流功能

## 核心功能

### 1. 文本提取

从字节跳动 API 响应中提取两种类型的文本：

#### 豆包语音文本 (Event 550)
- **事件类型**：`event: 550`
- **数据位置**：`payload_msg['content']`
- **特点**：流式返回，每次一小段文本
- **处理逻辑**：
  - 累积同一 `reply_id` 的文本
  - 每次发送增量文本和累积文本
  - Event 350 标记新回复开始
  - Event 359 标记回复结束，发送最终文本

#### 用户语音文本 (Event 451)
- **事件类型**：`event: 451`
- **数据位置**：`payload_msg['results'][0]['text']`
- **特点**：包含中间结果（`is_interim: True`）和最终结果（`is_interim: False`）
- **处理逻辑**：
  - 使用 `question_id` 进行去重
  - 只在文本变化或最终结果时发送
  - 支持 `is_soft_finished` 标志

### 2. 文本消息格式

发送给浏览器的 JSON 消息格式：

```json
{
  "type": "text_transcription",
  "speaker": "assistant" | "user",
  "text": "当前文本片段",
  "accumulated_text": "累积的完整文本（仅assistant）",
  "is_final": true | false,
  "is_interim": true | false,  // 仅user
  "timestamp": 1234567890,
  "reply_id": "reply_id",      // 仅assistant
  "question_id": "question_id" // 仅user
}
```

### 3. 去重和缓存机制

#### 用户语音缓存
- **Key**: `question_id`
- **Value**: 最后发送的文本
- **目的**: 避免重复发送相同的中间结果

#### 助手语音缓存
- **Key**: `reply_id`
- **Value**: 累积的完整文本
- **目的**: 累积流式文本片段，形成完整回复

### 4. 事件处理流程

```
Event 350 (TTS任务开始)
  ↓
  清空对应 reply_id 的缓存
  ↓
Event 550 (TTS文本片段) × N
  ↓
  累积文本并发送增量消息
  ↓
Event 359 (TTS任务结束)
  ↓
  发送最终累积文本并清理缓存
```

```
Event 450 (用户语音开始)
  ↓
  清空对应 question_id 的缓存
  ↓
Event 451 (ASR识别结果) × N
  ↓
  去重检查 → 发送文本（如果变化或最终）
```

## 代码结构

### 新增组件

1. **文本输出队列** (`text_output_queue`)
   - 存储待发送的文本消息
   - 类型：`asyncio.Queue`

2. **缓存字典**
   - `user_speech_cache`: 用户语音去重缓存
   - `assistant_speech_cache`: 助手语音累积缓存

3. **文本发送循环** (`send_text_to_client_loop`)
   - 独立的异步循环
   - 从队列中取出文本消息并发送
   - 处理连接关闭和错误

4. **文本提取方法** (`_extract_and_queue_text`)
   - 处理不同事件类型的文本提取
   - 实现去重和累积逻辑

## 错误处理

1. **文本提取失败**
   - 记录警告日志，不影响音频流
   - 继续处理后续消息

2. **队列操作失败**
   - 使用 `put_nowait` 和异常处理
   - 避免阻塞主循环

3. **WebSocket 发送失败**
   - 捕获 `ConnectionClosed` 异常
   - 继续处理其他消息
   - 在 finally 中清空队列

4. **资源清理**
   - 关闭时等待所有任务完成
   - 清空缓存和队列
   - 优雅处理任务异常

## 日志记录

- **Debug 级别**：文本提取和发送的详细信息
- **Info 级别**：重要操作和状态变化
- **Warning 级别**：错误和异常情况

日志格式：
- `[Text] Enqueued assistant text: '...'`
- `[Text] Enqueued user text: '...'`
- `[Text] Sent {speaker} text to browser: '...'`

## 性能考虑

1. **异步处理**
   - 文本发送独立循环，不阻塞音频流
   - 使用 `asyncio.wait_for` 设置超时

2. **内存管理**
   - 及时清理已完成的缓存项
   - 队列大小自动管理

3. **网络优化**
   - JSON 消息体积小，不影响音频传输
   - 文本和音频并行发送

## 测试建议

1. **功能测试**
   - 验证豆包语音文本正确提取和发送
   - 验证用户语音文本正确提取和发送
   - 验证去重机制工作正常
   - 验证累积机制工作正常

2. **边界测试**
   - 空文本处理
   - 缺失字段处理
   - 连接断开处理
   - 异常响应处理

3. **性能测试**
   - 高并发场景
   - 长时间运行
   - 内存泄漏检查

## 前端集成

前端需要在 `ttsClient.js` 的 `handleMessage` 方法中处理文本消息：

```javascript
handleMessage(event) {
  // 判断消息类型
  if (typeof event.data === 'string') {
    try {
      const textData = JSON.parse(event.data);
      if (textData.type === 'text_transcription') {
        // 调用字幕管理器显示文本
        if (window.subtitleManager) {
          window.subtitleManager.addText(
            textData.text,
            textData.speaker,
            textData.is_final
          );
        }
      }
    } catch (e) {
      // 非JSON文本，可能是其他文本消息
    }
  } else {
    // 二进制音频数据，按原逻辑处理
    // ...
  }
}
```

## 注意事项

1. **事件顺序**
   - Event 350 应该在 Event 550 之前到达
   - 如果顺序异常，使用 fallback 机制

2. **文本编码**
   - 使用 `ensure_ascii=False` 支持中文
   - JSON 序列化时保持 Unicode

3. **时间戳**
   - 使用毫秒级时间戳
   - 便于前端同步显示

4. **向后兼容**
   - 不影响现有音频流功能
   - 文本消息为可选功能
   - 前端不处理文本消息时不影响使用

## 后续优化建议

1. **配置化**
   - 可配置是否启用文本转录
   - 可配置日志级别

2. **统计信息**
   - 记录文本消息数量
   - 记录发送成功率

3. **重试机制**
   - 文本发送失败时的重试逻辑
   - 队列满时的处理策略

