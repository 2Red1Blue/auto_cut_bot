# 有效开场帧与无效前导清理规则

## 1. 先区分三个时间点

- `candidate_window_start`：模型或分析窗口给出的候选起点，只是检索范围，不能直接作为成片入点。
- `effective_opening_frame`：观众第一次看到有效剧情动作、有效表情或完整台词起句的最早画面。
- `render_source_start`：最终 Recipe 使用的源片入点，必须等于经过边界核对后的 `effective_opening_frame`，而不是机械沿用候选窗口起点。

## 2. 入点确定规则

1. 对每个冷开场候选，必须连续检查候选起点前后至少 3 秒的原片；候选窗口不足时检查到源片边界。
2. 识别并剔除无叙事功能的前导：闪黑、黑帧、片头包装、Logo、静帧、空镜、无功能走位和上一镜头残留。
3. 若前导是无功能闪黑，入点移动到闪黑结束后的第一帧有效剧情画面；不能用“约 2 秒”或最近整秒代替精确时间码。
4. 若高光是动作，入点取动作开始产生可理解变化的第一帧，而不是动作结果之后，也不是为了追求接触峰值而切进动作中段。皇冠掉落应从皇冠开始脱离/下落的第一帧起，不应保留之前的闪黑，也不应只保留落地后的反应。
5. 若高光是台词，入点必须保留完整词句起句；若动作和台词重叠，取能够同时保住动作预备与台词起句的更早安全边界。
6. 允许保留有明确叙事功能的黑场，但必须标记为 `intentional_story_black` 并提供功能证据；普通闪黑不得以“氛围”名义保留。
7. 必须区分“源片前导闪黑”和“成片冷开场—正文之间的设计黑场”。后者由 Render Recipe 的 `black_separator` 显式生成，不得把它当作源片有效开场，也不得用它掩盖前导未清理。

## 3. 必填审计字段

新生成的 Highlight/Hook 候选应填写：

- `lead_in_artifact`：`none`、`black_flash`、`packaging`、`static_or_frozen`、`non_narrative`、`intentional_story_black` 或 `uncertain`。
- `lead_in_duration_seconds`：无效前导的实际时长，精确到源片时间轴；无前导为 `0`。
- `source_start_is_effective_opening_frame`：源片入点是否已经对齐有效开场帧。
- `effective_opening_frame_note`：说明第一有效帧是什么，例如“皇冠开始脱离手指并下落的第一帧”。

## 4. 门禁与失败码

- `lead_in_artifact` 为 `black_flash`、`packaging`、`static_or_frozen` 或 `non_narrative`，且入点未对齐有效开场帧：`opening_lead_in_artifact_not_trimmed`，阻断并回到边界修复。
- `lead_in_artifact=uncertain` 或缺少足够的连续源片证据：`opening_effective_frame_unresolved`，不得自动批准。
- 入点虽对齐有效画面，但动作/台词从中间开始：继续触发 `opening_action_or_speech_incomplete`，不得以“第一有效帧”覆盖安全边界要求。
- 有效画面前的黑场被声明为故事功能黑场但没有证据：`opening_black_frame_without_narrative_function`，阻断。

## 5. 反例

- 候选范围从 65.000 秒开始，但 65.000–67.000 秒是闪黑；不能把 65.000 秒直接写入 Recipe。
- 为了卡住皇冠落地、巴掌接触或道具砸桌的峰值，切掉动作起势，导致观众只看到结果而不知道发生了什么。
- 把分析窗口的 0 秒、最近整秒或模型的粗略 `lead_in` 文字当作最终切点。
- 片头源片闪黑仍保留，却只检查了成片中人为添加的 `black_separator`。
