# Travel Agent 100-Turn Evaluation

- Generated at: 2026-06-28 18:14:04
- Dataset: `D:\Algorithm\for study\travel agent\tests\eval_dataset_100.json`
- RAG dataset: `D:\Algorithm\for study\travel agent\tests\rag_evidence_review.json`
- RAG gold field: `review_search_query`

## Summary

- Intent success rate: 89/100 = 89.0%
- Preference trigger rate: 20/20 = 100.0%
- Overall cache hit rate: 184/200 = 92.0%
  - preferences: 92/100 = 92.0%
  - short_term: 92/100 = 92.0%
- RAG Cluster Hit@K: 16/20 = 80.0%
- RAG Evidence Hit@K: 18/20 = 90.0%
- RAG Strict Hit@K: 13/20 = 65.0%

## Failed Intent Cases

- `case_011` gold=['event_collection', 'itinerary_planning', 'memory_query'] pred=['event_collection', 'preference', 'itinerary_planning'] query=参加完北京的会议后，我要去广州再住两天一晚，广州纯旅游，帮我规划。
- `case_022` gold=['memory_query', 'rag_knowledge'] pred=['rag_knowledge', 'event_collection'] query=刚才说的报销标准是啥来着，所以我这次去的话每天能报销多少？
- `case_026` gold=['memory_query'] pred=['information_query'] query=总结一下我的所有行程，然后告诉我要准备些什么东西
- `case_033` gold=['information_query'] pred=['rag_knowledge'] query=我到站后要等别人下班来接我，合肥南站附近有什么适合短暂停留的地方？
- `case_047` gold=['information_query'] pred=['preference', 'event_collection', 'itinerary_planning'] query=武汉站到光谷软件园交通怎么安排？
- `case_060` gold=['memory_query', 'rag_knowledge'] pred=['rag_knowledge'] query=我之前说的那几个规定，有针对济南说什么吗，就是济南有啥规定？
- `case_062` gold=['event_collection', 'itinerary_planning', 'rag_knowledge'] pred=['event_collection', 'itinerary_planning'] query=帮我做一个从上海去南昌参加项目验收的行程。
- `case_071` gold=['information_query', 'itinerary_planning'] pred=['information_query'] query=珠海下雨的话，有什么室内旅游项目嘛？
- `case_076` gold=['memory_query', 'information_query', 'itinerary_planning'] pred=['memory_query', 'event_collection', 'information_query'] query=我上次发的那个去天津的行程，有没有什么在北京中转半天的出行方式
- `case_086` gold=['information_query', 'itinerary_planning', 'event_collection'] pred=['information_query', 'itinerary_planning'] query=福州附近是不是哪里有什么蓝月亮现象，是湖里的微生物发光导致的，那是哪，我第一天晚上准备去那，重新规划一下行程？
- `case_087` gold=['memory_query'] pred=['event_collection', 'itinerary_planning'] query=所以帮我总结一下福州这次的行程。

## Failed RAG Strict Cases

- `case_018` cluster_hit=True evidence_hit=False max_similarity=0.6832 query=按学校文件的说法，我出行的话的，市内交通费标准是什么？
  - best_doc: {'rank': 2, 'parent_doc': '04_business_travel_faq_tongji.txt', 'chunk_index': 1, 'similarity': 0.6832}
- `case_019` cluster_hit=True evidence_hit=False max_similarity=0.6984 query=学校报销系统里差旅报销大概怎么提交？
  - best_doc: {'rank': 3, 'parent_doc': '财务报销培训.md', 'chunk_index': 15, 'similarity': 0.6984}
- `case_043` cluster_hit=False evidence_hit=True max_similarity=0.7747 query=出国参加会议的时候，学校有啥规定吗？
  - best_doc: {'rank': 1, 'parent_doc': '同济大学报销手册_2026年3月.md', 'chunk_index': 32, 'similarity': 0.7747}
- `case_044` cluster_hit=False evidence_hit=True max_similarity=0.742 query=需要什么审批材料吗？
  - best_doc: {'rank': 3, 'parent_doc': '同济大学报销手册_2026年3月.md', 'chunk_index': 55, 'similarity': 0.742}
- `case_055` cluster_hit=False evidence_hit=True max_similarity=0.757 query=哪些差旅相关费用不能报？
  - best_doc: {'rank': 1, 'parent_doc': 'tongji_travel_expense_management_rules_2024_revised_2026.md', 'chunk_index': 9, 'similarity': 0.757}
- `case_056` cluster_hit=True evidence_hit=True max_similarity=0.725 query=个人顺路旅游能放进学校差旅报销里吗？
  - best_doc: {'rank': 3, 'parent_doc': 'tongji_travel_expense_management_rules_2024_revised_2026.md', 'chunk_index': 9, 'similarity': 0.725}
- `case_092` cluster_hit=False evidence_hit=True max_similarity=0.7313 query=学校外出证件遗失或者票据丢了怎么处理？
  - best_doc: {'rank': 1, 'parent_doc': '同济大学报销手册_2026年3月.md', 'chunk_index': 10, 'similarity': 0.7313}
