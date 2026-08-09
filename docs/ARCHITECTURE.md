# Auto Cut Bot — Agent-Native Architecture

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│                         🎯 AGENT LAYER (nanobot)                                  │
│                                                                                  │
│  ┌────────────────────────────────────────────────────────────────────────────┐  │
│  │                         Skills (7)                                          │  │
│  │  ac-source-prep  │  ac-series-knowledge  │  ac-story-generation             │  │
│  │  ac-plan-orchestr.  │  ac-qc  │  ac-render  │  ac-shared-contracts          │  │
│  │                                                                             │  │
│  │  SKILL.md = Agent 人机交互指令  │  references/ = 按需加载的知识文档           │  │
│  └────────────────────────────────────────────────────────────────────────────┘  │
│                                      │                                            │
│                                      ▼                                            │
│  ┌────────────────────────────────────────────────────────────────────────────┐  │
│  │                      Tools (21) — pipeline/                                 │  │
│  │                                                                             │  │
│  │  source_windows     window_analysis    event_cards       episode_digests    │  │
│  │  chapter_digests    series_registry    series_assignment  series_bible       │  │
│  │  story_catalog      story_portfolio    story_treatments   story_scripts      │  │
│  │  story_preflight    story_approval ✋  story_evidence     span_candidates    │  │
│  │  story_plans        story_plans_materialize  story_qc    story_qc_review ✋  │  │
│  │  story_render                                                                │  │
│  │                                                                             │  │
│  │  ✋ = human_review=True → Agent 暂停, WebSocket 通知 WebUI, 等待人工审批     │  │
│  └────────────────────────────────────────────────────────────────────────────┘  │
│                                      │                                            │
│                          AgentLoop.process_direct()                               │
│                          POST /v1/pipeline/run                                    │
└──────────────────────────────────────┼──────────────────────────────────────────┘
                                       │
                                       ▼
┌──────────────────────────────────────────────────────────────────────────────────┐
│                    🏗 PIPELINE ENGINE (auto_cut_bot/pipeline/)                    │
│                                                                                  │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐  │
│  │ StageRegistry  │  │  Contracts     │  │ Orchestrator   │  │  ArtifactBus   │  │
│  │                │  │                │  │                │  │                │  │
│  │ 自动发现 23    │  │ 20条规则引擎   │  │ pipeline.py    │  │ 内容寻址存储   │  │
│  │ Stage          │  │ 产物校验       │  │ auto.py        │  │ SHA-256 哈希链 │  │
│  │ _PIPELINE_ORDER│  │ 声明式规则     │  │ 状态机+恢复    │  │ 跨 Stage 绑定  │  │
│  └────────────────┘  └────────────────┘  └────────────────┘  └────────────────┘  │
│                                                                                  │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐  │
│  │  DB Layer 🆕   │  │  Cache 🆕      │  │  IO Layer       │  │  Backends      │  │
│  │                │  │                │  │                │  │                │  │
│  │ database_patch │  │ Stage 缓存     │  │ 原子写入       │  │ LLM 后端       │  │
│  │ asyncpg 直连   │  │ 断点续传       │  │ SHA-256 校验   │  │ provider       │  │
│  │ 10 表 CRUD     │  │ 幂等重跑       │  │ JSON/JSONL     │  │ factory        │  │
│  └────────────────┘  └────────────────┘  └────────────────┘  └────────────────┘  │
│                                                                                  │
│  ┌────────────────────────────────────────────────────────────────────────────┐  │
│  │                      Plugins (6) — 66 files                                 │  │
│  │  ac_source_prep │ ac_series_knowledge │ ac_story_generation                  │  │
│  │  ac_plan_orchestration │ ac_qc │ ac_render                                  │  │
│  └────────────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────┬──────────────────────────────────────────┘
                                       │
                                       ▼
┌──────────────────────────────────────────────────────────────────────────────────┐
│                         ⚙️ INFRASTRUCTURE LAYER                                   │
│                                                                                  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐  │
│  │ config   │ │ errors   │ │ io.py    │ │ logging  │ │ registry │ │ schema/  │  │
│  │ .py      │ │ .py      │ │ 原子 I/O │ │ 结构化   │ │ Stage    │ │ 8 模块   │  │
│  │ 4层合并  │ │ 7异常    │ │ SHA-256  │ │ 日志     │ │ 发现     │ │ Pydantic │  │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘  │
│                                                                                  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐               │
│  │ semantic │ │ contracts│ │ backends │ │ stages/  │ │ libs/    │               │
│  │ batch    │ │ /rules/  │ │ /_base   │ │ _base.py │ │ 纯函数   │               │
│  │ runner   │ │ 20 rules │ │ provider │ │ adapter  │ │ 工具集   │               │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘               │
└──────────────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────────────┐
│                       🖥 FRONTEND — Next.js 15 (viz-web-next)                     │
│                                                                                  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐  │
│  │Dashboard │ │BookList  │ │BookDetail│ │Character │ │Episode   │ │Story     │  │
│  │/         │ │/books    │ │[bookId]  │ │Graph     │ │View      │ │Review    │  │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐               │
│  │Story     │ │Pipeline  │ │Settings  │ │Explorer  │ │GraphiQL  │               │
│  │Detail    │ │View      │ │/settings │ │/explorer │ │/graphiql │               │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘               │
│                                                                                  │
│  Data Sources:  PostGraphile v5 (GraphQL)  │  JSON Files (readFileSync)          │
│                 SSE Event Stream           │  Project Config Scanner             │
└──────────────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────────────┐
│                          📡 CHANNELS (仅 3 个)                                    │
│                                                                                  │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐               │
│  │ websocket        │  │ api (HTTP)       │  │ MCP              │               │
│  │ WebUI 实时通信   │  │ POST /v1/pipeline│  │ 外部工具集成     │               │
│  │ :8765            │  │ :8900            │  │                  │               │
│  └──────────────────┘  └──────────────────┘  └──────────────────┘               │
│                                                                                  │
│  ❌ 已删除: telegram, discord, slack, feishu, weixin, whatsapp,                  │
│             dingtalk, matrix, signal, email, msteams, mattermost,                │
│             mochat, napcat, qq, wecom (16 channels)                              │
└──────────────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────────────┐
│                       📊 AGENT-NATIVE FLOW                                        │
│                                                                                  │
│  ┌─────────┐    ┌─────────┐    ┌─────────┐    ┌─────────┐    ┌─────────┐        │
│  │ HTTP    │───▶│ Agent   │───▶│ Skill   │───▶│ Tool    │───▶│ Stage   │        │
│  │ Trigger │    │ Loop    │    │ Load    │    │ Execute │    │ Execute │        │
│  └─────────┘    └─────────┘    └─────────┘    └─────────┘    └─────────┘        │
│                      │                                            │               │
│                      │              ┌─────────────────────────────┘               │
│                      ▼              ▼                                              │
│               ┌──────────┐  ┌──────────────┐                                      │
│               │ Contracts│  │ ArtifactBus  │                                      │
│               │ Validate │  │ .put()       │                                      │
│               └──────────┘  └──────────────┘                                      │
│                      │              │                                              │
│                      ▼              ▼                                              │
│               ┌──────────┐  ┌──────────────┐                                      │
│               │ DB       │  │ Next Tool    │  ← 循环直到 pipeline 完成            │
│               │ .patch() │  │              │                                      │
│               └──────────┘  └──────────────┘                                      │
│                                                                                  │
│  Human Review Flow:                                                              │
│  Tool(✋) → Agent.pause() → WebSocket → WebUI → 人工审批 → Agent.resume()        │
└──────────────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────────────┐
│                       📋 IMPLEMENTATION STATUS                                    │
│                                                                                  │
│  ✅ 已迁移:                                                                      │
│    • Pipeline Engine: core/ (134 files) + plugins/ (66 files)                    │
│    • Registry: StageRegistry + _PIPELINE_ORDER (23 stages)                       │
│    • Contracts: 20 rules engine                                                  │
│    • IO: atomic_write + SHA-256 + canonical_json                                 │
│    • ArtifactBus: 内容寻址 + 哈希链 + index.json                                 │
│    • Config: 4 层优先级合并                                                       │
│    • Schema: 8 modules + db_entities                                             │
│    • Backends: _base.py + rate_limiter                                           │
│    • Semantic: batch_runner + engine/ + prep/                                    │
│    • Channels: 16→1 (websocket only)                                             │
│    • Tools: 21 pipeline Tool wrappers                                            │
│    • Skills: 7 SKILL.md + references                                             │
│    • API: POST /v1/pipeline/run                                                  │
│    • Config: pre-configured config.json                                          │
│                                                                                  │
│  ❌ 待实现:                                                                      │
│    • DB Layer: StageDBClient (asyncpg)                                           │
│    • Cache Layer: StageCache (断点续传)                                          │
│    • Contracts Integration: Tool.validate() 调用规则引擎                          │
│    • Frontend: viz-web-next 适配 auto_cut_bot                                    │
│    • E2E Test: 端到端流水线测试                                                  │
│    • 路径遍历修复: pipeline tools 校验 job_root                                  │
└──────────────────────────────────────────────────────────────────────────────────┘