# 从 ac_auto_cut 迁移修复到 auto_cut_bot

## 架构说明

auto_cut_bot 采用 **Agent-Native V2** 架构：

```
auto_cut_bot/
├── state_graph/              # 本地核心（Agent 框架）
├── pipeline/plugins/         # 复制的 ac_auto_cut stages
├── agent/tools/pipeline/     # 包装 stages 为 Agent tools
└── imports autocut_core      # 外部依赖 ac_auto_cut
```

**关键问题**：`pipeline/plugins/` 目录是从 ac_auto_cut 复制的，包含独立的 stage 实现，需要手动同步修复。

---

## 已完成的修复（ac_auto_cut 14 commits）

### P0 — 运行时崩溃（4项）

| # | 问题 | 修复位置 | auto_cut_bot 状态 |
|---|------|----------|------------------|
| 1 | `_write_recovery_log` 返回未定义 `result` | `autocut_core/orchestrator/pipeline.py` | ✅ 自动生效（外部依赖） |
| 2 | `global_context` 重复 except 块 | `pipeline/plugins/ac_source_prep/stages/global_context/stage.py` | ✅ 已包含修复 |
| 3 | API 降级时 episodes 为空 → FK 阻断 VLM | `pipeline/plugins/ac_source_prep/stages/vlm_analysis/stage.py` | ✅ 已包含修复 |
| 4 | `init.sql` 缺少 `sources_evidence` 列 | `deploy/db/init.sql` | ⚠️ 需要检查 DB schema |

### P1 — 契约不一致（5项）

| # | 问题 | 修复位置 | auto_cut_bot 状态 |
|---|------|----------|------------------|
| 5 | `event_cards` 声明 `db_writes=["boundaries", "subjects"]` 但从未写 DB | `pipeline/plugins/ac_series_knowledge/stages/event_cards/stage.py` | ❌ 未修复 |
| 6 | `series_registry` 声明 `db_writes` 不匹配 | `pipeline/plugins/ac_series_knowledge/stages/registry/stage.py` | ❌ 需要检查 |
| 8 | `_execute_recovery` 使用 `order.index()` | `autocut_core/orchestrator/pipeline.py` | ✅ 自动生效 |
| 9 | `chapter_digests` 跨插件 import | `pipeline/plugins/ac_series_knowledge/stages/chapter_digests/stage.py` | ❌ 需要检查 |
| 14 | `pre_build` 阶段 `prepare()` 降级 | `autocut_core/orchestrator/pipeline.py` | ✅ 自动生效 |

### P2 — 代码卫生（2项）

| # | 问题 | 修复位置 | auto_cut_bot 状态 |
|---|------|----------|------------------|
| 11 | bare `except:` | `autocut_core/orchestrator/pipeline.py` | ✅ 自动生效 |
| 12 | f-string SQL schema | `autocut_core/orchestrator/pipeline.py` | ✅ 自动生效 |

---

## 需要同步的修复

### 1. event_cards db_writes 修正

**文件**: `auto_cut_bot/pipeline/plugins/ac_series_knowledge/stages/event_cards/stage.py`

```python
# 修复前
db_writes=["boundaries", "subjects"],

# 修复后
db_writes=[],  # stage 代码中无 DB 写入操作
```

### 2. series_registry db_writes 修正

**文件**: `auto_cut_bot/pipeline/plugins/ac_series_knowledge/stages/registry/stage.py`

检查实际 DB 写入：
```bash
grep -n "db\.\|insert_\|upsert_" auto_cut_bot/pipeline/plugins/ac_series_knowledge/stages/registry/stage.py
```

如果只写 `highlight_skill_evolution` 表：
```python
db_writes=["highlight_skill_evolution"],
```

### 3. chapter_digests 跨插件 import

**文件**: `auto_cut_bot/pipeline/plugins/ac_series_knowledge/stages/chapter_digests/stage.py`

检查是否有：
```python
from plugins.ac_series_knowledge.stages.episode_digests.stage import _collect_digest_records
```

如果有，需要：
1. 将 `_collect_digest_records` 提取到 `autocut_core/io.py`
2. 更新 import 为 `from autocut_core.io import collect_digest_records`

---

## 自动生效的修复（外部依赖）

以下修复位于 `autocut_core` 包中，auto_cut_bot 通过外部依赖自动获得：

- ✅ `pipeline.py` 的 orchestrator 修复（#1, #8, #11, #12, #14）
- ✅ `db/client.py` 的数据库方法修复
- ✅ `semantic/` 层的契约和验证修复
- ✅ `io.py` 的公共函数

**前提**：auto_cut_bot 的 `pyproject.toml` 正确依赖 `ac_auto_cut` 包。

---

## 建议的同步流程

### 方案 A：手动同步（当前）

```bash
# 1. 检查需要修复的文件
cd /Users/liuzx/Code/python/work_ai/auto_cut_bot

# 2. 应用修复（参考 ac_auto_cut 的 commits）
# - event_cards: db_writes=[]
# - series_registry: db_writes=["highlight_skill_evolution"]
# - chapter_digests: 提取 _collect_digest_records 到 autocut_core.io

# 3. 提交
git add -u
git commit -m "fix(pipeline): sync critical fixes from ac_auto_cut

- event_cards: db_writes=[] (no DB writes in code)
- series_registry: align db_writes with actual behavior
- chapter_digests: eliminate cross-plugin import"
```

### 方案 B：依赖管理（推荐）

1. **添加 autocut_core 为依赖**：
```toml
# pyproject.toml
[project]
dependencies = [
    "ac-auto-cut @ file:///Users/liuzx/Code/python/work_ai/ac_auto_cut",
    # ... 其他依赖
]
```

2. **删除本地复制的 stages**：
```bash
rm -rf auto_cut_bot/pipeline/plugins/
```

3. **Agent tools 直接 import autocut_core**：
```python
# auto_cut_bot/agent/tools/pipeline/global_context.py
from autocut_core.pipeline.plugins.ac_source_prep.stages.global_context import GlobalContextStage
```

**优点**：
- 自动获得 ac_auto_cut 的所有修复
- 减少代码重复
- 统一的版本管理

---

## 验证清单

应用修复后，验证：

- [ ] `event_cards` stage 运行无 DB 写入警告
- [ ] `series_registry` stage 的 db_writes 与实际行为匹配
- [ ] `chapter_digests` 不再跨插件 import
- [ ] `vlm_analysis` 在 API 降级时仍能写入 episodes（FK guard）
- [ ] `global_context` 无重复 except 块
- [ ] orchestrator 的 `_write_recovery_log` 无 NameError
- [ ] 无 bare `except:` 警告

---

## 参考

- ac_auto_cut 修复 commits: `4c2d288` 到 `288a461`
- ac_auto_cut 分支: `feat/vad-spk-punc-pipeline`
- auto_cut_bot 架构: Agent-Native V2 (StateGraph)
