# Travel Agent 记忆管理链路说明

本文按一次用户请求的执行顺序，说明 Travel Agent 中短期记忆、长期记忆、记忆摘要、MemoryQuery Skill、Preference Skill 和 OrchestrationAgent 的关系。

## 1. 结论概览

当前项目的记忆系统不是 Redis/PostgreSQL 实现，而是：

- 短期记忆：Python 内存中的 `ShortTermMemory.messages`
- 长期记忆：JSON 文件 `data/memory/{user_id}.json`
- RAG 知识库：Milvus Lite

记忆使用方式可以概括为：

```text
用户输入
  -> CLI 读取短期记忆 + 长期记忆摘要/偏好/少量相关行程
  -> IntentionAgent 基于这些上下文生成 intents + agent_schedule
  -> OrchestrationAgent 按 agent_schedule 执行各 Skill Agent
  -> Skill Agent 根据需要读取记忆
  -> OrchestrationAgent 把偏好/行程结构化写回长期记忆
  -> CLI 把用户输入和最终聚合结果写入短期记忆 + 长期 chat_history
```

需要注意：

- IntentionAgent 看到的不是完整长期记忆，而是“完整偏好 + 历史摘要 + 少量相关行程 + 短期对话”。
- MemoryQueryAgent 不是每轮都调用，只有 IntentionAgent 判断需要查询历史记忆时才调度。
- 中途各子 Agent 的交流不会逐条写入记忆，但最终 assistant 聚合结果 JSON 会写入 `chat_history`，因此长期聊天历史里会间接看到各 Agent 的输出。

## 2. 记忆组件

### 2.1 ShortTermMemory

文件：`context/short_term_memory.py`

短期记忆只保存当前会话最近对话，存在内存中，不写文件。

每条消息结构：

```python
{
    "role": "user" 或 "assistant",
    "content": "消息内容",
    "timestamp": "时间戳",
    "metadata": {}
}
```

`MemoryManager` 初始化时：

```python
self.short_term = ShortTermMemory(max_turns=10)
```

一轮对话按 2 条消息计算，即用户一条、助手一条，所以最多保留 20 条消息。超过后保留最近消息：

```python
self.messages = self.messages[-max_messages:]
```

短期记忆用途：

- 给下一轮 IntentionAgent 做上下文消歧
- 给 OrchestrationAgent 准备 `recent_dialogue`
- 当前会话结束或 clear 后会清空

### 2.2 LongTermMemory

文件：`context/long_term_memory.py`

长期记忆持久化到：

```text
data/memory/{user_id}.json
```

主要字段：

```json
{
  "user_id": "...",
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

长期记忆存 3 类核心信息：

- `preferences`：用户偏好，如常住地、交通偏好、酒店偏好
- `chat_history`：跨会话完整聊天消息
- `trip_history`：结构化行程记录，如出发地、目的地、日期、目的

当前代码没有真实接入 PostgreSQL 或 Redis。README 里提到的 PostgreSQL/Redis 是生产化架构设计方案。

### 2.3 MemoryManager

文件：`context/memory_manager.py`

`MemoryManager` 是统一入口。最关键的方法是：

```python
def add_message(self, role, content, metadata=None):
    self.short_term.add_message(role, content, metadata)
    self.long_term.add_chat_message(role, content, self.session_id)
```

因此同一条用户/助手消息会同时进入：

- 短期记忆：当前会话内存窗口
- 长期记忆：JSON 文件中的 `chat_history`

`MemoryManager` 还提供长期摘要：

```python
async def get_long_term_summary_async(self, max_messages=50)
```

它会读取：

- 最多 50 条长期聊天历史，并排除当前 session
- 最近 20 条行程历史

然后调用 LLM 总结成一段摘要。这个摘要不会写回 JSON 文件，只是在当前请求中临时作为上下文使用。

## 3. 一轮请求中的记忆流

### 3.1 CLI 初始化阶段

文件：`cli.py`

系统启动时创建：

```python
self.memory_manager = MemoryManager(
    user_id=self.user_id,
    session_id=self.session_id,
    llm_model=self.model
)
```

然后把同一个 `memory_manager` 注入给：

- `LazyAgentRegistry`
- `OrchestrationAgent`

`IntentionAgent` 本身不直接持有 `memory_manager`，它是通过 CLI 传入的 `context_messages` 间接看到记忆。

### 3.2 用户输入后，意图识别前

文件：`cli.py`

用户输入到达后，CLI 先做记忆读取，然后再调用 IntentionAgent。

#### 读取长期上下文

CLI 调用：

```python
long_term_summary = await self._get_long_term_summary(user_input)
```

这个函数会拼出给 IntentionAgent 的长期记忆上下文，包含三部分：

1. 长期偏好，基本完整读取：

```python
prefs = self.memory_manager.long_term.get_preference()
```

输出形式类似：

```text
【用户背景信息】（来自长期记忆，可用于推断缺失信息）
• transportation_preference: 高铁
• hotel_brands: 汉庭, 如家
• home_location: 上海
```

2. 历史聊天和行程摘要：

```python
chat_summary = await self.memory_manager.get_long_term_summary_async(max_messages=50)
```

这里不是把完整 `chat_history` 给 IntentionAgent，而是先让 LLM 总结历史聊天和行程，再给摘要。

3. 少量相关历史行程：

```python
all_trips = self.memory_manager.long_term.get_trip_history(limit=None)
```

然后按当前用户输入中的地点做简单匹配：

```python
if origin in user_input or destination in user_input:
    relevant_trips.append(trip)
```

最终只取：

```python
trips_to_show = relevant_trips[:2] + other_trips[:1]
```

即最多给几条相关/最近行程，不会把完整 `trip_history` 全部塞给 IntentionAgent。

#### 读取短期上下文

CLI 读取最近 5 轮短期对话：

```python
recent_context = self.memory_manager.short_term.get_recent_context(n_turns=5)
```

这些短期消息就是当前会话中最近的 user/assistant 消息。

#### 组装给 IntentionAgent 的消息

CLI 组装：

```python
context_messages = []
if long_term_summary:
    context_messages.append(Msg(name="system", content=long_term_summary, role="system"))
for msg in recent_context:
    context_messages.append(Msg(name=msg["role"], content=msg["content"], role=msg["role"]))
context_messages.append(Msg(name="user", content=user_input, role="user"))
```

所以 IntentionAgent 实际看到：

- system message：长期偏好 + 历史摘要 + 少量相关行程
- user/assistant message：当前会话最近 5 轮短期记忆
- 当前 user message：本轮用户输入

## 4. IntentionAgent 怎么使用记忆

文件：`agents/intention_agent.py`

IntentionAgent 的核心是：

```text
LLM + 固定提示词 + Skill 元数据 + JSON 输出协议 + 解析兜底
```

它不直接读 JSON 记忆文件，而是处理 CLI 传来的 `context_messages`。

### 4.1 区分长期记忆和短期记忆

如果消息角色是 `system`，IntentionAgent 会把它当作系统记忆：

```python
if msg.role == "system":
    self.conversation_history.append(f"[系统记忆]\n{msg.content}")
```

如果是普通 user/assistant 历史消息，则作为对话历史：

```python
role_name = "用户" if msg.role == "user" else "助手"
content = msg.content[:800] if len(msg.content) > 800 else msg.content
self.conversation_history.append(f"{role_name}: {content}")
```

注意：非 system 的短期历史会做 800 字截断；system 里的长期摘要完整保留。

### 4.2 IntentionAgent 输出什么

IntentionAgent 会输出 JSON：

```json
{
  "reasoning": "...",
  "intents": [
    {
      "type": "itinerary_planning",
      "confidence": 0.95,
      "description": "...",
      "reason": "..."
    }
  ],
  "key_entities": {
    "origin": "...",
    "destination": "...",
    "date": "...",
    "duration": "...",
    "other": "..."
  },
  "rewritten_query": "...",
  "agent_schedule": [
    {
      "agent_name": "event_collection",
      "priority": 1,
      "reason": "...",
      "expected_output": "..."
    }
  ]
}
```

所以 IntentionAgent 不只是提取意图，它也生成后续调度计划 `agent_schedule`。

### 4.3 为什么已有长期记忆，还会调 MemoryQueryAgent

意图识别前的长期记忆摘要主要用于：

- 帮 IntentionAgent 做路由判断
- 对省略表达做消歧
- 判断是否需要引用历史

MemoryQueryAgent 的职责不同。它是一个业务 Skill，专门在用户显式询问历史、偏好、上次行程，或当前规划需要具体历史依据时被调度。

例如：

```text
我上次去青岛是什么时候？
我之前去过哪些地方？
还是按上次青岛国信集团那个安排来
我不知道青岛要去几天，你参考我之前的记录
```

这类情况下，IntentionAgent 会在 `agent_schedule` 中加入：

```json
{
  "agent_name": "memory_query",
  "priority": 1
}
```

简单偏好或常住地推断不一定需要调 MemoryQueryAgent，因为偏好已经直接注入到了上下文里。

## 5. 用户输入什么时候写入记忆

文件：`cli.py`

用户输入不是一开始就写入。当前流程是：

1. 先读取记忆，调用 IntentionAgent。
2. 意图识别 JSON 解析成功后，写入用户输入：

```python
self.memory_manager.add_message("user", user_input)
```

这会同时写入：

- 短期记忆 `short_term.messages`
- 长期记忆 `chat_history`

因此，本轮用户输入不会提前影响本轮 IntentionAgent 的历史摘要，但会影响后续轮次。

## 6. OrchestrationAgent 怎么使用记忆

文件：`agents/orchestration_agent.py`

OrchestrationAgent 是调度/编排 Agent。它自己继承 `AgentBase`，但职责不是直接回答问题，而是：

- 解析 IntentionAgent 的 `agent_schedule`
- 按 priority 分组
- 同优先级并行执行
- 不同优先级顺序执行
- 构造传给子 Agent 的上下文
- 聚合子 Agent 结果
- 写回长期记忆
- 必要时调用 LLM 做最终展示融合

### 6.1 准备给子 Agent 的上下文

OrchestrationAgent 会调用 `_prepare_context()`：

```python
context = {
    "reasoning": intention_data.get("reasoning", ""),
    "intents": intention_data.get("intents", []),
    "key_entities": intention_data.get("key_entities", {}),
    "rewritten_query": intention_data.get("rewritten_query", "")
}
```

如果有 `memory_manager`，还会读取：

```python
recent_context = self.memory_manager.short_term.get_recent_context(3)
context["recent_dialogue"] = recent_context

preferences = self.memory_manager.long_term.get_preference()
context["user_preferences"] = preferences
```

所以子 Agent 得到的上下文里包含：

- IntentionAgent 生成的意图、实体、改写 query
- 最近 3 轮短期对话
- 长期偏好

### 6.2 调度执行

OrchestrationAgent 不重新判断是否要调用哪个 Agent。它执行 IntentionAgent 给出的 `agent_schedule`。

同一 priority 的任务并行执行：

```text
Priority 1: memory_query / event_collection / preference / information_query / rag_knowledge
Priority 2: itinerary_planning
```

前序结果会作为 `previous_results` 传给后续 Agent。

## 7. 各 Skill Agent 怎么使用记忆

### 7.1 MemoryQueryAgent

文件：`.claude/skills/memory-query/script/agent.py`

MemoryQueryAgent 是专门的历史记忆查询 Agent。

触发场景：

- 用户问历史行程
- 用户问之前说过什么
- 用户问自己的偏好
- 当前任务需要参考上次安排

它读取：

```python
trip_history = self.memory_manager.long_term.get_trip_history(limit=50)
preferences = self.memory_manager.long_term.get_preference()
chat_summary = await self.memory_manager.get_long_term_summary_async(max_messages=30)
```

注意：

- `trip_history limit=50` 是最近 50 条结构化行程记录，不是 50 轮完整对话。
- 行程会格式化成“出发地 -> 目的地 + 日期 + 目的”。
- 历史聊天不给完整原文，而是调用 LLM 摘要。

它最终用这些信息构造 prompt，让 LLM 回答用户关于历史记忆的问题。

### 7.2 PreferenceAgent

文件：`.claude/skills/preference/script/agent.py`

PreferenceAgent 用来识别用户表达的新偏好。

它会先读取当前已有偏好：

```python
current_preferences = self.memory_manager.long_term.get_preference()
```

然后把当前偏好和用户输入一起给 LLM，让模型输出：

```json
{
  "preferences": [
    {
      "type": "hotel_brands",
      "value": "如家",
      "action": "append"
    }
  ],
  "has_preferences": true
}
```

其中：

- `append`：用户说“还、也、另外、以及”
- `replace`：用户说“搬家到、改成、现在是、换成”

PreferenceAgent 自己不直接写长期记忆。真正写入发生在 OrchestrationAgent 的 `_update_memory()`。

### 7.3 EventCollectionAgent

文件：`.claude/skills/event-collection/script/agent.py`

EventCollectionAgent 主要抽取：

- `origin`
- `destination`
- `start_date`
- `end_date`
- `duration_days`
- `return_location`
- `trip_purpose`

它会使用 OrchestrationAgent 传入的长期偏好 `user_preferences`。

例如，如果用户没说出发地，但偏好里有 `home_location`，它可以推断出发地。

它不直接写长期记忆。行程结构化写入由 OrchestrationAgent 统一完成。

### 7.4 ItineraryPlanningAgent

文件：`.claude/skills/plan-trip/script/agent.py`

ItineraryPlanningAgent 会从上下文中读取 `user_preferences`，并使用前序 Agent 的结果：

- EventCollectionAgent 的事项信息
- MemoryQueryAgent 的历史查询结果
- RAGKnowledgeAgent 的政策信息
- InformationQueryAgent 的实时信息

它不直接写长期记忆。它生成 itinerary 后，OrchestrationAgent 会把行程摘要写入 `trip_history`。

### 7.5 RAGKnowledgeAgent

文件：`.claude/skills/ask-question/script/agent.py`

RAGKnowledgeAgent 不读取用户长期记忆。它使用 Milvus Lite 检索差旅政策/FAQ 文档，再结合 LLM 回答。

它的输出不会单独写入结构化长期记忆。

但因为最终 assistant 聚合结果会写入 `chat_history`，所以 RAG 的回答会间接保存在长期聊天历史中。

### 7.6 InformationQueryAgent

文件：`.claude/skills/query-info/script/agent.py`

InformationQueryAgent 用于天气、搜索、实时信息查询。

它不直接读取长期记忆，也不单独把查询结果写入结构化长期记忆。

但它的结果会随最终聚合 JSON 写入长期 `chat_history`。

## 8. OrchestrationAgent 写回长期记忆

文件：`agents/orchestration_agent.py`

OrchestrationAgent 在所有子 Agent 执行完之后调用 `_update_memory()`。

它只做两类结构化写入：

### 8.1 写入偏好

如果子 Agent 是 `preference`，并且输出了：

```json
{
  "preferences": [
    {"type": "...", "value": "...", "action": "append"}
  ]
}
```

则：

- `append`：读取当前偏好，追加到列表
- `replace`：直接覆盖该类型偏好

写入方法：

```python
self.memory_manager.long_term.save_preference(pref_type, pref_value)
```

### 8.2 写入行程历史

如果子 Agent 是 `itinerary_planning`，并且生成了 itinerary，OrchestrationAgent 会从 EventCollectionAgent 的结果中取：

- `origin`
- `destination`
- `start_date`
- `end_date`
- `trip_purpose`

然后写入：

```python
self.memory_manager.long_term.save_trip_history({
    "origin": origin,
    "destination": destination,
    "start_date": start_date,
    "end_date": end_date,
    "purpose": purpose
})
```

写入前提是有 `destination`。

### 8.3 不会单独写入的内容

以下内容不会作为结构化长期记忆单独写入：

- IntentionAgent 的完整 `reasoning`
- 完整 `agent_schedule`
- RAG 检索结果
- 天气查询结果
- MemoryQueryAgent 的回答
- 每个子 Agent 之间的中间消息

但是最终 assistant 聚合 JSON 会写入 `chat_history`，所以这些内容可能间接出现在长期聊天历史中。

## 9. Assistant 最终结果什么时候写入

文件：`cli.py`

OrchestrationAgent 返回最终聚合结果后，CLI 会展示结果，然后写入：

```python
self.memory_manager.add_message("assistant", json.dumps(result_data, ensure_ascii=False))
```

因此短期和长期 `chat_history` 里的 assistant 内容通常是完整聚合 JSON，而不是单纯自然语言正文。

这个聚合 JSON 通常包含：

```json
{
  "status": "completed",
  "intention": {
    "intents": [...],
    "key_entities": {...}
  },
  "agents_executed": 4,
  "results": [
    {"agent_name": "event_collection", "data": {...}},
    {"agent_name": "memory_query", "data": {...}},
    {"agent_name": "rag_knowledge", "data": {...}},
    {"agent_name": "itinerary_planning", "data": {...}}
  ],
  "final_display": {...}
}
```

这解释了为什么你在长期记忆 `chat_history` 中能看到单个 Agent 的输出：不是它们被单独写入，而是最终聚合结果把它们一起包含了。

## 10. 各阶段吃到的记忆总结

| 阶段 | 是否使用短期记忆 | 是否使用长期记忆 | 使用内容 |
|---|---:|---:|---|
| CLI 意图识别前 | 是 | 是 | 最近 5 轮短期对话；长期偏好；历史聊天/行程摘要；少量相关行程 |
| IntentionAgent | 是 | 是 | 通过 `context_messages` 间接使用，不直接读文件 |
| OrchestrationAgent `_prepare_context` | 是 | 是 | 最近 3 轮短期对话；长期偏好 |
| MemoryQueryAgent | 否/间接 | 是 | 最近 50 条行程；全部偏好；历史聊天摘要 |
| PreferenceAgent | 否/间接 | 是 | 当前已有偏好，用于判断 append/replace |
| EventCollectionAgent | 间接 | 是 | OrchestrationAgent 传入的 `user_preferences` |
| ItineraryPlanningAgent | 间接 | 是 | `user_preferences` 和 previous_results 中的记忆查询结果 |
| RAGKnowledgeAgent | 否 | 否 | 使用 Milvus 知识库，不使用用户记忆 |
| InformationQueryAgent | 否 | 否 | 查询实时信息，不使用用户记忆 |
| OrchestrationAgent `_update_memory` | 否 | 写入 | 偏好写入 `preferences`；行程写入 `trip_history` |
| CLI 结束本轮 | 写入 | 写入 | user 原始输入和 assistant 最终聚合 JSON |

## 11. 面试回答模板

可以这样回答：

> 这个项目的记忆分短期和长期两层。短期记忆是当前会话最近 10 轮 user/assistant 消息，存在内存里；长期记忆是按 user_id 存到 JSON 文件里的偏好、聊天历史、行程历史和统计信息。每轮请求在进入 IntentionAgent 前，CLI 会读取最近 5 轮短期对话，并把长期偏好、历史聊天/行程的 LLM 摘要、少量相关历史行程拼成 system message 传给 IntentionAgent。IntentionAgent 不直接查数据库，它根据这些上下文生成意图和 `agent_schedule`。OrchestrationAgent 再按调度计划执行 Skill Agent，并给子 Agent 传入最近 3 轮对话和长期偏好。只有显式查询历史或需要用历史记录补全任务时，才调 MemoryQueryAgent。最后 OrchestrationAgent 只把偏好和行程做结构化写回，CLI 再把用户输入和最终聚合结果写入短期和长期聊天历史。

如果面试官问“为什么长期记忆里能看到单个 Agent 输出”，可以回答：

> 子 Agent 输出不是单独落库，而是包含在 OrchestrationAgent 的最终聚合 JSON 中。CLI 会把这个最终 JSON 作为 assistant 消息写入 `chat_history`，所以长期聊天历史里会间接看到各子 Agent 的结果。

如果面试官问“MemoryQueryAgent 和意图识别前的长期摘要有什么区别”，可以回答：

> 意图识别前的长期摘要用于路由和上下文消歧；MemoryQueryAgent 是业务 Agent，用于用户显式询问历史、偏好、上次安排，或者当前任务需要具体历史记录作为依据时。前者是压缩上下文，后者是一次真正的记忆查询任务。
