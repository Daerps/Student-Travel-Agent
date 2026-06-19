"""
协调器智能体 OrchestrationAgent
职责：根据意图识别结果，协调调度多个子智能体完成任务

核心功能：
1. 接收 IntentionAgent 的调度决策
2. 按照优先级顺序执行子智能体
3. 管理智能体之间的消息传递
4. 聚合多个智能体的结果
5. 与三层记忆系统集成

执行模式：
- Sequential (顺序执行): 按优先级依次执行，前一个的输出作为后一个的输入
- Parallel (并行执行): 同时执行多个智能体（暂不实现）
"""
from agentscope.agent import AgentBase
from agentscope.message import Msg
from typing import Optional, Union, List, Dict, Any
import json
import logging
import asyncio
from utils.langsmith_tracing import start_trace

logger = logging.getLogger(__name__)


class OrchestrationAgent(AgentBase):
    """协调器智能体 - 调度和协调多个子智能体"""

    def __init__(
        self,
        name: str = "OrchestrationAgent",
        agent_registry: Dict[str, AgentBase] = None,
        memory_manager = None,
        model = None,
        **kwargs
    ):
        """
        初始化协调器

        Args:
            name: 智能体名称
            agent_registry: 子智能体注册表 {agent_name: agent_instance}
            memory_manager: 记忆管理器
            model: 用于最终结果融合的LLM模型
        """
        super().__init__()
        self.name = name
        self.agent_registry = agent_registry or {}
        self.memory_manager = memory_manager
        self.model = model

    def register_agent(self, agent_name: str, agent: AgentBase):
        """注册子智能体"""
        self.agent_registry[agent_name] = agent
        logger.info(f"Registered agent: {agent_name}")

    def unregister_agent(self, agent_name: str):
        """注销子智能体"""
        if agent_name in self.agent_registry:
            del self.agent_registry[agent_name]
            logger.info(f"Unregistered agent: {agent_name}")

    async def reply(self, x: Optional[Union[Msg, List[Msg]]] = None) -> Msg:
        """
        协调执行流程

        Args:
            x: 输入消息，应包含 IntentionAgent 的输出

        Returns:
            Msg: 执行结果
        """
        if x is None:
            return Msg(
                name=self.name,
                content=json.dumps({"error": "No input provided"}),
                role="assistant"
            )

        # 解析输入
        if isinstance(x, list):
            intention_output = x[-1].content if x else "{}"
        else:
            intention_output = x.content

        # 解析意图识别结果
        try:
            intention_data = json.loads(intention_output) if isinstance(intention_output, str) else intention_output
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse intention output: {e}")
            return Msg(
                name=self.name,
                content=json.dumps({"error": "Invalid intention format"}),
                role="assistant"
            )

        # 获取智能体调度计划
        agent_schedule = intention_data.get("agent_schedule", [])
        if not agent_schedule:
            return Msg(
                name=self.name,
                content=json.dumps({
                    "status": "no_agents",
                    "message": "没有需要调度的智能体"
                }),
                role="assistant"
            )

        # 按优先级排序
        sorted_schedule = sorted(agent_schedule, key=lambda x: x.get("priority", 999))

        logger.info(f"Orchestrating {len(sorted_schedule)} agents")

        # 准备上下文信息
        context = self._prepare_context(intention_data)

        # 并行执行智能体（按优先级分组）
        results = []
        current_priority = None
        parallel_tasks = []

        for task in sorted_schedule:
            priority = task.get("priority", 0)

            # 如果优先级变化，先执行当前批次
            if current_priority is not None and priority != current_priority:
                # 并行执行当前优先级的所有任务
                if parallel_tasks:
                    batch_results = await self._execute_parallel_agents(parallel_tasks, context, results)
                    results.extend(batch_results)
                    parallel_tasks = []

            current_priority = priority
            parallel_tasks.append(task)

        # 执行最后一批
        if parallel_tasks:
            batch_results = await self._execute_parallel_agents(parallel_tasks, context, results)
            results.extend(batch_results)

        # 聚合结果
        final_result = self._aggregate_results(results, intention_data)
        final_display = await self._synthesize_final_display(intention_data, final_result)
        if final_display:
            final_result["final_display"] = final_display

        # 更新记忆
        if self.memory_manager:
            self._update_memory(intention_data, results)

        return Msg(
            name=self.name,
            content=json.dumps(final_result, ensure_ascii=False),
            role="assistant"
        )

    def _prepare_context(self, intention_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        准备上下文信息，供子智能体使用

        Args:
            intention_data: 意图识别结果

        Returns:
            上下文字典
        """
        context = {
            "reasoning": intention_data.get("reasoning", ""),
            "intents": intention_data.get("intents", []),
            "key_entities": intention_data.get("key_entities", {}),
            "rewritten_query": intention_data.get("rewritten_query", "")
        }

        # 从记忆系统获取上下文
        if self.memory_manager:
            # 短期记忆：最近对话
            recent_context = self.memory_manager.short_term.get_recent_context(3)
            context["recent_dialogue"] = recent_context

            # 长期记忆：用户偏好
            preferences = self.memory_manager.long_term.get_preference()
            context["user_preferences"] = preferences

        return context

    async def _execute_parallel_agents(
        self,
        tasks: List[Dict],
        context: Dict[str, Any],
        previous_results: List[Dict]
    ) -> List[Dict]:
        """
        并行执行多个智能体

        Args:
            tasks: 任务列表，每个任务包含 agent_name, priority, reason, expected_output
            context: 上下文信息
            previous_results: 前序智能体的结果

        Returns:
            执行结果列表
        """
        if not tasks:
            return []

        # 如果只有一个任务，直接执行
        if len(tasks) == 1:
            task = tasks[0]
            result = await self._execute_agent(
                agent_name=task.get("agent_name"),
                context=context,
                reason=task.get("reason", ""),
                expected_output=task.get("expected_output", ""),
                previous_results=previous_results
            )
            return [{
                "agent_name": task.get("agent_name"),
                "priority": task.get("priority", 0),
                "result": result
            }]

        # 多个任务并行执行
        logger.info(f"Executing {len(tasks)} agents in parallel")

        # 创建并行任务
        parallel_coroutines = []
        for task in tasks:
            agent_name = task.get("agent_name")
            priority = task.get("priority", 0)
            reason = task.get("reason", "")
            expected_output = task.get("expected_output", "")

            logger.info(f"Parallel executing agent: {agent_name} (priority={priority})")

            # 创建协程
            coroutine = self._execute_agent(
                agent_name=agent_name,
                context=context,
                reason=reason,
                expected_output=expected_output,
                previous_results=previous_results
            )
            parallel_coroutines.append((agent_name, priority, coroutine))

        # 使用 asyncio.gather 并行执行
        execution_results = await asyncio.gather(
            *[coro for _, _, coro in parallel_coroutines],
            return_exceptions=True
        )

        # 整理结果
        results = []
        for (agent_name, priority, _), exec_result in zip(parallel_coroutines, execution_results):
            if isinstance(exec_result, Exception):
                logger.error(f"Parallel agent execution failed: {agent_name}, error: {exec_result}")
                result = {
                    "status": "error",
                    "agent_name": agent_name,
                    "data": {"error": str(exec_result)},
                    "message": f"并行执行失败: {str(exec_result)}"
                }
            else:
                result = exec_result

            results.append({
                "agent_name": agent_name,
                "priority": priority,
                "result": result
            })

        return results

    async def _execute_agent(
        self,
        agent_name: str,
        context: Dict[str, Any],
        reason: str,
        expected_output: str,
        previous_results: List[Dict]
    ) -> Dict[str, Any]:
        """
        执行单个智能体

        Args:
            agent_name: 智能体名称
            context: 上下文信息
            reason: 调用原因
            expected_output: 期望输出
            previous_results: 前序智能体的结果

        Returns:
            执行结果
        """
        agent_trace = start_trace(
            f"agent.{agent_name}",
            inputs={
                "agent_name": agent_name,
                "context": {
                    "intents": context.get("intents", []),
                    "key_entities": context.get("key_entities", {}),
                    "rewritten_query": context.get("rewritten_query", ""),
                },
                "reason": reason,
                "expected_output": expected_output,
                "previous_agents": [item.get("agent_name") for item in previous_results],
            },
            metadata={"agent_name": agent_name},
        )

        # 检查智能体是否注册
        if agent_name not in self.agent_registry:
            logger.warning(f"Agent not registered: {agent_name}")
            agent_trace.end({
                "status": "error",
                "message": f"智能体未注册: {agent_name}",
            })
            return {
                "status": "error",
                "message": f"智能体未注册: {agent_name}"
            }

        agent = self.agent_registry[agent_name]

        # 构建输入消息
        input_msg = Msg(
            name="Orchestrator",
            content=json.dumps({
                "context": context,
                "reason": reason,
                "expected_output": expected_output,
                "previous_results": previous_results
            }, ensure_ascii=False),
            role="user"
        )

        try:
            # 调用智能体
            response = await agent.reply(input_msg)

            # 解析响应
            if isinstance(response.content, str):
                try:
                    result = json.loads(response.content)
                except json.JSONDecodeError:
                    result = {"output": response.content}
            else:
                result = response.content

            # 检查 result 中是否有 error 字段
            # 如果有，说明智能体内部执行失败了
            if isinstance(result, dict) and "error" in result:
                error_msg = result.get("error", "未知错误")
                agent_trace.end({
                    "status": "error",
                    "agent_name": agent_name,
                    "message": error_msg,
                    "output": result,
                })
                return {
                    "status": "error",
                    "agent_name": agent_name,
                    "data": result,
                    "message": error_msg
                }

            output = {
                "status": "success",
                "agent_name": agent_name,
                "data": result
            }
            agent_trace.end(output)
            return output

        except Exception as e:
            logger.error(f"Agent execution failed: {agent_name}, error: {e}")
            agent_trace.end_error(e, outputs={
                "status": "error",
                "agent_name": agent_name,
            })
            # 返回友好的错误信息，但不中断流程
            return {
                "status": "error",
                "agent_name": agent_name,
                "data": {"error": str(e)},
                "message": f"智能体执行失败: {str(e)}"
            }

    def _aggregate_results(
        self,
        results: List[Dict],
        intention_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        聚合多个智能体的结果

        Args:
            results: 所有智能体的执行结果
            intention_data: 原始意图识别结果

        Returns:
            聚合后的最终结果
        """
        aggregated = {
            "status": "completed",
            "intention": {
                "intents": intention_data.get("intents", []),
                "key_entities": intention_data.get("key_entities", {})
            },
            "agents_executed": len(results),
            "results": []
        }

        # 收集每个智能体的结果
        for result in results:
            aggregated["results"].append({
                "agent_name": result["agent_name"],
                "priority": result["priority"],
                "status": result["result"].get("status", "unknown"),
                "data": result["result"].get("data", {})
            })

        # 检查是否有错误
        errors = [r for r in results if r["result"].get("status") == "error"]
        if errors:
            aggregated["status"] = "partial_failure"
            aggregated["errors"] = len(errors)

        return aggregated

    async def _synthesize_final_display(
        self,
        intention_data: Dict[str, Any],
        aggregated: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        使用LLM生成最终展示元数据。

        主行程仍由 CLI 使用 Rich 原生格式展示；LLM 只负责提炼开场、
        补充建议、政策提醒、风险和待确认信息。
        """
        if not self.model:
            return None

        results = aggregated.get("results", [])
        plan_result = next(
            (
                item for item in results
                if item.get("agent_name") == "itinerary_planning"
                and item.get("status") == "success"
            ),
            None
        )
        if not plan_result:
            return None

        supporting_results = [
            item for item in results
            if item.get("agent_name") != "itinerary_planning"
        ]

        synthesis_trace = start_trace(
            "orchestration.final_synthesis",
            inputs={
                "intention": intention_data,
                "plan_agent": self._compact_for_synthesis(plan_result),
                "supporting_agents": self._compact_for_synthesis(supporting_results),
            },
            metadata={
                "agent_name": self.name,
                "supporting_agent_count": len(supporting_results),
            },
        )

        prompt = f"""你是一个旅行/差旅助手的最终回复生成器。

你会收到多个子智能体的输出：
- itinerary_planning：主行程方案，会由程序单独用Rich格式展示，你不要重写主行程。
- rag_knowledge：学校差旅、报销、合规规定，只能提炼成提醒。
- memory_query：用户历史偏好或历史记录，只能提炼成个性化参考。
- information_query：天气、交通、开放时间、来源链接等事实，只能提炼成事实补充。
- event_collection：事项提取和缺失字段，只能提炼成待确认信息。
- preference：用户偏好更新，只能提炼成一句偏好说明。

你的目标：
为Rich终端界面生成展示元数据。不要生成完整行程正文，不要输出Markdown全文。

重要约束：
1. 必须只输出合法JSON。
2. 不要解释你如何融合多个智能体。
3. 不要把各agent的回答逐段粘贴。
4. 不要输出多个版本的方案。
5. 不要重写 itinerary_planning 的 daily_plans、车次、时间线和景点顺序。
6. 不要新增与主行程冲突的车次、景点顺序、酒店区域或日期。
7. 其他agent的内容只提炼为补充建议、合规提醒、风险提示、缺失信息提醒。
8. 如果辅助信息和主行程冲突，以主行程为准；如果涉及学校制度，以RAG制度提醒为准。
9. 如果信息不足，不要阻止回答，但要自然提醒用户确认。
10. 每个列表项都要短、具体、可执行。

输出JSON格式：
{{
  "opening": "1-2句话概括本次规划依据和核心安排，不要超过120字。",
  "sections": [
    {{
      "title": "补充提醒",
      "items": ["结合辅助agent提炼出的出行建议，最多4条"]
    }},
    {{
      "title": "差旅与报销提醒",
      "items": ["学校制度、票据、标准、不可报销事项等，最多5条"]
    }},
    {{
      "title": "需要确认",
      "items": ["仍需用户确认的信息，最多4条"]
    }}
  ],
  "closing": "一句自然收尾，可为空。"
}}

【用户意图】
{json.dumps(self._compact_for_synthesis(intention_data), ensure_ascii=False, indent=2)}

【主行程 itinerary_planning】
{json.dumps(self._compact_for_synthesis(plan_result), ensure_ascii=False, indent=2)}

【辅助agent结果】
{json.dumps(self._compact_for_synthesis(supporting_results), ensure_ascii=False, indent=2)}

请严格输出JSON，不要输出Markdown，不要输出代码块。"""

        try:
            response = await self.model([
                {
                    "role": "system",
                    "content": "你是一个严格的多智能体结果提炼器，只输出合法JSON。"
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ])
            final_text = await self._extract_model_text(response)
            final_text = self._clean_json_text(final_text)

            if not final_text:
                synthesis_trace.end({
                    "status": "empty_output",
                    "final_display": {},
                })
                return None

            try:
                final_display = json.loads(final_text)
            except json.JSONDecodeError as parse_error:
                synthesis_trace.end_error(parse_error, outputs={
                    "status": "invalid_json",
                    "raw_output": final_text,
                })
                return None

            synthesis_trace.end({
                "status": "success",
                "final_display": final_display,
            })
            return final_display
        except Exception as e:
            logger.warning(f"Final synthesis failed: {e}")
            synthesis_trace.end_error(e, outputs={"status": "error"})
            return None

    async def _extract_model_text(self, response: Any) -> str:
        """提取AgentScope模型的文本输出，兼容异步生成器。"""
        text = ""
        if hasattr(response, "__aiter__"):
            async for chunk in response:
                if isinstance(chunk, str):
                    text = chunk
                elif hasattr(chunk, "content"):
                    if isinstance(chunk.content, str):
                        text = chunk.content
                    elif isinstance(chunk.content, list):
                        for item in chunk.content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                text = item.get("text", "")
        elif hasattr(response, "text"):
            text = response.text
        elif hasattr(response, "content"):
            text = response.content
        elif isinstance(response, dict) and "content" in response:
            text = response["content"]
        else:
            text = str(response) if response else ""
        return text or ""

    def _clean_json_text(self, text: str) -> str:
        """清理模型可能包裹的JSON代码块。"""
        text = (text or "").strip()
        if text.startswith("```json"):
            text = text[len("```json"):]
        elif text.startswith("```"):
            text = text[3:]

        if text.endswith("```"):
            text = text[:-3]

        return text.strip()

    def _clean_markdown_text(self, text: str) -> str:
        """清理模型可能包裹的markdown代码块。"""
        text = (text or "").strip()
        if text.startswith("```markdown"):
            text = text[len("```markdown"):]
        elif text.startswith("```md"):
            text = text[len("```md"):]
        elif text.startswith("```"):
            text = text[3:]

        if text.endswith("```"):
            text = text[:-3]

        return text.strip()

    def _compact_for_synthesis(self, value: Any, max_chars: int = 2500, depth: int = 0) -> Any:
        """压缩传给最终融合LLM的上下文，避免辅助结果过长。"""
        if depth > 5:
            return "<max-depth>"

        if isinstance(value, dict):
            compacted = {}
            for key, item in value.items():
                key_str = str(key)
                # retrieved_documents 往往很长，保留元数据和前段内容即可。
                compacted[key_str] = self._compact_for_synthesis(
                    item,
                    max_chars=1200 if key_str in {"content", "answer", "summary"} else max_chars,
                    depth=depth + 1,
                )
            return compacted

        if isinstance(value, list):
            limited = value[:8]
            output = [
                self._compact_for_synthesis(item, max_chars=max_chars, depth=depth + 1)
                for item in limited
            ]
            if len(value) > len(limited):
                output.append(f"<truncated {len(value) - len(limited)} items>")
            return output

        if isinstance(value, str):
            if len(value) > max_chars:
                return value[:max_chars] + f"... <truncated {len(value) - max_chars} chars>"
            return value

        if isinstance(value, (int, float, bool)) or value is None:
            return value

        return self._compact_for_synthesis(str(value), max_chars=max_chars, depth=depth + 1)

    def _update_memory(self, intention_data: Dict[str, Any], results: List[Dict]):
        """
        更新记忆系统

        Args:
            intention_data: 意图识别结果
            results: 智能体执行结果
        """
        if not self.memory_manager:
            return

        # 提取并保存信息到长期记忆
        for result in results:
            agent_name = result["agent_name"]
            data = result["result"].get("data", {})

            # 如果是偏好智能体，保存偏好信息到长期记忆
            if agent_name == "preference" and isinstance(data, dict):
                preferences_data = data.get("preferences", {})

                # 新格式：preferences 是列表，包含 {type, value, action}
                if isinstance(preferences_data, list):
                    for pref_item in preferences_data:
                        if not isinstance(pref_item, dict):
                            continue

                        pref_type = pref_item.get("type")
                        pref_value = pref_item.get("value")
                        pref_action = pref_item.get("action", "replace")  # 默认覆盖

                        if not pref_type or not pref_value:
                            continue

                        # 根据 action 决定操作
                        if pref_action == "append":
                            # 追加模式：获取现有值并追加
                            current_prefs = self.memory_manager.long_term.get_preference()
                            existing_value = current_prefs.get(pref_type)

                            # 如果现有值是列表，追加
                            if isinstance(existing_value, list):
                                if pref_value not in existing_value:
                                    existing_value.append(pref_value)
                                self.memory_manager.long_term.save_preference(pref_type, existing_value)
                                logger.info(f"Appended to {pref_type}: {pref_value}, total: {existing_value}")
                            else:
                                # 如果现有值不是列表，创建新列表
                                new_list = [existing_value, pref_value] if existing_value else [pref_value]
                                self.memory_manager.long_term.save_preference(pref_type, new_list)
                                logger.info(f"Created list for {pref_type}: {new_list}")
                        else:
                            # 覆盖模式：直接保存新值
                            self.memory_manager.long_term.save_preference(pref_type, pref_value)
                            logger.info(f"Replaced {pref_type}: {pref_value}")

                # 旧格式兼容：preferences 是字典
                elif isinstance(preferences_data, dict):
                    for pref_type, value in preferences_data.items():
                        if value and pref_type != "has_preferences" and pref_type != "error":
                            self.memory_manager.long_term.save_preference(pref_type, value)
                            logger.info(f"Updated {pref_type}: {value} (legacy format)")

            # 如果是行程规划智能体，保存行程到长期记忆
            if agent_name == "itinerary_planning" and isinstance(data, dict):
                itinerary = data.get("itinerary", {})

                # 只要有行程信息就保存（不管是否完全规划好）
                if itinerary:
                    # 提取事项收集的信息（出发地、目的地等）
                    event_data = {}
                    for r in results:
                        if r["agent_name"] == "event_collection":
                            event_data = r["result"].get("data", {})
                            break

                    # 从 event_data 获取行程信息
                    origin = event_data.get("origin")
                    destination = event_data.get("destination")
                    start_date = event_data.get("start_date")
                    end_date = event_data.get("end_date")
                    purpose = event_data.get("trip_purpose", "旅游")

                    # 保存到长期记忆（只要有目的地就保存）
                    if destination:
                        self.memory_manager.long_term.save_trip_history({
                            "origin": origin,
                            "destination": destination,
                            "start_date": start_date,
                            "end_date": end_date,
                            "purpose": purpose
                        })
                        logger.info(f"Saved trip to long-term memory: {origin} -> {destination}")

        logger.info("Memory updated after orchestration")
