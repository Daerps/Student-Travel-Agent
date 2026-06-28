#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Auto-label RAG retrieval-intent queries for RAG eval samples.

The script reads LLM credentials from config.LLM_CONFIG, which in turn reads
environment variables or local_secrets.json. It never prints the API key.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from openai import OpenAI


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = PROJECT_ROOT / "tests" / "eval_dataset_100.json"
REVIEW_PATH = PROJECT_ROOT / "tests" / "rag_evidence_review.json"

sys.path.insert(0, str(PROJECT_ROOT))
from config import LLM_CONFIG, SYSTEM_CONFIG  # noqa: E402


PROMPT_TEMPLATE = """请将用户问题改写为一句用于 RAG 知识库检索的语义查询陈述。

要求：
1. 输出陈述句，不要输出疑问句，不要使用“是否、哪些、如何、是什么、多少、吗、？”等问句表达。
2. 不要写成过短标题或关键词，例如“会议费报销制度”“住宿费标准”。
3. 需要围绕用户问题展开 3 到 6 个可能出现在制度文件正文中的语义要点。
4. 可以使用“涉及、包括、关注、对应、需要查询”等表达，但不要使用“XX一般由A、B、C组成”的固定模板。
5. 保留用户问题中的业务场景、费用类型、地点、材料、审批、能否报销、限制条件等信息。
6. 尽量使用学校制度文件表述，如“差旅费管理细则”“会议费管理细则”“因公出国”“住宿费标准”“市内交通费”“综合定额”“报销材料”“审批材料”“不予报销情形”等。
7. 不要编造用户没有提到且无法合理推断的具体结论。
8. 长度控制在 30 到 100 字。

示例：
用户问题：住宿费报销需要准备哪些材料？
输出：住宿费报销材料涉及住宿发票、酒店水单、住宿明细、住宿日期、天数、房间数和金额信息等内容

用户问题：会议费哪些情况不能报销？
输出：会议费不予报销情形涉及超标准支出、超范围支出、与会议无关消费、宴请娱乐和旅游参观等限制

用户问题：个人顺路旅游能放进学校差旅报销里吗？
输出：差旅费报销限制涉及因私顺路旅游、与公务无关支出、个人自理费用和不予报销情形

用户问题：学校会议不安排住宿时，综合定额怎么处理？
输出：会议费综合定额处理涉及不安排住宿时住宿费扣减、伙食费扣减、费用调剂和定额执行规则

用户问题：{query}
输出："""


QUESTION_MARKERS = ("？", "?", "是否", "哪些", "如何", "是什么", "多少", "吗")
BAD_OUTPUT_MARKERS = ("请提供", "用户问题", "需要改写", "好的")


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_rag_samples(dataset: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [item for item in dataset["samples"] if item["gold"]["rag"].get("eval")]


def clean_one_line(text: str) -> str:
    line = " ".join(text.strip().split())
    if line.startswith(("“", '"', "'")) and line.endswith(("”", '"', "'")):
        line = line[1:-1].strip()
    for prefix in ("输出：", "输出:", "检索主题：", "检索主题:"):
        if line.startswith(prefix):
            line = line[len(prefix):].strip()
    return line


def extract_message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(getattr(item, "text", "") or getattr(item, "content", "")))
        return "\n".join(part for part in parts if part)
    reasoning = getattr(message, "reasoning_content", None)
    if isinstance(reasoning, str):
        return reasoning
    return ""


def validate_label(content: str) -> None:
    if not 30 <= len(content) <= 100:
        raise RuntimeError(f"LLM output length {len(content)} is outside 30-100 chars: {content}")
    if any(marker in content for marker in QUESTION_MARKERS):
        raise RuntimeError(f"LLM output still looks like a question: {content}")
    if any(marker in content for marker in BAD_OUTPUT_MARKERS):
        raise RuntimeError(f"LLM output is meta-instruction, not a retrieval topic: {content}")
    if "一般由" in content:
        raise RuntimeError(f"LLM output uses forbidden generic template: {content}")


def build_client() -> OpenAI:
    api_key = LLM_CONFIG.get("api_key")
    if not api_key:
        raise RuntimeError("LLM API key is empty. Set LLM_API_KEY or local_secrets.json llm_api_key.")
    return OpenAI(
        api_key=api_key,
        base_url=LLM_CONFIG.get("base_url", "https://api.deepseek.com"),
        timeout=float(SYSTEM_CONFIG.get("timeout", 60)),
    )


def label_query(client: OpenAI, query: str, temperature: float, max_tokens: int, max_retries: int) -> str:
    prompt = PROMPT_TEMPLATE.format(query=query)
    last_error: Exception | None = None
    previous_bad = ""
    for attempt in range(1, max_retries + 1):
        try:
            user_prompt = prompt
            if last_error is not None:
                user_prompt += (
                    "\n\n上一版不符合要求，请重新输出。必须是 30 到 100 字的语义查询陈述，"
                    "不要疑问句，不要短标题，不要只复述用户问题，不要使用“是否、哪些、如何、是什么、多少、吗、？”等问句表达。"
                )
                if previous_bad:
                    user_prompt += (
                        f"\n上一版：{previous_bad}"
                        "\n请不要回复元话语，直接改成带有 3 到 6 个制度语义要点的检索陈述，"
                        "例如“会议费报销材料涉及会议审批、会议通知、签到表、费用发票和结算明细等内容”。"
                    )
            response = client.chat.completions.create(
                model=LLM_CONFIG.get("model_name", "deepseek-v4-pro"),
                messages=[
                    {
                        "role": "system",
                        "content": "你是学校差旅、会议费、报销制度 RAG 检索查询改写助手。",
                    },
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            message = response.choices[0].message
            content = clean_one_line(extract_message_text(message))
            if not content:
                finish_reason = getattr(response.choices[0], "finish_reason", None)
                raise RuntimeError(f"LLM returned empty content; finish_reason={finish_reason}")
            try:
                validate_label(content)
            except RuntimeError:
                previous_bad = content
                raise
            return content
        except Exception as exc:
            last_error = exc
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"LLM labeling failed after {max_retries} retries: {last_error}")


def build_review_payload(dataset: Dict[str, Any], labels: Dict[str, str], threshold: float) -> Dict[str, Any]:
    items = []
    for sample in get_rag_samples(dataset):
        rag = sample["gold"]["rag"]
        items.append({
            "id": sample["id"],
            "user_id": sample["user_id"],
            "turn_id": sample["turn_id"],
            "query": sample["query"],
            "gold_cluster": rag["gold_cluster"],
            "review_search_query": labels[sample["id"]],
            "similarity_threshold": threshold,
            "top_k": int(rag.get("top_k", 3)),
        })
    return {
        "name": "rag_query_review",
        "purpose": "LLM-generated semantic retrieval statements for the 20 RAG evaluation samples.",
        "notes": [
            "review_search_query 表示面向 RAG 知识库检索的语义查询陈述。",
            "本文件由 tests/auto_label_rag_queries.py 根据 eval_dataset_100.json 中 rag.eval=true 的原始 query 自动生成。",
            "生成规则要求保留业务场景和费用/材料/审批/能否报销等限定信息，展开制度正文中可能出现的语义要点，避免疑问句、短标题和固定泛化模板。",
        ],
        "items": items,
    }


def sync_dataset(dataset: Dict[str, Any], labels: Dict[str, str], threshold: float) -> None:
    for sample in get_rag_samples(dataset):
        rag = sample["gold"]["rag"]
        rag["gold_evidence"] = labels[sample["id"]]
        rag["similarity_threshold"] = threshold
        rag.setdefault("top_k", 3)
    notes = dataset.setdefault("evaluation_notes", {})
    notes["rag_gold_source"] = (
        "当前 gold_evidence 已由 tests/auto_label_rag_queries.py 调用 LLM 基于原始 query 自动生成，"
        f"similarity_threshold={threshold:.2f}。"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-label RAG retrieval topics with LLM.")
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH)
    parser.add_argument("--review-output", type=Path, default=REVIEW_PATH)
    parser.add_argument("--sync-dataset", action="store_true", help="Also overwrite gold_evidence in eval dataset.")
    parser.add_argument("--threshold", type=float, default=0.70)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--max-retries", type=int, default=8)
    parser.add_argument("--limit", type=int, default=None, help="Debug only: label the first N RAG samples.")
    parser.add_argument("--dry-run", action="store_true", help="Print labels without writing files.")
    args = parser.parse_args()

    dataset = load_json(args.dataset)
    rag_samples = get_rag_samples(dataset)
    if args.limit is not None:
        rag_samples = rag_samples[: args.limit]

    client = build_client()
    labels: Dict[str, str] = {}
    for index, sample in enumerate(rag_samples, 1):
        label = label_query(client, sample["query"], args.temperature, args.max_tokens, args.max_retries)
        labels[sample["id"]] = label
        print(f"[{index}/{len(rag_samples)}] {sample['id']}: {label}")

    if args.dry_run:
        print("Dry run enabled; no files were written.")
        return

    if args.limit is None:
        review_payload = build_review_payload(dataset, labels, args.threshold)
    else:
        review_payload = build_review_payload({"samples": rag_samples}, labels, args.threshold)
    write_json(args.review_output, review_payload)
    print(f"Wrote review labels to {args.review_output}")

    if args.sync_dataset:
        sync_dataset(dataset, labels, args.threshold)
        write_json(args.dataset, dataset)
        print(f"Synced labels into {args.dataset}")


if __name__ == "__main__":
    main()
