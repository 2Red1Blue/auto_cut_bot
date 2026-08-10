---
excalidraw-plugin: parsed
tags: [excalidraw, architecture]
---
== Switch to EXCALIDRAW VIEW in the MORE OPTIONS menu of this document. ==

# Auto Cut Bot — Agent-Native V2 Architecture

```json
{
  "type": "excalidraw",
  "version": 2,
  "source": "https://excalidraw.com",
  "elements": [
    {
      "id": "frontend-zone",
      "type": "rectangle",
      "x": 40, "y": 40, "width": 1120, "height": 90,
      "strokeColor": "#1971c2", "backgroundColor": "#e7f5ff",
      "fillStyle": "solid", "strokeWidth": 2, "roughness": 0,
      "roundness": {"type": 3}
    },
    {
      "id": "frontend-title",
      "type": "text",
      "x": 60, "y": 50, "width": 400, "height": 25,
      "text": "🖥️ Next.js 15 WebUI & API Gateways",
      "fontSize": 18, "fontFamily": 1, "textAlign": "left"
    },
    {
      "id": "frontend-desc",
      "type": "text",
      "x": 60, "y": 80, "width": 1000, "height": 20,
      "text": "Dashboard | StoryReview | Live SSE Execution Tracer | Human Approval Portal (HITL)",
      "fontSize": 13, "fontFamily": 1, "textAlign": "left"
    },
    {
      "id": "agent-core-zone",
      "type": "rectangle",
      "x": 40, "y": 160, "width": 1120, "height": 230,
      "strokeColor": "#5f3dc4", "backgroundColor": "#f3f0ff",
      "fillStyle": "solid", "strokeWidth": 2, "roughness": 0,
      "roundness": {"type": 3}
    },
    {
      "id": "agent-core-title",
      "type": "text",
      "x": 60, "y": 170, "width": 500, "height": 25,
      "text": "🧠 Agent Control Plane (Single Control Engine)",
      "fontSize": 18, "fontFamily": 1, "textAlign": "left"
    },
    {
      "id": "state-graph-box",
      "type": "rectangle",
      "x": 60, "y": 205, "width": 530, "height": 160,
      "strokeColor": "#3bc9db", "backgroundColor": "#e6fc15",
      "fillStyle": "solid", "strokeWidth": 1, "roughness": 0,
      "roundness": {"type": 3}
    },
    {
      "id": "state-graph-text",
      "type": "text",
      "x": 75, "y": 215, "width": 500, "height": 130,
      "text": "📌 Agent StateGraph Engine (LangGraph/Custom)\n- Goal-Driven Milestones (No Rigid Stage 1-26)\n- Checkpointer Persistence (Postgres Agent State)\n- HITL Interrupt Gates (story_approval ✋ / qc_review ✋)\n- Dynamic Skill Loader (.md Playbooks on demand)",
      "fontSize": 12, "fontFamily": 1, "textAlign": "left"
    },
    {
      "id": "subagent-zone",
      "type": "rectangle",
      "x": 610, "y": 205, "width": 530, "height": 160,
      "strokeColor": "#7048e8", "backgroundColor": "#f8f0ff",
      "fillStyle": "solid", "strokeWidth": 1, "roughness": 0,
      "roundness": {"type": 3}
    },
    {
      "id": "subagent-text",
      "type": "text",
      "x": 625, "y": 215, "width": 500, "height": 130,
      "text": "🤖 Specialized Domain Sub-Agents (Coarse-Grained Tools)\n1. SourceSubAgent (Adaptive Parsing & Alignment)\n2. StorySubAgent (Bible, Treatments, Scripts)\n3. ProductionSubAgent (Orchestration, QC, Rendering)\n-> Replaces 20+ fragmented micro-tools to prevent hallucination",
      "fontSize": 12, "fontFamily": 1, "textAlign": "left"
    },
    {
      "id": "engine-zone",
      "type": "rectangle",
      "x": 40, "y": 420, "width": 1120, "height": 180,
      "strokeColor": "#2b8a3e", "backgroundColor": "#ebfbee",
      "fillStyle": "solid", "strokeWidth": 2, "roughness": 0,
      "roundness": {"type": 3}
    },
    {
      "id": "engine-title",
      "type": "text",
      "x": 60, "y": 430, "width": 500, "height": 25,
      "text": "⚡ Deterministic Execution Engine (Python Core)",
      "fontSize": 18, "fontFamily": 1, "textAlign": "left"
    },
    {
      "id": "engine-tools-text",
      "type": "text",
      "x": 60, "y": 465, "width": 1080, "height": 120,
      "text": "🛠️ Adaptive Script Chunker (Context Parsing OR MapReduce Fallback)  |  🎬 FFmpeg / GPU Video Rendering Core\n📦 Artifact Bus (SHA256 Content-Addressing Cache)                  |  🗄️ StageDB Client (10 Tables Atomic Ops)\n🎯 Semantic Matching Engine (No nested Agent LLM calls)            |  📑 Rules & Contracts Enforcement Engine",
      "fontSize": 12, "fontFamily": 1, "textAlign": "left"
    },
    {
      "id": "arrow-1",
      "type": "arrow",
      "x": 600, "y": 130, "width": 0, "height": 30,
      "strokeColor": "#1971c2", "strokeWidth": 2, "points": [[0, 0], [0, 30]]
    },
    {
      "id": "arrow-2",
      "type": "arrow",
      "x": 600, "y": 390, "width": 0, "height": 30,
      "strokeColor": "#5f3dc4", "strokeWidth": 2, "points": [[0, 0], [0, 30]]
    },
    {
      "id": "dataflow-zone",
      "type": "rectangle",
      "x": 40, "y": 630, "width": 1120, "height": 160,
      "strokeColor": "#c92a2a", "backgroundColor": "#fff5f5",
      "fillStyle": "solid", "strokeWidth": 2, "roughness": 0,
      "roundness": {"type": 3}
    },
    {
      "id": "dataflow-title",
      "type": "text",
      "x": 60, "y": 640, "width": 400, "height": 25,
      "text": "📊 Data Flow (source_script example)",
      "fontSize": 16, "fontFamily": 1, "textAlign": "left"
    },
    {
      "id": "dataflow-steps",
      "type": "text",
      "x": 60, "y": 675, "width": 1080, "height": 100,
      "text": "1. API Trigger → Main Agent plans milestone → SourceAgent.load_script() → gets full script text\n2. Adaptive: <50K tokens → Direct Context Parse | >50K tokens → MapReduce Chunker (parallel chunks)\n3. SourceAgent.save_script(episodes) → DB write (scenes/subjects/shots/subtitles) + Artifact publish\n4. Milestone → source_ready | HITL needed → Interrupt → Checkpoint to DB → Release resources\n5. Human approves → resume(session_id) → Agent unfreezes → StoryAgent continues",
      "fontSize": 13, "fontFamily": 1, "textAlign": "left"
    },
    {
      "id": "comparison-table",
      "type": "rectangle",
      "x": 40, "y": 820, "width": 1120, "height": 130,
      "strokeColor": "#495057", "backgroundColor": "#f8f9fa",
      "fillStyle": "solid", "strokeWidth": 2, "roughness": 0,
      "roundness": {"type": 3}
    },
    {
      "id": "comparison-title",
      "type": "text",
      "x": 60, "y": 830, "width": 400, "height": 25,
      "text": "📋 Architecture Comparison",
      "fontSize": 16, "fontFamily": 1, "textAlign": "left"
    },
    {
      "id": "comparison-text",
      "type": "text",
      "x": 60, "y": 860, "width": 1080, "height": 80,
      "text": "Old: 26 Stages + 20+ Micro-Tools → Agent vs Orchestrator fight for control | Blind single-pass parsing → truncation | HITL = process hang | Cache locks bad results\nNew: 3 Domain Sub-Agents → Single Control Plane (StateGraph) | Adaptive: Direct OR MapReduce | HITL = persistent checkpoint | Cache-aware with auto-invalidation\nResult: Smarter (Goal-driven), More Stable (No hallucination from 20 tools), Cheaper (No nested LLM), Production-Ready (Checkpoint survive restarts)",
      "fontSize": 12, "fontFamily": 1, "textAlign": "left"
    }
  ],
  "appState": { "viewBackgroundColor": "#ffffff" },
  "files": {}
}
```