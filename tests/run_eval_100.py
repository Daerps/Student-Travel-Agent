#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Run the 100-turn Travel Agent evaluation dataset.

Outputs:
- tests/results/eval_100_latest.json
- tests/results/eval_100_latest.md

The script intentionally keeps the dataset schema simple and computes:
- intent success rate over all samples
- preference trigger rate over preference samples
- RAG Cluster/Evidence/Strict Hit@K over RAG samples
- Redis cache hit rates for short-term memory, preferences, and summaries
"""
from __future__ import annotations

import asyncio
import argparse
import importlib.util
import json
import os
import sys
import time
import traceback
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = PROJECT_ROOT / "tests" / "eval_dataset_100.json"
RAG_REVIEW_PATH = PROJECT_ROOT / "tests" / "rag_evidence_review.json"
RESULTS_DIR = PROJECT_ROOT / "tests" / "results"
RESULT_JSON = RESULTS_DIR / "eval_100_latest.json"
RESULT_MD = RESULTS_DIR / "eval_100_latest.md"

os.environ.setdefault("LANGSMITH_TRACING", "false")
sys.path.insert(0, str(PROJECT_ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


DOC_CLUSTER_BY_PARENT_DOC = {
    "tongji_travel_expense_management_rules_2024_revised_2026.md": 1,
    "tongji_research_travel_lodging_package_and_uplift_rules_2021.md": 1,
    "03_transport_and_hotel_booking_playbook.txt": 1,
    "01_trip_planning_checklist.txt": 1,
    "同济大学报销手册_2026年3月.md": 2,
    "财务报销培训.md": 2,
    "02_reimbursement_materials_workflow.txt": 2,
    "06_tongji_reimbursement_system_workflow.txt": 2,
    "04_business_travel_faq_tongji.txt": 2,
    "tongji_conference_expense_management_rules_2024.md": 3,
    "同济大学因公临时出国经费管理办法.md": 4,
    "tongji_overseas_lodging_meals_misc_standards_2019.md": 4,
    "tongji_overseas_travel_foreign_affairs_approval_items_2016.md": 4,
    "tongji_eight_point_rules_financial_prohibitions_80_items_2024.md": 5,
    "08_green_and_cost_saving_travel_tips.txt": 5,
    "05_emergency_handling_playbook.txt": 6,
    "07_city_travel_tips_for_conference.txt": 6,
}


def pct(num: int, den: int) -> str:
    return f"{num / den * 100:.1f}%" if den else "N/A"


def has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def load_dataset() -> Dict[str, Any]:
    with DATASET_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_rag_review_dataset() -> Dict[str, Any]:
    with RAG_REVIEW_PATH.open("r", encoding="utf-8") as f:
        review = json.load(f)

    samples = []
    for item in review["items"]:
        samples.append({
            "id": item["id"],
            "user_id": item["user_id"],
            "turn_id": item["turn_id"],
            "query": item["query"],
            "gold": {
                "required_agents": [5],
                "preference": {"eval": False},
                "rag": {
                    "eval": True,
                    "gold_cluster": item["gold_cluster"],
                    "gold_evidence": item["review_search_query"],
                    "similarity_threshold": item.get("similarity_threshold", 0.65),
                    "top_k": item.get("top_k", 3),
                },
            },
        })

    return {
        "name": review.get("name", "rag_query_review"),
        "source": str(RAG_REVIEW_PATH),
        "samples": samples,
    }


def canonical_agent_mapping(dataset: Dict[str, Any]) -> Dict[int, str]:
    return {int(k): v for k, v in dataset["agent_mapping"].items()}


def gold_agents(sample: Dict[str, Any], mapping: Dict[int, str]) -> List[str]:
    return [mapping[int(item)] for item in sample["gold"]["required_agents"]]


def extract_scheduled_agents(intention_data: Dict[str, Any]) -> List[str]:
    agents = []
    for item in intention_data.get("agent_schedule", []):
        if isinstance(item, dict) and item.get("agent_name"):
            agents.append(str(item["agent_name"]))
    return agents


async def build_long_term_context(memory_manager, user_input: str) -> str:
    """Build a compact long-term context similar to Student_CLI._get_long_term_summary."""
    parts: List[str] = []

    prefs = memory_manager.long_term.get_preference()
    if prefs:
        pref_lines = ["【用户背景信息】（来自长期记忆，可用于推断缺失信息）"]
        for key, value in prefs.items():
            if value:
                if isinstance(value, list):
                    pref_lines.append(f"- {key}: {', '.join(str(item) for item in value)}")
                else:
                    pref_lines.append(f"- {key}: {value}")
        if len(pref_lines) > 1:
            parts.extend(pref_lines)

    chat_summary = await memory_manager.get_long_term_summary_async(max_messages=50)
    if chat_summary:
        parts.append("\n【历史会话总结】")
        parts.append(chat_summary)

    trips = memory_manager.long_term.get_trip_history(limit=None)
    if trips:
        relevant = []
        other = []
        for trip in trips:
            origin = trip.get("origin", "") or ""
            destination = trip.get("destination", "") or ""
            if (origin and origin in user_input) or (destination and destination in user_input):
                relevant.append(trip)
            else:
                other.append(trip)
        trips_to_show = relevant[:2] + other[:1]
        if trips_to_show:
            parts.append("\n【历史行程】")
            for trip in trips_to_show:
                parts.append(
                    f"- {trip.get('origin', '未知')} -> {trip.get('destination', '未知')} "
                    f"({trip.get('start_date', '')} 至 {trip.get('end_date', '')}) "
                    f"{trip.get('purpose', '')}"
                )

    return "\n".join(parts) if parts else ""


@dataclass
class CacheStats:
    reads: Counter = field(default_factory=Counter)
    hits: Counter = field(default_factory=Counter)

    def record(self, name: str, hit: bool) -> None:
        self.reads[name] += 1
        if hit:
            self.hits[name] += 1

    def as_dict(self) -> Dict[str, Any]:
        names = sorted(set(self.reads) | set(self.hits))
        by_type = {}
        total_reads = 0
        total_hits = 0
        for name in names:
            reads = self.reads[name]
            hits = self.hits[name]
            total_reads += reads
            total_hits += hits
            by_type[name] = {
                "hits": hits,
                "reads": reads,
                "hit_rate": pct(hits, reads),
            }
        return {
            "by_type": by_type,
            "overall": {
                "hits": total_hits,
                "reads": total_reads,
                "hit_rate": pct(total_hits, total_reads),
            },
        }


CACHE_STATS = CacheStats()


def install_cache_instrumentation() -> None:
    """Patch cache-facing methods for hit/miss accounting without editing app code."""
    try:
        from context.long_term_memory import LongTermMemory
        from context.short_term_memory import ShortTermMemory
        import context.memory_manager as memory_manager_module
    except Exception:
        return

    original_pref = LongTermMemory._get_cached_preferences

    def counted_get_cached_preferences(self):
        cached = original_pref(self)
        if self.redis_client is not None:
            CACHE_STATS.record("preferences", isinstance(cached, dict))
        return cached

    LongTermMemory._get_cached_preferences = counted_get_cached_preferences

    original_recent = ShortTermMemory.get_recent_context

    def counted_get_recent_context(self, n_turns=None):
        result = original_recent(self, n_turns=n_turns)
        if self.redis_client is not None and self.redis_key:
            CACHE_STATS.record("short_term", bool(result))
        return result

    ShortTermMemory.get_recent_context = counted_get_recent_context

    original_mm_get_json = memory_manager_module.get_json

    def counted_get_json(client, key, default=None):
        result = original_mm_get_json(client, key, default=default)
        if client is not None and ":summary:" in str(key):
            CACHE_STATS.record("summary", isinstance(result, str))
        return result

    memory_manager_module.get_json = counted_get_json


def load_rag_agent_class():
    module_path = PROJECT_ROOT / ".claude" / "skills" / "ask-question" / "script" / "agent.py"
    spec = importlib.util.spec_from_file_location("eval_rag_agent_module", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load RAG agent from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_rag_agent_module"] = module
    spec.loader.exec_module(module)
    return module.RAGKnowledgeAgent


class EvidenceScorer:
    def __init__(self):
        if not has_module("sentence_transformers"):
            raise RuntimeError("sentence_transformers is not installed")
        import numpy as np
        from sentence_transformers import SentenceTransformer

        self.np = np
        model_path = PROJECT_ROOT / "data" / "models" / "bge-small-zh-v1.5"
        self.model = SentenceTransformer(str(model_path))

    def similarities(self, texts: List[str], gold_evidence: str) -> List[float]:
        if not texts:
            return []
        embeddings = self.model.encode(
            [gold_evidence, *texts],
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        gold = embeddings[0]
        chunks = embeddings[1:]
        return [float(item) for item in (chunks @ gold)]


def cluster_for_doc(parent_doc: str) -> Optional[int]:
    return DOC_CLUSTER_BY_PARENT_DOC.get(parent_doc)


def normalize_gold_clusters(value: Any) -> set[int]:
    if isinstance(value, list):
        return {int(item) for item in value}
    return {int(value)}


async def run_agent_eval(dataset: Dict[str, Any]) -> Dict[str, Any]:
    required = ["agentscope", "rich"]
    missing = [name for name in required if not has_module(name)]
    if missing:
        return {
            "status": "skipped",
            "reason": f"Missing dependencies: {', '.join(missing)}",
            "case_results": [],
        }

    from agentscope.message import Msg
    from agentscope.model import OpenAIChatModel
    from agents.intention_agent import IntentionAgent
    from config import LLM_CONFIG, SYSTEM_CONFIG
    from config_agentscope import init_agentscope
    from context.memory_manager import MemoryManager

    install_cache_instrumentation()
    init_agentscope()

    timeout_sec = SYSTEM_CONFIG.get("timeout", 60)
    model = OpenAIChatModel(
        model_name=LLM_CONFIG["model_name"],
        api_key=LLM_CONFIG["api_key"],
        client_kwargs={
            "base_url": LLM_CONFIG["base_url"],
            "timeout": float(timeout_sec),
        },
        temperature=LLM_CONFIG.get("temperature", 0.7),
        max_tokens=LLM_CONFIG.get("max_tokens", 2000),
    )

    mapping = canonical_agent_mapping(dataset)
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for sample in dataset["samples"]:
        grouped[sample["user_id"]].append(sample)

    user_state = {}
    for user_id in grouped:
        session_id = f"eval_{user_id}"
        memory_manager = MemoryManager(user_id=user_id, session_id=session_id, llm_model=model)
        intention_agent = IntentionAgent(name="IntentionAgent", model=model)
        user_state[user_id] = {
            "session_id": session_id,
            "memory_manager": memory_manager,
            "intention_agent": intention_agent,
        }

    case_results = []
    intent_success = 0
    pref_total = 0
    pref_success = 0

    for user_id, samples in sorted(grouped.items()):
        state = user_state[user_id]
        memory_manager = state["memory_manager"]
        intention_agent = state["intention_agent"]

        for sample in sorted(samples, key=lambda item: int(item["turn_id"])):
            started = time.perf_counter()
            query = sample["query"]
            gold = gold_agents(sample, mapping)
            result = {
                "id": sample["id"],
                "user_id": user_id,
                "turn_id": sample["turn_id"],
                "query": query,
                "gold_agents": gold,
                "pred_agents": [],
                "intent_success": False,
                "preference_eval": sample["gold"]["preference"]["eval"],
                "preference_success": None,
                "error": None,
                "latency_sec": None,
            }

            try:
                context_messages = []
                long_term_summary = await build_long_term_context(memory_manager, query)
                if long_term_summary:
                    context_messages.append(Msg(name="system", content=long_term_summary, role="system"))
                for msg in memory_manager.short_term.get_recent_context(n_turns=5):
                    context_messages.append(Msg(name=msg["role"], content=msg["content"], role=msg["role"]))
                context_messages.append(Msg(name="user", content=query, role="user"))

                intention_msg = await intention_agent.reply(context_messages)
                intention_data = json.loads(intention_msg.content)
                pred_agents = extract_scheduled_agents(intention_data)
                result["pred_agents"] = pred_agents
                result["intent_success"] = set(gold).issubset(set(pred_agents))
                intent_success += int(result["intent_success"])

                memory_manager.add_message("user", query)
                memory_manager.add_message("assistant", json.dumps({
                    "intention_agents": pred_agents,
                    "intention": {
                        "intents": intention_data.get("intents", []),
                        "key_entities": intention_data.get("key_entities", {}),
                    },
                }, ensure_ascii=False))

                if result["preference_eval"]:
                    pref_total += 1
                    preference_called = "preference" in pred_agents
                    result["preference_success"] = preference_called
                    pref_success += int(preference_called)

            except Exception as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"
                result["traceback"] = traceback.format_exc(limit=3)

            result["latency_sec"] = round(time.perf_counter() - started, 3)
            case_results.append(result)

    total = len(dataset["samples"])
    return {
        "status": "completed",
        "mode": "intention_only_no_orchestration",
        "intent": {
            "success": intent_success,
            "total": total,
            "success_rate": pct(intent_success, total),
            "failed_cases": [
                item for item in case_results
                if not item["intent_success"] or item.get("error")
            ],
        },
        "preference": {
            "success": pref_success,
            "total": pref_total,
            "success_rate": pct(pref_success, pref_total),
            "failed_cases": [
                item for item in case_results
                if item["preference_eval"] and not item["preference_success"]
            ],
        },
        "cache": CACHE_STATS.as_dict(),
        "case_results": case_results,
    }


def run_rag_eval(dataset: Dict[str, Any]) -> Dict[str, Any]:
    missing = [
        name for name in ["pymilvus", "sentence_transformers", "jieba"]
        if not has_module(name)
    ]
    if missing:
        return {
            "status": "skipped",
            "reason": f"Missing dependencies: {', '.join(missing)}",
            "case_results": [],
        }

    RAGKnowledgeAgent = load_rag_agent_class()
    rag_agent = RAGKnowledgeAgent(
        name="RAGKnowledgeAgent",
        model=None,
        knowledge_base_path=str(PROJECT_ROOT / ".claude" / "skills" / "ask-question" / "data" / "rag_knowledge"),
        collection_name="business_travel_knowledge",
        top_k=3,
    )
    scorer = EvidenceScorer()

    rag_samples = [item for item in dataset["samples"] if item["gold"]["rag"]["eval"]]
    results = []
    cluster_hit = 0
    evidence_hit = 0
    strict_hit = 0

    for sample in rag_samples:
        rag_gold = sample["gold"]["rag"]
        top_k = int(rag_gold.get("top_k", 3))
        threshold = float(rag_gold.get("similarity_threshold", 0.65))
        gold_clusters = normalize_gold_clusters(rag_gold["gold_cluster"])
        retrieved = rag_agent.search_knowledge(sample["query"], top_k=top_k)
        texts = [doc.get("content", "") for doc in retrieved]
        sims = scorer.similarities(texts, rag_gold["gold_evidence"])

        docs = []
        sample_cluster_hit = False
        sample_evidence_hit = False
        sample_strict_hit = False
        max_similarity = None
        best_doc = None

        for rank, (doc, sim) in enumerate(zip(retrieved, sims), 1):
            metadata = doc.get("metadata", {}) or {}
            parent_doc = metadata.get("parent_doc") or Path(str(metadata.get("file_path", ""))).name
            cluster = cluster_for_doc(parent_doc)
            is_cluster = cluster in gold_clusters
            is_evidence = sim >= threshold
            is_strict = is_cluster and is_evidence
            sample_cluster_hit = sample_cluster_hit or is_cluster
            sample_evidence_hit = sample_evidence_hit or is_evidence
            sample_strict_hit = sample_strict_hit or is_strict
            if max_similarity is None or sim > max_similarity:
                max_similarity = sim
                best_doc = {
                    "rank": rank,
                    "parent_doc": parent_doc,
                    "chunk_index": metadata.get("chunk_index"),
                    "similarity": round(sim, 4),
                }
            docs.append({
                "rank": rank,
                "parent_doc": parent_doc,
                "cluster": cluster,
                "cluster_hit": is_cluster,
                "evidence_hit": is_evidence,
                "strict_hit": is_strict,
                "similarity": round(sim, 4),
                "chunk_index": metadata.get("chunk_index"),
                "title": metadata.get("title"),
                "distance": doc.get("distance"),
                "bm25_score": doc.get("bm25_score"),
                "rrf_score": doc.get("rrf_score"),
                "retrieval_sources": doc.get("retrieval_sources", []),
                "preview": doc.get("content", "")[:240],
            })

        cluster_hit += int(sample_cluster_hit)
        evidence_hit += int(sample_evidence_hit)
        strict_hit += int(sample_strict_hit)
        results.append({
            "id": sample["id"],
            "user_id": sample["user_id"],
            "turn_id": sample["turn_id"],
            "query": sample["query"],
            "gold_cluster": rag_gold["gold_cluster"],
            "gold_evidence": rag_gold["gold_evidence"],
            "threshold": threshold,
            "top_k": top_k,
            "cluster_hit": sample_cluster_hit,
            "evidence_hit": sample_evidence_hit,
            "strict_hit": sample_strict_hit,
            "max_similarity": round(max_similarity, 4) if max_similarity is not None else None,
            "best_doc": best_doc,
            "retrieved_documents": docs,
        })

    total = len(rag_samples)
    return {
        "status": "completed",
        "cluster_hit": {
            "success": cluster_hit,
            "total": total,
            "hit_rate": pct(cluster_hit, total),
        },
        "evidence_hit": {
            "success": evidence_hit,
            "total": total,
            "hit_rate": pct(evidence_hit, total),
        },
        "strict_hit": {
            "success": strict_hit,
            "total": total,
            "hit_rate": pct(strict_hit, total),
        },
        "failed_cases": [
            item for item in results
            if not item["strict_hit"]
        ],
        "case_results": results,
    }


def write_markdown(report: Dict[str, Any]) -> str:
    agent_eval = report["agent_eval"]
    rag_eval = report["rag_eval"]
    lines = [
        "# Travel Agent 100-Turn Evaluation",
        "",
        f"- Generated at: {report['generated_at']}",
        f"- Dataset: `{report['dataset']}`",
        f"- RAG dataset: `{report.get('rag_dataset', report['dataset'])}`",
        f"- RAG gold field: `{report.get('rag_gold_field', 'gold_evidence')}`",
        "",
        "## Summary",
        "",
    ]

    if agent_eval["status"] == "completed":
        lines.extend([
            f"- Intent success rate: {agent_eval['intent']['success']}/{agent_eval['intent']['total']} = {agent_eval['intent']['success_rate']}",
            f"- Preference trigger rate: {agent_eval['preference']['success']}/{agent_eval['preference']['total']} = {agent_eval['preference']['success_rate']}",
            f"- Overall cache hit rate: {agent_eval['cache']['overall']['hits']}/{agent_eval['cache']['overall']['reads']} = {agent_eval['cache']['overall']['hit_rate']}",
        ])
        for name, item in agent_eval["cache"]["by_type"].items():
            lines.append(f"  - {name}: {item['hits']}/{item['reads']} = {item['hit_rate']}")
    else:
        lines.append(f"- Agent eval skipped: {agent_eval.get('reason')}")

    if rag_eval["status"] == "completed":
        lines.extend([
            f"- RAG Cluster Hit@K: {rag_eval['cluster_hit']['success']}/{rag_eval['cluster_hit']['total']} = {rag_eval['cluster_hit']['hit_rate']}",
            f"- RAG Evidence Hit@K: {rag_eval['evidence_hit']['success']}/{rag_eval['evidence_hit']['total']} = {rag_eval['evidence_hit']['hit_rate']}",
            f"- RAG Strict Hit@K: {rag_eval['strict_hit']['success']}/{rag_eval['strict_hit']['total']} = {rag_eval['strict_hit']['hit_rate']}",
        ])
    else:
        lines.append(f"- RAG eval skipped: {rag_eval.get('reason')}")

    lines.extend(["", "## Failed Intent Cases", ""])
    if agent_eval["status"] == "completed":
        failed = agent_eval["intent"]["failed_cases"][:30]
        if failed:
            for item in failed:
                lines.append(
                    f"- `{item['id']}` gold={item['gold_agents']} pred={item['pred_agents']} query={item['query']}"
                )
                if item.get("error"):
                    lines.append(f"  - error: {item['error']}")
        else:
            lines.append("- None")
    else:
        lines.append("- Not run")

    lines.extend(["", "## Failed RAG Strict Cases", ""])
    if rag_eval["status"] == "completed":
        failed = rag_eval["failed_cases"]
        if failed:
            for item in failed:
                lines.append(
                    f"- `{item['id']}` cluster_hit={item['cluster_hit']} evidence_hit={item['evidence_hit']} "
                    f"max_similarity={item['max_similarity']} query={item['query']}"
                )
                if item.get("best_doc"):
                    lines.append(f"  - best_doc: {item['best_doc']}")
        else:
            lines.append("- None")
    else:
        lines.append("- Not run")

    content = "\n".join(lines) + "\n"
    RESULT_MD.write_text(content, encoding="utf-8")
    return content


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run Travel Agent evaluation.")
    parser.add_argument(
        "--rag-only",
        action="store_true",
        help="Only rerun RAG retrieval metrics and reuse previous agent metrics if available.",
    )
    parser.add_argument(
        "--rag-review",
        action="store_true",
        help="Run RAG metrics from tests/rag_evidence_review.json using review_search_query as gold evidence.",
    )
    args = parser.parse_args()
    if args.rag_review:
        args.rag_only = True

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    dataset = load_dataset()
    rag_dataset = load_rag_review_dataset() if args.rag_review else dataset
    started = time.perf_counter()
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dataset": str(DATASET_PATH),
        "rag_dataset": str(RAG_REVIEW_PATH) if args.rag_review else str(DATASET_PATH),
        "rag_gold_field": "review_search_query" if args.rag_review else "gold_evidence",
        "dataset_summary": {
            "sample_count": len(dataset["samples"]),
            "preference_eval_count": sum(1 for item in dataset["samples"] if item["gold"]["preference"]["eval"]),
            "rag_eval_count": sum(1 for item in rag_dataset["samples"] if item["gold"]["rag"]["eval"]),
            "user_count": len({item["user_id"] for item in dataset["samples"]}),
        },
        "mode": "rag_review_reuse_previous_agent_eval" if args.rag_review else (
            "rag_only_reuse_previous_agent_eval" if args.rag_only else "full"
        ),
    }

    if args.rag_only:
        if RESULT_JSON.exists():
            previous = json.loads(RESULT_JSON.read_text(encoding="utf-8"))
            report["agent_eval"] = previous.get("agent_eval", {
                "status": "skipped",
                "reason": "Previous result exists but has no agent_eval field.",
            })
        else:
            report["agent_eval"] = {
                "status": "skipped",
                "reason": "--rag-only was used and no previous result file exists.",
            }
    else:
        report["agent_eval"] = await run_agent_eval(dataset)
    report["rag_eval"] = run_rag_eval(rag_dataset)
    report["elapsed_sec"] = round(time.perf_counter() - started, 3)

    RESULT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report)
    print(json.dumps({
        "result_json": str(RESULT_JSON),
        "result_md": str(RESULT_MD),
        "dataset_summary": report["dataset_summary"],
        "agent_eval_status": report["agent_eval"]["status"],
        "rag_eval_status": report["rag_eval"]["status"],
        "elapsed_sec": report["elapsed_sec"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
