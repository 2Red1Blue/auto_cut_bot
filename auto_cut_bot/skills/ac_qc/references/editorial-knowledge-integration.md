# 编导/技术知识库与流水线融合说明

本项目把两份内部知识库拆成“模型提示、确定性门禁、视频 QC 复核”三层，不把整篇 Markdown 原文直接塞进模型上下文。

## 1. 阶段映射

所有阶段先遵守“全剧理解—故事弧—高光联合选择”顺序：高光在窗口分析阶段只是候选；只有 Story Script 同时证明其 primary 归属、因果解释、后续推进和结尾落点，才可成为正式 Teaser。不得先锁定单个高光再倒推主线。

| 知识库判断 | 接入阶段 | 落地方式 | 失败路由 |
|---|---|---|---|
| 单主线、主辅线、起承转合 | Story Script | `editorial_knowledge` 上下文 + 本地 `story_coherence_diagnostics` | `story_script` / `story_plan` |
| 类型、母题、模板、开场卖点 | Registry → Bible → Catalog → Script | `genre_router.py` 读取 Bible 的显式类型与证据，只发送对应 `golden_case_ids`；先建立完整故事弧，再在全剧候选库中比较开场；Arya 不再是全局样例 | `human_review` / `story_script` |
| 不为拼时长加入无功能片段 | Story Script / Plan | 目标时长均为 0 偏好；检测凑时长措辞；Option 只覆盖功能 Span | `story_plan` |
| 原剧情顺序与混剪边界 | Plan | 延续现有 `temporal_relation`、Source 兼容和倒序门禁；两种模式都必须有高光开头 | `story_plan` |
| 线程切换、次线占比、次线因果角色 | Script Preflight | 独立次线切换 ≥2、独立次线 >1/3、独立次线承担 escalation/reveal/payoff；整合型感情支撑线另按状态变化和证据检查 | `story_plan` / `story_script` |
| Hook 冲突显性、信息密度、悬而未决 | Script Preflight / Video QC | 有证据时做结构化强度检查；画面效果交给 Qwen Flow | `story_script` / `review` |
| 冷开场提出问题、回到前因解释、避免机械重复 | Story Script / Plan | 默认 `causal_explanatory_no_reprise`；可显式选择 `causal_explanatory_delayed_reprise` 延后重现高光 | `story_script` / `story_plan` |
| 情绪跳跃、人物/时空迷失 | Video QC | 保留给视频模型，不由本地文本规则臆测 | `review` |
| 吞字、动作不完整 | Boundary QC | 沿用本地双路 VAD 与 Boundary Repair | `boundary_repair` |
| 300 秒不足时继续到片尾 | Story Script / Plan / Final Render | `duration_extension_policy` 锁定当前主线；从最后选段向后顺剪，先到当前集尾，仍不足才从下一集 0 秒继续；达到门槛后到所在集片尾 | 不跨线、不倒回、不重复、不用无功能内容 |
| 开头—中间—结尾连续性 | Story Script / Plan / QC | `continuity_contract` 要求同一主线；回溯只能解释前因/关系/背景/规则，回溯后必须回主线；每个跨段连接必须有桥接类型 | `story_script` / `story_plan` / `review` |
| 结尾落点 | Story Script / Plan | 有同线 Hook 就在 Hook 处结束；没有合法 Hook 才允许当前故事线集尾；禁止凭空制造 Hook 或接未来完整弧 | `story_script` / `story_plan` |

## 2. 当前实现

- 策略源文件：[editorial-knowledge-base.json](editorial-knowledge-base.json)
- 类型路由与黄金样例：[genre_router.py](../scripts/genre_router.py)、`editorial-knowledge/*-v1.json`
- 确定性检查：[editorial_knowledge.py](../scripts/editorial_knowledge.py)
- Story Script 上下文：`prepare_story_stages.py` 的 Script/Plan Context
- Story Script 门禁：`preflight_story_scripts.py`
- 独立技术确认入口：`validate_editorial_contract.py`
- Story QC 显示：`assemble_story_qc.py` 的 Flow 静态检查
- 选片与渲染的原有时间码、Span、音频和哈希合同保持不变。
- 类型一致性检查：`assemble_series_bible.py`、`build_story_portfolio.py`、
  `preflight_story_scripts.py`、`story_approval.py`、`validate_story_artifacts.py` 和
  `validate_story_plans.py` 共同校验 `genre_profile`/`golden_case_ids`。

## 3. 规则边界

本地规则只检查能从结构化字段证明的内容：关系线切换、主线/整合型支撑线分类、因果角色、角色占比、Arc 角色、Hook 字段和凑时长意图。它不替代视频理解，不根据关键词生成剧情，也不改变 Span 或 Clip 时间码。情绪过渡、人物是否看懂、动作是否完整继续由视频 QC 和人工复核负责。整合型支撑线也必须有 Event/Fact/Thread Beat 证据；没有证据时不能靠类型常识补写。

当 Story Plan 基础素材不足 300 秒时，渲染阶段才做已声明合同约束下的向后顺剪延展；优先当前源集尾，仍不足才按相邻集号从 0 秒继续，达到 300 秒后到门槛所在集尾。这个动作不回写原始 Story Script，不引入第二条故事线，也不通过重复、倒回、黑场或无功能内容填充；最终 Recipe 必须记录延展顺序和边界。

## 4. 黄金样例与整合型感情线

项目使用按类型路由的黄金样例。Arya 文件 `references/editorial-golden-case-arya.json`
只属于 `female_rebirth_revenge`；其他类型使用 `references/editorial-knowledge/` 下的
独立适配器。每个适配器同时包含：

- 正确结构：女主重生复仇为 primary，男女主血契感情为 `integrated_support`；
- 必保桥段：背叛台词、选龙后的疗伤/化形/血契承接、解除封印设问后的感情兑现；
- 错误反例：跨线振荡、删除关系状态变化、ep009→ep025 硬跳、红龙结果反接；
- 每个反例的失败码和修复路由。

系统先选择剪辑路径：`montage` 或 `original_chronological`。两条路径都要求高光开头和黄金三秒吸引用户；
原剧情顺剪的首个高光来自原片 mainline，不是未来预览。

在 `montage` 路径下，开场规则分为两种可接受策略：`causal_explanatory_no_reprise`（高光提出结果/危机 →
正文回到更早前因解释 → 解释后产生新推进，正文不重放高光），以及
`causal_explanatory_delayed_reprise`（同样先解释前因并至少完成一次新推进，之后才允许
在稍后位置重放高光）。两种策略都要求开头足够高光、正文与开头存在强因果关系；不能快速
接重复内容。历史 `future_preview_reprise` 只为旧脚本兼容，不作为新脚本策略。
用户此前指出的“片头抽后文、正文完整重复”“开头信息不足”“ep009 无桥接到 ep025”、
“黑龙胜利后接红龙再次胜利”等均作为错误反例或回归断言，不能作为新项目剧情证据。

`integrated_support` 不是“可随意删除的 secondary”。它必须由真实 Event/Fact/Thread Beat 证明，并且在关系状态、主线动机、盟友助力或设问兑现中至少承担一项功能。它不能代替主线的核心转折、主线 Payoff 或结尾 Hook；重复且不改变状态的甜戏才可压缩。

黄金样例中的具体人物、龙名、集号和时间码不能被模型迁移到其他项目。模型只能迁移判断规则；所有新剧情、对白、桥段和时间关系必须重新绑定当前项目证据。

每个类型适配器提供抽象正面模板和错误反例，用于结构迁移测试；之前版本只作为负面回归输入，永远不能作为当前项目的剧情证据。类型未知或低置信度时不加载任何适配器。
