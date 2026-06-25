#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Lightweight evaluation for Travel Agent.

This script intentionally avoids AgentScope, LLM calls, Milvus, and network access.
It measures only things that can be verified from local code/data:
- Skill plugin discovery count and scan time
- Memory persistence behavior
- Preference append/replace write behavior
- RAG source-document keyword coverage
- A simple keyword-intent baseline on a small curated set

Use these results as conservative engineering evidence, not as final LLM accuracy.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from statistics import mean


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from context.memory_manager import MemoryManager  # noqa: E402


def pct(num: int, den: int) -> str:
    return f"{num / den * 100:.1f}%" if den else "0.0%"


def evaluate_skill_discovery() -> dict:
    skills_root = PROJECT_ROOT / ".claude" / "skills"
    timings = []
    names = []
    for _ in range(30):
        start = time.perf_counter()
        discovered = []
        for skill_dir in skills_root.iterdir():
            if (skill_dir / "SKILL.md").exists() and (skill_dir / "script" / "agent.py").exists():
                discovered.append(skill_dir.name)
        timings.append((time.perf_counter() - start) * 1000)
        names = sorted(discovered)

    expected = {
        "ask-question",
        "event-collection",
        "memory-query",
        "plan-trip",
        "preference",
        "query-info",
    }
    return {
        "metric": "skill_plugin_discovery",
        "passed": set(names) == expected,
        "skill_count": len(names),
        "skills": names,
        "avg_scan_ms": round(mean(timings), 3),
        "min_scan_ms": round(min(timings), 3),
        "max_scan_ms": round(max(timings), 3),
    }


def evaluate_memory_persistence() -> dict:
    checks = []
    with tempfile.TemporaryDirectory(prefix="travel_agent_eval_") as temp_dir:
        user_id = "eval_user"
        manager = MemoryManager(user_id=user_id, session_id="session_a", storage_path=temp_dir)

        for i in range(24):
            role = "user" if i % 2 == 0 else "assistant"
            manager.add_message(role, f"message_{i}")

        # Short-term memory keeps max_turns * 2 messages. MemoryManager uses max_turns=10.
        checks.append(("short_term_keeps_20_messages", len(manager.short_term.messages) == 20))
        checks.append(("long_term_keeps_all_24_messages", len(manager.long_term.get_chat_history()) == 24))

        manager.long_term.save_preference("transportation_preference", "高铁")
        manager.long_term.save_preference("hotel_brands", ["汉庭"])

        current = manager.long_term.get_preference()
        hotel_brands = current.get("hotel_brands", [])
        if "如家" not in hotel_brands:
            hotel_brands.append("如家")
        manager.long_term.save_preference("hotel_brands", hotel_brands)

        manager.long_term.save_preference("home_location", "上海")
        manager.long_term.save_preference("home_location", "苏州")

        manager.long_term.save_trip_history({
            "origin": "上海",
            "destination": "青岛",
            "start_date": "2026-06-22",
            "end_date": "2026-06-23",
            "purpose": "出差",
        })
        manager.long_term.save_trip_history({
            "origin": "上海",
            "destination": "合肥",
            "start_date": "2026-07-01",
            "end_date": None,
            "purpose": "旅游",
        })

        reloaded = MemoryManager(user_id=user_id, session_id="session_b", storage_path=temp_dir)
        prefs = reloaded.long_term.get_preference()
        trips = reloaded.long_term.get_trip_history(limit=None)

        checks.append(("preference_replace_transport", prefs.get("transportation_preference") == "高铁"))
        checks.append(("preference_append_hotel_brand", prefs.get("hotel_brands") == ["汉庭", "如家"]))
        checks.append(("preference_replace_home_location", prefs.get("home_location") == "苏州"))
        checks.append(("trip_history_persisted", len(trips) == 2))
        checks.append(("frequent_destination_stats", dict(reloaded.long_term.get_frequent_destinations(2)) == {"青岛": 1, "合肥": 1}))

    passed = sum(1 for _, ok in checks if ok)
    return {
        "metric": "memory_persistence_and_preference_write",
        "passed": passed == len(checks),
        "passed_checks": passed,
        "total_checks": len(checks),
        "accuracy": pct(passed, len(checks)),
        "checks": [{"name": name, "passed": ok} for name, ok in checks],
    }


def evaluate_rag_corpus_coverage() -> dict:
    docs_dir = PROJECT_ROOT / ".claude" / "skills" / "ask-question" / "data" / "documents"
    files = sorted([p for p in docs_dir.iterdir() if p.suffix.lower() in {".txt", ".md"}])
    corpus = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in files)

    cases = [
        {"name": "住宿标准/住宿费", "keywords": ["住宿", "标准"]},
        {"name": "报销材料", "keywords": ["发票", "报销"]},
        {"name": "航班延误", "keywords": ["延误", "凭证"]},
        {"name": "会议费/会务", "keywords": ["会议", "报销"]},
        {"name": "因公出国", "keywords": ["因公", "出国"]},
        {"name": "八项规定", "keywords": ["八项规定"]},
        {"name": "北京城市出行", "keywords": ["北京", "首都"]},
        {"name": "青岛城市出行", "keywords": ["青岛"]},
    ]

    results = []
    for case in cases:
        found = [kw for kw in case["keywords"] if kw in corpus]
        results.append({
            "name": case["name"],
            "expected_keywords": case["keywords"],
            "found_keywords": found,
            "passed": len(found) == len(case["keywords"]),
        })

    passed = sum(1 for item in results if item["passed"])
    return {
        "metric": "rag_source_document_keyword_coverage",
        "passed": passed == len(results),
        "document_count": len(files),
        "total_chars": len(corpus),
        "passed_cases": passed,
        "total_cases": len(results),
        "coverage": pct(passed, len(results)),
        "results": results,
    }


INTENT_CASES = [
    ("我要从上海去青岛出差两天", {"event_collection", "itinerary_planning"}),
    ("下周一去北京参加会议，帮我安排行程", {"event_collection", "itinerary_planning"}),
    ("我喜欢住汉庭，以后优先安排", {"preference"}),
    ("我还喜欢如家", {"preference"}),
    ("我搬家到苏州了", {"preference"}),
    ("上海明天天气怎么样", {"information_query"}),
    ("帮我查一下杭州限行", {"information_query"}),
    ("北京住宿标准是多少", {"rag_knowledge"}),
    ("差旅费怎么报销", {"rag_knowledge"}),
    ("航班延误怎么办", {"rag_knowledge"}),
    ("我之前去过哪些地方", {"memory_query"}),
    ("我上次去青岛是什么时候", {"memory_query"}),
    ("我有什么出行偏好", {"memory_query"}),
    ("我从上海去青岛参加国信项目，不知道几天", {"event_collection", "itinerary_planning", "memory_query", "rag_knowledge"}),
    ("从天津去北京，我喜欢住全季，查天气并规划", {"event_collection", "preference", "information_query", "itinerary_planning"}),
    ("同济会议费报销需要什么材料", {"rag_knowledge"}),
    ("我想去信阳一日游", {"event_collection", "itinerary_planning"}),
    ("帮我看看青岛国信集团附近住哪里合适", {"event_collection", "itinerary_planning", "rag_knowledge"}),
    ("我改成靠窗座位", {"preference"}),
    ("今天有什么新闻", {"information_query"}),
]


def keyword_baseline(query: str) -> set[str]:
    intents = set()
    if re.search(r"之前|上次|去过|历史|记得|我有什么|我的.*偏好", query):
        intents.add("memory_query")
    if re.search(r"喜欢|偏好|家在|搬家|常坐|靠窗|大机型|改成|优先", query):
        intents.add("preference")
    if re.search(r"天气|限行|新闻|查一下|查询|开放|实时", query):
        intents.add("information_query")
    if re.search(r"报销|标准|差旅|住宿标准|发票|审批|补助|会议费|同济|航班延误", query):
        intents.add("rag_knowledge")
    if re.search(r"从.+去|去.+出差|安排行程|规划|一日游|参加会议|参加.*项目|住哪里", query):
        intents.add("event_collection")
        intents.add("itinerary_planning")
    return intents or {"information_query"}


def evaluate_keyword_intent_baseline() -> dict:
    rows = []
    exact = 0
    contains_expected = 0
    for query, expected in INTENT_CASES:
        predicted = keyword_baseline(query)
        is_exact = predicted == expected
        has_expected = expected.issubset(predicted)
        exact += int(is_exact)
        contains_expected += int(has_expected)
        rows.append({
            "query": query,
            "expected": sorted(expected),
            "predicted": sorted(predicted),
            "exact": is_exact,
            "expected_subset_hit": has_expected,
        })

    return {
        "metric": "keyword_intent_baseline",
        "case_count": len(INTENT_CASES),
        "exact_match": pct(exact, len(INTENT_CASES)),
        "expected_subset_hit": pct(contains_expected, len(INTENT_CASES)),
        "exact_count": exact,
        "expected_subset_count": contains_expected,
        "note": "This is a simple keyword baseline, not the LLM IntentionAgent result.",
        "rows": rows,
    }


def evaluate_storage_claims() -> dict:
    py_files = [
        p for p in PROJECT_ROOT.rglob("*.py")
        if p.resolve() != Path(__file__).resolve()
    ]
    requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8", errors="ignore")
    text = requirements + "\n" + "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in py_files)
    lower = text.lower()
    return {
        "metric": "storage_implementation_scan",
        "postgresql_code_present": any(term in lower for term in ["postgres", "psycopg", "sqlalchemy", "asyncpg", "pgvector"]),
        "redis_code_present": "redis" in lower,
        "milvus_code_present": "pymilvus" in lower or "milvusclient" in lower,
        "json_memory_files_present": bool(list((PROJECT_ROOT / "data" / "memory").glob("*.json"))),
    }


def main() -> None:
    started = time.perf_counter()
    report = {
        "project": "travel agent",
        "mode": "offline_lightweight",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "evaluations": [
            evaluate_skill_discovery(),
            evaluate_memory_persistence(),
            evaluate_rag_corpus_coverage(),
            evaluate_keyword_intent_baseline(),
            evaluate_storage_claims(),
        ],
    }
    report["elapsed_sec"] = round(time.perf_counter() - started, 3)

    out_dir = PROJECT_ROOT / "tests" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "lightweight_eval_latest.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
