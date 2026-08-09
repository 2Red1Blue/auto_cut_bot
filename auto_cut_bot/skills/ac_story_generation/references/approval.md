# Story Script 人工审批

## 状态

- `pending`：等待决定。
- `approved`：批准进入后续原片编排。
- `rejected`：本轮不使用。
- `revision_requested`：要求修改当前脚本。
- `merge_with`：与目标 Story 合并并生成新 ID。
- `split_requested`：拆成多个新 Story。

## 命令

批准：

```bash
python3 /absolute/skill/scripts/story_approval.py decide \
  /absolute/job/story-approval.json story-001 approved \
  --notes "人物关系和局部兑现成立"
```

`partial` Story 必须显式接受风险：

```bash
python3 /absolute/skill/scripts/story_approval.py decide \
  /absolute/job/story-approval.json story-001 approved \
  --accept-risks \
  --notes "已复核 Teaser 返回点和 Hook 边界，接受当前风险"
```

禁止批准 `not_feasible` Story。先使用 `revision_requested`、`merge_with`、
`split_requested` 或 `rejected` 处理。

要求修改：

```bash
python3 /absolute/skill/scripts/story_approval.py decide \
  /absolute/job/story-approval.json story-002 revision_requested \
  --notes "背景过长，需更快进入核心冲突"
```

检查：

```bash
python3 /absolute/skill/scripts/story_approval.py status \
  /absolute/job/story-approval.json
```

审批文件确定性维护：

- `selected_story_ids`
- `fulfillment_status=awaiting_decisions|ready`

只有所有实际 Primary 都得到终态决定，且文件达到 `ready`，才能正常进入 Story
Evidence Retrieval。`pending`、`revision_requested`、`merge_with` 和
`split_requested` 都是未决状态；`approved` 与 `rejected` 是终态决定。后续只处理实际
批准的 Story，不再用固定数量判断审批是否完成。

## 哈希绑定

`approved` 同时绑定当前 Story Script SHA-256 和 Story Portfolio SHA-256。脚本文件变化后，
只让该 Story 的审批 stale；Portfolio、Primary/Reserve 或生产槽位变化后，让受旧 Portfolio
约束的审批 stale，并重新生成/预检脚本。

Story Script 阶段的 Reserve 补位不改写原 Portfolio。审批初始化按“原 Primary +
replenishment 中已尝试晋级的 Reserve”归账；每个晋级 Script 还必须绑定稳定的
promotion fingerprint。只进入 Reserve 列表但从未晋级的 Story 不进入审批。

审批文件同时冻结预检时的：

- `feasibility_status`
- 预计可用原片上下界
- `material_risks`
- 是否由审核人显式接受风险

## 审批后的阶段边界

审批文件初始化后先暂停等待人工决定。审批出口达到 `ready` 后，允许依次生成
Story Evidence Packet 和 Span Candidate；不得继续生成 Story Plan、QC、转场或 MP4。
