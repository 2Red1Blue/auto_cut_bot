---
excalidraw-plugin: parsed
tags: [excalidraw]
---
== Switch to EXCALIDRAW VIEW in the MORE OPTIONS menu of this document. ==

# Auto Cut Bot — Agent-Native Architecture

## Text Elements

### Agent Layer (nanobot)
🎯 Skills — auto_cut_bot/skills/
ac-source-prep (Stg 1-5) | ac-series-knowledge (Stg 6-11) | ac-story-generation (Stg 12-17)
ac-plan-orchestration (Stg 18-22) | ac-qc (Stg 23-25) | ac-render (Stg 26) | ac-shared-contracts

🔧 Tools — auto_cut_bot/agent/tools/pipeline/
source_windows | window_analysis | event_cards | episode_digests | chapter_digests
series_registry | series_assignment | series_bible | story_catalog | story_portfolio
story_treatments | story_scripts | story_preflight | story_approval ✋ | story_evidence
span_candidates | story_plans | story_plans_materialize | story_qc | story_qc_review ✋ | story_render

✋ = human_review=True (Agent 暂停等待人工审批)

### Pipeline Engine
🏗 Pipeline Engine — auto_cut_bot/pipeline/
core/ (134 files) | plugins/ (66 files)
StageRegistry (自动发现) | Contracts (20条规则) | Orchestrator (状态机)
ArtifactBus (哈希链+缓存) | DB Layer (database_patch) | IO (原子写入)

### Infrastructure
⚙️ Infrastructure
config.py | errors.py | io.py | logging.py | registry.py
contracts/ (rules engine) | backends/ (LLM providers)
semantic/ (batch runner) | schema/ (data models) | stages/ (base + adapter)

### Frontend
🖥 Frontend — Next.js 15
Dashboard | BookList | BookDetail | CharacterGraph | EpisodeView
StoryReview | StoryDetail | PipelineView | Settings | Explorer | GraphiQL

### Data Flow
📊 Data Flow
API Trigger → AgentLoop → Skill Load → Tool Execute → Pipeline Engine
→ StageRegistry.discover() → Stage.prepare() → Stage.execute()
→ Contracts.validate() → ArtifactBus.put() → DB.patch()
→ Agent Response → WebUI SSE

### Channels
📡 Channels (仅保留3个)
websocket (WebUI) | api (HTTP trigger) | MCP (external tools)

### Status
迁移状态
已迁移: Stage 层次 23 Stage + Infrastructure 全部
待集成: DB Layer, Cache Layer, Contracts 校验
待实现: 前端 Next.js 适配, 端到端测试

## Drawing

%% Excalidraw diagram would be rendered here in the IDE
%% Format follows the same compressed-json structure as the reference
%% Key layers: Agent (top) → Pipeline Engine (middle) → Infrastructure (bottom)
%% With frontend panel on the right and data flow arrows connecting them