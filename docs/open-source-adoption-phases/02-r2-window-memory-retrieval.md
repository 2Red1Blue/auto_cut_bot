# R2：跨窗记忆与语义检索

## 目标与完成定义

分别验证 VideoAgent 的长视频检索思路和 MMLVE 的跨镜头实体关联，减少同人拆分、异人合并、
重复事件与关键高光遗漏。第一步仅处理已保存 VLM 输出；确认有效后才把前缀记忆加入新 VLM 请求版本。

完成要求：无未来信息、无标签泄漏；实体匹配和 Recall@K 均有独立分母；增量重建范围可计算；
增强模式能按单集/窗口续跑，且不会让原始独立窗口路径失效。

## R2A：后处理对照

拟新增实验模块：

```text
tools/reuse/window_memory/
  entity_matcher.py          # 输入现有 entity observations，输出 hypothesis
  event_deduper.py           # 事件相似候选，不删除原事件
  evaluator.py
tools/reuse/videoagent_retrieval/
  projection.py              # Event/Candidate → index document
  lexical_index.py           # 无额外模型 baseline
  upstream_adapter.py        # 可选 VideoAgent/VideoRAG 对照
```

`EntityMatchHypothesis/v1`：left/right entity refs、feature refs、method、score、decision suggestion。
它不是 Character identity；低分或冲突项保持独立。`EventDuplicateHypothesis/v1` 同样只建议归并。

`RetrievalIndexManifest/v1` 绑定 object refs/content hashes、文本投影版本、索引算法/参数和构建顺序。
`RetrievalHit/v1` 只返回已有 ref、rank、score、粗 support、reason。命中后必须重读源对象，索引不能新增事实。

首个 baseline 使用确定性 lexical/BM25 类检索；只有上游语义检索在相同可见输入上提升 Recall@K，
才引入固定 embedding 模型。索引是派生缓存，可删除重建，不新建独立业务数据库。

## R2B：前缀 Window Memory

R2A 通过后，在 Kernel 新增 `WindowMemorySnapshot/v1` 和构建/读取 Command：

| 字段 | 必要性 | 约束 |
|---|---|---|
| `source_set_ref`, `window_refs` | 必需 | 精确已提交输入，不读 latest |
| `known_until` | 必需 | episode/window census 顺序；只含当前窗口之前 |
| `parent_snapshot_ref` | 条件必需 | 同源相邻前缀，首窗口为空 |
| `entity_cards` | 必需，可空 | 已观察特征、别名、last_seen、匹配 hypothesis；未知身份保留 |
| `open_event_refs`, `contradictions` | 必需，可空 | 仅引用前缀已有对象 |
| `selected_refs`, `omitted_refs` | 必需 | ContextSelectionPolicy 的可见预算结果 |
| `policy_ref`, `content_hash` | 必需 | 绑定构建和截断策略 |

扩展现有 `context_pack/models.py`/`selector.py` 时新增 v2 类型和 selector，不修改 v1 serialization。
外部 API context、视频 observation、memory hypothesis 在 prompt 中分组；关系受 `known_from_episode` 限制。
模型可以借 memory 消歧，但 observed claim 仍需当前视频证据。

增强请求顺序为前缀依赖：窗口 N 的 snapshot 只由 `<N` 构成。独立 VLM baseline 仍可并行；
增强路径可按 episode 并行，每集内部按必要前缀处理。窗口 N 变化只使其后依赖 snapshot 标记待重建，
先输出重算计划，不立即付费调用。

## 实现任务

1. 创建视频核对过的同人/异人、换装、遮挡、字幕姓名、跨窗事件标签。
2. 实现 R2A 两个纯 adapter 和指标；对比现有 Character/事件归并及检索。
3. 做未来信息和 context-copy 对抗样本；当前集之后的 API/剧本内容必须不可见。
4. 若有收益，定义 Snapshot v1 Artifact、BuildWindowMemoryCommand、reader/replay 和依赖图。
5. 新增 Context Pack v2 与 VLM prompt/request identity；保留 video-only 与 v1 请求路径。
6. PC WSL 仅重跑选定窗口，比较请求 token、实体一致性、高光召回和下游影响。

## 验证和退出门槛

- 后处理单元：同名异人、换装同人、空特征、冲突特征、跨源 ref、索引陈旧、重复 document。
- 前缀单元：future ref、非相邻 parent、循环、错 episode 顺序、预算省略、v1/v2 混读。
- 集成：修改一个窗口后列出精确失效 snapshot/请求；未选择执行前 provider 调用为零。
- 质量：留出剧实体 pair P/R、异人误合并率、事件重复/遗漏和高光 Recall@K 达到冻结门槛。

R2A 可独立接受；R2B 只有在线增强净收益成立才接 R5。无收益时保留现有 WindowContextPack。
