# Travel Agent 记忆管理系统学习总结

## 1. 总体设计

`context` 文件夹下的记忆系统是一个两层记忆结构：

- `ShortTermMemory`：短期记忆，只保存当前会话最近若干轮对话。
- `LongTermMemory`：长期记忆，把用户偏好、聊天历史、行程历史持久化到 JSON 文件。
- `MemoryManager`：统一入口，把短期记忆和长期记忆组合起来，给 CLI、OrchestrationAgent 和其他 Agent 使用。

核心思想是：

1. 用户和助手每产生一条消息，都先通过 `MemoryManager.add_message()` 写入。
2. 这条消息会同时进入短期记忆和长期记忆。
3. 当前会话推理时主要读取短期记忆。
4. 跨会话个性化时读取长期记忆里的偏好、历史聊天总结和历史行程。
5. 当编排 Agent 得到新的偏好或行程结果时，再回写长期记忆。

## 2. ShortTermMemory：当前会话上下文

文件：`short_term_memory.py`

短期记忆只存在内存里，不写文件。它维护一个 `messages` 列表，每条消息结构大致是：

```python
{
    "role": "user" 或 "assistant",
    "content": "...",
    "timestamp": "...",
    "metadata": {}
}
```

主要方法：

- `add_message(role, content, metadata=None)`：添加一条消息。
- `get_recent_context(n_turns=None)`：取最近 N 轮对话。一轮按 2 条消息计算，即用户一条、助手一条。
- `get_context_string(n_turns=5)`：把最近对话拼成可放入 prompt 的文本。
- `clear()`：清空当前会话记忆。
- `get_statistics()`：返回消息数、最大轮数、最早和最新消息时间。

它的自动淘汰规则很简单：

```python
max_messages = max_turns * 2
if len(self.messages) > max_messages:
    self.messages = self.messages[-max_messages:]
```

默认 `MemoryManager` 初始化它时使用 `max_turns=10`，所以最多保留最近 20 条消息。

## 3. LongTermMemory：跨会话持久记忆

文件：`long_term_memory.py`

长期记忆会写入 JSON 文件，默认路径是：

```text
data/memory/{user_id}.json
```

它的数据结构大致是：

```json
{
  "user_id": "用户ID",
  "created_at": "...",
  "updated_at": "...",
  "preferences": [],
  "chat_history": [],
  "trip_history": [],
  "statistics": {
    "total_trips": 0,
    "total_messages": 0,
    "frequent_destinations": {}
  }
}
```

长期记忆主要管三类信息：

### 3.1 用户偏好

偏好存成列表：

```json
[
  {"type": "budget", "value": "经济型"},
  {"type": "transportation", "value": "优先高铁"}
]
```

相关方法：

- `save_preference(pref_type, value)`：新增或覆盖某个偏好。
- `get_preference(pref_type=None)`：读取某个偏好；不传参数时返回所有偏好的字典形式。
- `add_hotel_brand(brand)`：追加酒店品牌偏好。
- `add_airline(airline)`：追加航空公司偏好。

注意：`_migrate_data()` 会兼容旧格式，例如把旧的 dict 偏好迁移成 list 偏好，并修复嵌套偏好的旧数据。

### 3.2 聊天历史

每次 `MemoryManager.add_message()` 都会调用：

```python
self.long_term.add_chat_message(role, content, self.session_id)
```

长期聊天记录会包含 `session_id`，所以它能区分不同会话。

相关方法：

- `add_chat_message(role, content, session_id=None)`
- `get_chat_history(limit=None, session_id=None)`

### 3.3 行程历史

当行程规划 Agent 产出 itinerary 后，编排器会把目的地、出发地、日期、目的等信息保存到长期记忆。

相关方法：

- `save_trip_history(trip_info)`
- `get_trip_history(limit=10)`
- `get_frequent_destinations(top_n=5)`

`save_trip_history()` 还会更新统计信息：

- `total_trips`
- `frequent_destinations[destination]`

## 4. MemoryManager：统一入口

文件：`memory_manager.py`

`MemoryManager` 是项目里最重要的入口类。初始化时会创建两层记忆：

```python
self.short_term = ShortTermMemory(max_turns=10)
self.long_term = LongTermMemory(user_id, storage_path)
```

### 4.1 写入消息

`add_message()` 同时写两份：

```python
self.short_term.add_message(role, content, metadata)
self.long_term.add_chat_message(role, content, self.session_id)
```

所以：

- 短期记忆用于当前会话上下文。
- 长期记忆用于跨会话历史追踪。

### 4.2 获取完整上下文

`get_full_context()` 返回结构化上下文：

- 短期：最近对话、格式化字符串、统计信息。
- 长期：用户偏好、最近聊天历史、最近行程、高频目的地、统计信息。

适合调试或展示记忆状态。

### 4.3 获取 Agent prompt 上下文

`get_context_for_agent(long_term_summary=None)` 会拼接：

1. 历史会话总结。
2. 用户偏好。
3. 当前会话最近 3 轮对话。

它返回字符串，可直接塞进 Agent prompt。

### 4.4 长期记忆总结

`get_long_term_summary_async(max_messages=50)` 会：

1. 从长期聊天历史里排除当前 session 的消息。
2. 读取最近行程历史。
3. 构造总结 prompt。
4. 调用传入的 `llm_model` 生成长期记忆摘要。

如果没有传 `llm_model`，直接返回空字符串。

同步方法 `get_long_term_summary()` 只是对 async 方法包了一层 `asyncio.run()`；如果已经在异步上下文里调用，它会返回空字符串并提示应该用 async 版本。

## 5. 项目中的数据流

### 5.1 CLI 初始化

`cli.py` 初始化 `MemoryManager`：

```python
self.memory_manager = MemoryManager(
    user_id=self.user_id,
    session_id=self.session_id,
    llm_model=self.model
)
```

然后把同一个 `memory_manager` 传给 lazy agent registry 和 orchestration agent。

### 5.2 用户输入进入系统

用户输入后，CLI 会：

1. 获取长期记忆摘要。
2. 获取短期最近对话。
3. 把这些上下文交给意图识别 Agent。
4. 意图识别后，调用 `memory_manager.add_message("user", user_input)` 保存用户输入。
5. 编排执行完成后，再调用 `memory_manager.add_message("assistant", result_json)` 保存助手结果。

### 5.3 OrchestrationAgent 使用记忆

编排 Agent 会读取：

- `memory_manager.short_term.get_recent_context(3)`：最近对话。
- `memory_manager.long_term.get_preference()`：用户偏好。

当某些 Agent 返回了新的偏好，它会调用：

```python
memory_manager.long_term.save_preference(...)
```

当行程规划 Agent 返回 itinerary，它会调用：

```python
memory_manager.long_term.save_trip_history(...)
```

这就是项目记忆闭环：

```text
用户输入
  -> MemoryManager 写短期 + 长期聊天历史
  -> Agent 读取短期上下文 + 长期偏好/摘要
  -> Agent 产出偏好或行程
  -> OrchestrationAgent 回写长期记忆
  -> 下次会话继续读取
```

## 6. 这三个文件应该怎么学

建议按这个顺序学：

### 第一步：先学 `short_term_memory.py`

目标：理解“当前会话上下文”。

重点看：

- `self.messages` 的结构。
- `add_message()` 如何追加消息。
- `max_turns * 2` 如何限制记忆长度。
- `get_recent_context()` 和 `get_context_string()` 的区别。

你应该能回答：

- 为什么一轮对话等于 2 条消息？
- 短期记忆什么时候会丢弃旧消息？
- 为什么短期记忆不跨会话？

### 第二步：再学 `long_term_memory.py`

目标：理解“持久化用户画像”。

重点看：

- `__init__()` 如何根据 `user_id` 定位 JSON 文件。
- `_load()`、`_save()`、`_init_data()` 如何完成文件持久化。
- `_migrate_data()` 为什么要兼容旧格式。
- `save_preference()` 和 `get_preference()` 如何管理偏好。
- `save_trip_history()` 如何记录行程并更新目的地频次。
- `add_chat_message()` 如何保存跨会话聊天记录。

你应该能回答：

- 长期记忆保存在哪里？
- 偏好为什么用 `type/value` 列表，而不是普通 dict？
- 新开一个 session 后，为什么还能读到旧偏好和旧行程？

### 第三步：最后学 `memory_manager.py`

目标：理解两层记忆如何被统一调度。

重点看：

- `__init__()` 如何组合短期和长期记忆。
- `add_message()` 为什么同时写两层。
- `get_full_context()` 返回什么结构。
- `get_context_for_agent()` 如何拼 prompt 上下文。
- `get_long_term_summary_async()` 如何用 LLM 把长期历史压缩成摘要。

你应该能回答：

- 当前对话和跨会话记忆分别从哪里来？
- 为什么长期聊天历史需要 `session_id`？
- Agent 真正拿到的上下文是怎么拼出来的？

### 第四步：结合调用点读

学完三个文件后，再看：

- `cli.py`：看用户输入和助手输出什么时候写入记忆。
- `agents/orchestration_agent.py`：看偏好和行程什么时候回写长期记忆。
- `tests/test_memory_system.py`：按测试脚本理解每个功能的预期行为。

推荐阅读顺序：

```text
short_term_memory.py
  -> long_term_memory.py
  -> memory_manager.py
  -> tests/test_memory_system.py
  -> cli.py
  -> agents/orchestration_agent.py
```

## 7. 学习时要注意的问题

1. 当前系统不是向量记忆，也没有语义检索。
   长期记忆主要靠 JSON 结构化存储、最近记录截断、LLM 摘要和简单地点匹配。

2. 短期记忆和长期记忆的职责不同。
   短期记忆负责“最近说了什么”，长期记忆负责“这个用户长期是什么样的人、去过哪里、偏好什么”。

3. `MemoryManager.add_message()` 是聊天消息写入的主入口。
   如果绕过它直接写短期或长期，可能导致两层记忆不一致。

4. 行程和偏好不是自动从所有文本里抽取的。
   偏好和行程主要由 OrchestrationAgent 根据下游 Agent 的结构化结果回写。

5. 代码注释存在编码显示问题，但实现逻辑可以直接读 Python 代码判断。

