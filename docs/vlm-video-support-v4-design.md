# VLM 视频语义支持 v4 — 修正模型接口，不改物理剪切职责

状态：V4原协议实现、独立审查及数据库回归完成；第四次单集真实调用已完成但语义引用校验未通过。
随后完成prompt6输入/生成结构优化并设为新semantic-only默认值；本轮仅离线验证，
数据库迁移0031及第五次真实调用尚未执行。字段审计见§7–9，本轮实际交付见§10。
v3历史仍按原规则读取，Stage1–3尚不消费V4。
证据：[Mac真实记录](mac-semantic-run-20260828.md)，第三次调用完整JSON、reasoning0；
79处support中34处区间非法，另有合法区间找不到可引用帧。不能统一缩放或裁剪修复。

## 1. 判断

VLM观看完整视频、只提供9个稀疏帧引用、同时要求所有短暂事实都有区间内引用帧，
这三个条件不能保证同时满足。毫秒/短ID减少表达负担，但不补充缺失的帧证据。
因此选择明确区分模型视频观察与帧锚定观察，不把模型语义声明当作物理安全证明。
双Runtime、共享Kernel、SourcePrep/ASR/VAD/精确编译器职责保持不变。

## 2. 版本化模型wire

新增wire schema_version=4与独立prompt/parser strategy。每个support为封闭union：

```json
{
  "support_kind": "video_observation",
  "interval_ms": {"start_ms": 10000, "end_ms": 25000, "uncertainty_ms": 1000},
  "confidence": "0.9"
}
```

`frame_anchored_observation` 分支另外要求非空、唯一的`frame_refs`短alias；
video分支不允许传可误解为帧证明的字段。两类都是模型观察，不是已独立确认的事实。
模型不能填写视频路径、Blob hash、已验收标志或物理端点。上下文由程序绑定精确
WindowManifest、实际上传视频、播放时间原点、time_base和可逆alias表。
frame分支仍须引用已登记帧且至少一帧在区间内，不能自动更换/补入帧。

事实、事件、人物、候选与continuity的语义字段及引用要求不借此删除。
新版完整pack须显式schema_version=4；不转换成伪v3 pack。

## 3. 时间与必要字段

| 字段 | 必要性与用途 |
| --- | --- |
| support_kind | 必需；区分视频观察与帧锚定，不让消费方猜测 |
| interval_ms | 必需；播放起点起算的粗粒度模型定位，不是源物理端点 |
| confidence | 必需；模型置信度，保持canonical decimal字符串，不代表QC许可 |
| frame_refs | 条件必需；仅frame分支，映射到原manifest的完整hash/PTS |
| WindowManifest/上传视频/alias表身份 | 必需；由请求冻结、程序绑定，不让模型自填 |
| 独立通过/安全/发布标志 | 不允许；此阶段无权生成 |

毫秒时间原点是`manifest.timeline_map.proxy_range.start_pts`，不是源时间零点。
用整数/Fraction计算：start向下、end向上落proxy tick；记录量化误差，随后使用现有
ProxyTimelineMap得到粗源区间。绝不使用二进制float推导媒体时间。
可表示上界为真实播放时长的向下取整毫秒值；不足1ms的尾部差额显式属于量化限制，
不据此宣称逐帧覆盖或物理完整性。小于1ms的窗口不支持此wire，不凭空扩展。
负数、bool、浮点数、倒置、超真实时长、负误差、未知alias均拒绝。
不clamp、不扩大事件区间、不按最近帧替换错误引用，不把这次失败输出修成成功。
语义区间的排序、相邻性与交集用原始整数毫秒判断，不用向外取整后的粗PTS：
例如1/12800时钟下相邻[0,1)ms、[1,2)ms可能共享粗tick，但不是语义重叠。
同理，取整制造的交集不能满足event/fact或candidate/event的真实相交约束。

## 4. 兼容与持久化

- 旧`vlm/models.py/parser.py/window.py`及其v3实现bundle hash保持原样；新增模块。
- 原raw响应、失败Receipt、旧request/profile/hash均不重写。
- 新parser在request/profile/reuse identity中显式登记，并绑定新实现、时间/alias策略。
  V4专属`parser_contract_sha256`必须随原请求冻结；读取时只比较，不替换成当前值。
  修改实际解析规则必须使用相应新契约，不能只更新安装摘要后重解释旧请求。
- Provider继续用Ark SDK流式、explicitthinkingdisabled与已有视频Files缓存；
  wire/prompt不同只影响语义身份，不导致相同视频重传。
- Generation解析和已提交重放必须分到同一个新版parser，raw_response hash始终
  指向实际provider bytes，不指向归转换器伪装的v3 JSON。
- 新pack需要独立严格decoder；新Batch策略必须包含parser/wire版本，旧finalizer
  不能把v4成员认成v3。下游若不支持新证据类型，应返回明确unsupported，不静默降级。
- SourcePrep采样不改变时不重写其身份；本修正不声称9帧是密集视觉证明。
- Store沿用不可变Blob/Artifact/Command/Receipt事务，不新增第二套状态系统。
  是否需要增量DDL由真实SQL闭合约束决定，不为版本号先重建数据库。
- 新契约仅用于semantic-only execution profile v10；migration0030增加V4
  parser/prompt/wire/stage组合的SQL约束，不重建库或修改旧run。
- V4 Store从同一Job的精确SourcePrep Receipt/ArtifactSet、content hash及
  provenance恢复上下文，并用原始响应Blob重解析比对，不能仅相信pack自报hash。
  本次不扩展跨Job复用权限。已提交源证据恢复不需要原主机路径存在。
- Stage1–3旧输入reader明确拒绝V4；此时semantic-only完成不等于全流程完成。

## 5. 交付顺序和验收

1. 纯Kernel v4 support/time/alias解码与测试，不修改旧v3源文件。
2. 完整v4 pack/parser/reader及Generation/Batch版本分派，明确消费者边界。
3. prompt/schema/factory、安装authority、复用身份和debug接线。
4. PostgreSQL真实提交/重放、旧v3固定hash回归和独立对抗审查。
5. 新单集真实调用；仍保留失败关闭、有限重试与原始debug。

必须覆盖：非零proxy起点、非整数毫秒time_base、半开区间、量化误差、未知/错帧alias、
视频观察不伪造帧证明、越界/倒置反例、rawhash不被投影覆盖、旧历史逐字节不变。
真实语义仍需与视频抽查；parser通过不能证明模型没有幻觉，更不是ASR/剪切/QC通过。

## 6. 2026-0828部署检查点

- 纯Kernel提交`7980bc52`；Pipeline/Store/profile接线提交`5c0960c5`。
- 更新本机实际安装的Kernel wheel，不只靠pytest的源码路径通过测试。
- 真实`autocut`应用0030前完整备份并经pg_restore验证；六条既有run逐字节不变，
  旧profile全部仍合法，新V4 profile通过SQL校验。未重建库，未改旧Receipt。
- V4实现摘要：`sha256:bd7a642ab2f3bf84dd99ea06297134a284f5e2c0d092b26fb832f9d3d7ccd63f`。Provider 返回的封闭枚举集合允许任意顺序，Parser 在持久化前按注册枚举顺序规范化；重复值和未知值仍拒绝。
- 新execution profile：`sha256:59c09cd593452ca9d33816690da2dc996b0ff69ded5219572f51a18a70f5d518`。
- 全量离线检查点1761passed/218skipped；真实disposable Store39passed、
  profile/run-store218passed；启用新authority后重点回归144passed/1skipped。

## 7. 2026-08-28真实模型输入输出审计

本节是审查结果与待实施建议，不追溯修改任何已冻结请求，也不是删字段许可。
主线程核对原始debug，独立审查者追踪下游消费者；本轮没有额外调用模型或运行SSH。
使用software-architecture-design的字段必要性方法及uncle-bob-craft的职责边界检查，
不采用skill中的旧媒体表设计或让模型直接写数据库的方案。

### 7.1 真实运行与debug位置

- 本次是Mac本地HTTP Pipeline调度、源素材准备与持久化，推理由豆包云端完成，不是Mac本地VLM推理。
- 剧集：42000021919第1集；run_id=`pipeline_run_5629779d3ba346afb94bd7667aa53e1c`。
- 时间：2026-08-28 04:00:23–04:06:02，中国时区；不是仍在执行的调用。
- 模型：doubao-seed-2-1-pro-260628，Ark SDK流式，thinking=disabled。
- Provider状态completed；Pipeline最终denied，Receipt=`0bc7de47-61a5-4237-9b5d-9235ca8f1532`。
- debug根目录：`/Users/liuzx/Downloads/ac-auto-cut-validation/mac-local-run/debug/pipeline_run_5629779d3ba346afb94bd7667aa53e1c/`。
- `source_prep/input.json`、`output.json`：源准备阶段记录。
- `vlm/input.json`、`output.json`：阶段请求/命令和结果，不是完整模型上下文。
- `vlm/model/doubao-ark-responses-stream/vlm_semantic_evidence-762b1725fda403a5c56a/request.json`：
  查看`body.input`与`body.text.format.schema`，才是实际提交SDK的模型请求体（debug副本脱敏并格式化）。
- 同目录`raw-output.bin`：原始UTF-8 JSON响应，不是剪辑视频；`terminal.json`：完整终态、响应与usage。
- capture发生在`responses.create(**body)`前；debug外层的run/调用ID、时间等不是自动附加给模型的提示词。

### 7.2 量化结果

| 项目 | 本次实际值 |
| --- | --- |
| 视频播放上界 | 241320毫秒 |
| 文本提示词 | 2833字符、4517 UTF-8字节 |
| 输出JSON Schema | 紧凑序列化13151 ASCII字符 |
| 模型文本中的完整SHA-256 | 1个，frame_alias_map_sha256 |
| 参考帧ID | 已映射为f0001至f0009；未发送九个完整帧hash |
| 输出中的完整SHA-256 | 0个 |
| 模型返回 | 8实体、48事实、12事件、2候选 |
| 原始输出 | 57473字节 |
| 引用数组 | 137处、3996个引用项；数组值共28229字节，约占原始输出49.1%（不是token占比） |
| 未声明的事实引用 | 3465次；只声明f001–f048，却引用到f500 |
| 时间区间 | 73处，全部满足合法播放范围和start<end |
| 帧锚定 | 71处选择frame_anchored_observation，其中44处没有所引帧落在区间内 |
| 模型自报uncertainty_ms | 73处全部为500 |
| Provider用量 | input34609、output28945、reasoning0、total63554 tokens |

帧不匹配按实际发送的time_ms_floor核对；44处最小距离仍有160ms，不是小于1ms的显示舍入造成。
例：事件e001为[3000,23000)ms，引用的帧却位于0和30160ms。
这说明“所有时间合法”的上一条进度判断成立，但不代表frame_refs也合法。
输入token包括视频与文本/schema；现有debug没有完整可用的分模态用量，不能把34609全部归因于长ID。

当前Schema限制facts最多48条，却没有给fact_refs设置相应长度/编号范围，也无法仅靠静态Schema证明
某个ID确实在本次输出中被声明。限定编号只是第一层，Kernel仍须检查实际集合闭合。
实际原始JSON先输出candidate、continuity，再entities/events/facts，与请求canonical schema顺序一致；
先引用后定义可能增加出错概率，但本次观测不能单独证明其因果性。

### 7.3 后续Stage1仍有未映射长ID（静态发现，非本次调用）

独立审查确认`stage1_draft_prompt_inputs()`直接投影完整entity_id/fact_id/event_id、
window_manifest_sha256与allowed_refs；到SDK前没有另一次短ID映射。
最短路径：`semantic_chain/stage1_draft.py:365–405` →
`pipeline/build_narrative_graph_request.py:138–143` →
`auto_cut_bot/pipeline/vlm/ark_responses_transport.py:158`。
因此不能将“VLM已有局部短ID”泛化为“各阶段都做了ID压缩”。
后续应在Stage1模型请求边界生成调用内短alias，程序保存完整反向映射与输入绑定；
模型返回alias后严格解析回原引用，未知alias拒绝。持久化全局ID保持不变。
当前V4 reader未接Stage1，此处尚未产生本轮真实模型费用。

## 8. 每个输入/输出字段的职责审计

“保留模型”表示有真实语义判断职责；“程序”表示不应要求模型重复生成；“条件”必须明确消费目标。
以下模型输出字段大多是让模型新增观察，并非把已有实体/事实整份输入后再要求原样输出。
持久化结构与模型输出格式可以不同；可推导字段仍可由程序填入内部Artifact，保留来源及版本。

### 8.1 请求与播放上下文

| 字段 | 是否需要输入模型/让模型输出 | 判断 |
| --- | --- | --- |
| input_video.file_id | API需要；不让模型回填 | 豆包实际视频句柄，不能换成虚构短ID；不属于业务引用泄漏 |
| input_text.text | 需要输入 | 任务、局部时间规则、语义限制；删重复说明，不删关键职责边界 |
| role/type | API需要；程序固定 | 不是模型生成任务 |
| model/stream/store/temperature/thinking/max_output_tokens | 请求参数；程序配置 | 不要求模型返回；store支持响应查询，stream支持真实流式 |
| text.format.type/name/strict/schema | API结构约束 | schema有必要，但重复嵌套/枚举和prompt重复说明可精简；不能全移到prompt而放弃结构校验 |
| duration_ms_floor | 需要输入，不回填 | 提供本窗口合法播放上界，程序从真实媒体计算 |
| time_unit | 语义需要，表达只保留一处 | 可写在简短固定指令，不必重复长字符串或回填 |
| frame_time_display | 仅帧引用分支需要 | 整视频观察调用可不传 |
| reference_frames[].frame_ref/time_ms_floor | 条件 | 真正需要模型选择参考帧时才传；当前整视频观察无需强迫提供帧佐证 |
| frame_alias_map_sha256 | 不应输入或输出 | 完整hash无语义价值，保留在程序绑定/debug即可 |
| 全局实体/事实/事件ID、Manifest/Blob/Policy hash、Receipt、主机路径 | 不应输入或输出 | 程序持有；本次模型正文没有这些，只有上行alias-map hash例外 |
| debug外层schema_version/captured_at/operation/provider/调用key | 不输入模型 | 仅本地debug追踪，不应误算为提示词负担 |

### 8.2 通用时间、身份与支持信息

| 输出字段 | 建议职责 | 理由/约束 |
| --- | --- | --- |
| schema_version | 程序/固定wire常量 | 当前parser要求4；新版轻量输出可由版本化适配器注入，不要求模型推理 |
| local_entity_id/local_fact_id/local_event_id/local_candidate_id | 保留短局部引用机制 | 当前ID已短；全局ID由Kernel生成。可改数组索引，但不能事后把不存在的f500猜成某条事实 |
| support.interval_ms.start_ms/end_ms | 保留模型粗定位 | 不能从长ID/文本可靠计算；不是ASR切点 |
| support.interval_ms.uncertainty_ms | 需重新定义，不能伪装测量 | 当前值影响保守时间范围；全填500不证明有±500ms准确度。若改程序策略，须标为策略而非模型测量，不默认0 |
| support.confidence | 条件保留，不能直接删/默认1 | 现有下游覆盖与候选资格实际使用；自报分数不等于校准概率，修改需连消费规则一起评估 |
| support.support_kind | 本整视频调用建议程序固定video_observation | 可限制新wire只允许视频观察；如另有真正帧观察任务再启用帧分支 |
| support.frame_refs | 本整视频调用建议移出模型必选职责 | 不找最近帧、不扩大区间、不把失败的帧声明改成成功视频声明；程序关联时间内帧也不等于独立语义核验 |

### 8.3 entities与facts

| 输出字段 | 建议职责 | 理由/约束 |
| --- | --- | --- |
| entities.entity_kind | 保留模型 | 区分人物、物体、地点、文字载体 |
| entities.display_label | 保留模型 | 未知身份应保持未知，不凭字幕或外部常识编姓名 |
| entities.visual_description | 保留模型，简短 | 后续跨窗口辨认有价值，不从姓名可推导 |
| entities.support | 按8.2 | 最低观察依据；不能将长出现范围误解为每帧都出现 |
| facts.fact_kind | 保留模型 | 区分状态、动作、关系等 |
| facts.subject_ref/object_ref | 保留模型短引用，object可null | “谁对谁/什么做了什么”不是程序按名称可恢复的关系 |
| facts.summary | 保留模型，避免重复 | 原子语义观察的核心内容 |
| facts.support | 按8.2 | 保留独立事实的粗时间，不能只给整集区间 |

### 8.4 events

| 输出字段 | 建议职责 | 理由/约束 |
| --- | --- | --- |
| event_kind/summary | 保留模型 | 事件是对事实的语义组织，不只是复制第一条事实 |
| participant_refs | 保留模型 | 并非引用事实中所有背景实体都应自动变成参与者 |
| fact_refs | 保留模型，必须限量且真实闭合 | 只引用直接支撑该事件的已声明事实，不枚举全表 |
| cause_event_refs | 保留一个方向的模型声明 | 因果不能按先后顺序凭空生成 |
| effect_event_refs | 程序反建 | 当前已强制与cause互逆，无须模型再写一次；程序继续检查环和引用 |
| temporal_mode | 保留模型 | dream/flashback/present等需观察判断，播放顺序不等于叙事时间 |
| open_question | 条件 | 只有事件确实留下悬念才返回；与candidate问题相同可引用，不要求每事件编问题 |
| support | 按8.2 | 事件边与事实时间关系仍需验证，不能靠union自动扩大 |

### 8.5 window_summary与continuity

| 输出字段 | 建议职责 | 理由/约束 |
| --- | --- | --- |
| window_summary.summary | 保留模型，短 | 实际用于EpisodeDigest，具有压缩局部语义价值 |
| dominant_temporal_mode | 条件保留 | 若改按时间段时长计算，须先定义“dominant”就是时长而非叙事主导；否则不能声称等价 |
| window_summary.fact_refs/event_refs | 精简重复层级 | 保留支撑总结的事件，事件未覆盖的事实再单独引用；不能自动全选并宣称全部支撑 |
| window_summary.confidence | 条件保留 | 当前低置信覆盖规则有消费，不是纯装饰 |
| continues_from_previous/starts_mid_event | 程序可推导重复项 | 当前两者强制相等；还等于entry_state_fact_refs是否非空 |
| continues_into_next/ends_mid_event | 程序可推导重复项 | 当前两者强制相等；还等于exit_state_fact_refs是否非空 |
| entry_state_fact_refs/exit_state_fact_refs | 保留模型局部状态判断 | 可据此构建现有四个布尔字段；实际跨窗口连续性仍由邻窗证据验证，不让模型预知没输入的下一集 |
| temporal_segments[].mode/summary/support | 条件，低优先级 | 尚无专门Stage1–3算法消费，随pack进入上下文；删除可能丢失事件间的梦境/回忆信息，不能机械等同events |

### 8.6 candidate_hypotheses与measurements

| 输出字段 | 建议职责 | 理由/约束 |
| --- | --- | --- |
| candidate_kind | 保留模型 | hook/highlight判断；普通片段允许无候选 |
| anchor_event_ref | 保留模型 | 说明候选核心事件 |
| supporting_event_refs/context_event_refs/payoff_event_refs | 保留不同语义角色 | 直接支撑、背景、兑现不是一回事，不应合成无角色的refs全集 |
| anchor_summary | 程序展示投影 | 若定义为anchor event摘要即可直接使用；若需要跨事件总结，应重命名说明独立作用，而非两份必填近义文案 |
| reason | 保留一份模型理由 | 为什么值得选，不是事件摘要的同义改写 |
| open_question | hook条件必需 | 只返回尚未回答的问题，不是每个候选都必需 |
| payoff_or_open_question | 程序展示投影 | hook取已有open_question；highlight展示已有payoff事件内容，无需重复改写 |
| dialogue_excerpt | 条件可选 | 仅简短语义转述，不是逐字ASR或边界证据 |
| editing_modes | 保留模型或经明确定义再推导 | 当前有后续上下文/展示消费；对话与动作可同时存在 |
| narrative_functions | 保留模型 | 当前实际限制Stage3 Beat可用性，不能当冗余tag删除 |
| tags | 条件精简 | 保留确有检索/选择用途的维度；与editing_modes、function不同，不宜盲目全合并 |
| support | 按8.2 | 候选粗时间与事件引用必须相容，不是自动可渲染span |
| measurements[].measurement_kind/value | 条件，只保留有用途的维度 | kind齐备性有门槛；value主要给后续模型/展示，未找到独立数值排序分支，不应为每个候选凑全套评分 |
| measurements[].confidence | 条件保留 | 当前候选资格门槛实际消费，不能默认为通过 |
| measurements[].fact_refs/event_refs | 保留最小相关证据集合 | 优先复用已有事件；无须反复展开事件的全部事实；仍不能删除真正独立证据 |

## 9. 审查结论与实施边界

1. 长ID映射不是本次主要问题：只有一个可移出prompt的alias-map hash；主要问题是冗余引用、
   稀疏帧锚定职责混入整视频观察，以及让模型重复声明程序可推导字段。
2. 第一批可收敛为新prompt/schema：只做整视频观察、移出参考帧与其hash、保留播放上界，
   先定义entity/fact/event再生成candidate，限定引用并继续检查实际声明集合。
   此为审计当时建议；随后按§10实现。不是将旧失败输出删除frame_refs后接纳。
3. 第二批轻量模型输出：移出反向因果、连续性重复布尔值、候选重复文案、程序身份字段。
   必须有版本化、确定性适配及来源标记，不能在当前冻结schema里直接删字段；不自动提升语义置信度。
4. facts最多48且本次恰好48，说明可能触顶，不证明覆盖完备；如窗口信息量超预算，需显式拆窗/报告，
   不能以压缩为由静默漏掉独立事实。
5. 消费证据主要来自已存在的V3 Stage1–3。V4 reader仍在postgres.py:4948–4952明确拒绝，
   本审计不宣称本次已进入故事生成或V4消费链已接通；接通前补齐§7.3的请求局部ID映射。
6. §7–9审计轮只读代码/真实debug及运行125项离线回归（全部通过），仅更新设计审查记录；
   未更改运行协议、未发起第五次付费调用、未覆盖任何原始结果。

## 10. prompt6有界视频观察 — 实施与独立复审（2026-08-28）

### 10.1 已修正的生成职责

实现：[bounded_video_prompt.py](../auto_cut_bot/pipeline/vlm/bounded_video_prompt.py)。
版本为`vlm-semantic-pack-v6-bounded-references`，仍使用冻结的V4 parser。

- 模型文本上下文仅保留`duration_ms_floor`，不再输入参考帧表、alias-map hash、
  PTS或源媒体身份。视频仍通过Ark Files句柄输入，实际视频/Manifest身份由程序绑定。
- 本次生成Schema只允许`video_observation`，移除帧分支及`frame_refs`，
  不要求完整视频观察去伪造稀疏帧证明；不修改任何旧响应。
- 全部实体使用p001–p024（包含人物、物体、地点、文字来源），事实f001–f048、
  事件e001–e024、候选c001–c008；各引用字段按类型约束编号和单数组最大长度。
  原声明预算、必填语义字段、候选条件保持不变，不用任意跨字段总量上限截断证据。
- 最终SDK Schema按实体→事实→事件→总结→连续性→候选排列，先定义再引用；
  请求身份仍使用canonical JSON。顺序是生成引导，不是模型遵循顺序的保证。
- 明确事件/事实和候选直接素材必须相交，背景context可以在候选外；因果双向一致、
  禁止环；continuity只声明当前窗口内状态，不预知未输入的前后集。
- uncertainty是模型自报粗定位估计，不是已校准误差，也不能用于挽救越界、倒置或不相交。
  对重复摘要鼓励简短复用，不通过省略独立事实降低输出量。

### 10.2 接受规则与兼容边界

**生成约束不是新的Kernel接受契约。** prompt6的Schema是原V4有效结构的子集，
原V4 parser仍可以解析合法的旧帧观察、旧局部ID格式。不得据本次优化宣称Kernel新增了
“只接受短ID/只接受视频观察”的规则；如需该规则，必须另行版本化。

Schema只能约束ID词法/范围，无法证明f002本次实际存在。Generation和原始响应重放仍由
冻结V4 parser校验实际引用闭合、时间与语义不变量；没有修复/归一化真实输出的旁路。
schema_version、反向因果、四个连续性布尔值及部分候选展示文案仍按V4保留。
§8列出的程序可推导字段需要独立轻量wire及确定性适配设计，本轮没有静默注入默认值。

Factory/Provider在创建client、claim缓存和上传前校验prompt/parser/digest/schema/adapter组合。
Schema对比保留JSON类型差异，不能利用False==0混过相等判断。
旧V3及prompt5请求在新旧调用交错后，实际SDK请求体未排序的序列化字节仍完全一致。
相同视频和预处理身份仍复用上传缓存；prompt变化只改变语义请求身份。
已冻结run恢复其原profile，不改用当前安装默认prompt，原失败Receipt/debug保留。

新安装资源已选择prompt6，模型仍为doubao-seed-2-1-pro-260628、Ark流式、
thinking=disabled，输出预算仍为32768；ASR/VAD/物理剪辑职责及双Runtime边界未改变。
部署新run前须先应用增量[0031迁移](../packages/autocut-kernel/migrations/0031_vlm_bounded_video_prompt.sql)。
迁移仅增加新prompt组合的SQL验证投影，保留0030及历史行，不重建库、不更新旧profile字节。

### 10.3 量化结果与验证

以第四次真实单集请求的同一视频时长241320ms、同一provider固定前缀进行离线重建比较：

| 文本组成 | 原请求 | prompt6 | 差异 |
| --- | --- | --- | --- |
| input_text UTF-8字节 | 4517 | 3380 | -25.2% |
| input_text字符 | 2833 | 1656 | -41.5% |
| canonical输出Schema字节 | 13151 | 10797 | -17.9% |

这是文本体积，不是供应商计费token。视频输入与fps不变；实际输入/输出token和语义通过率
必须由下一次真实调用确认，不能根据缩短文字宣称总token同比降低。
上一轮3996条引用及3465次未声明事实引用是问题证据，不是已修复输出的测量值。

验证检查点（套件重叠，不累加）：

- 默认接线后pipeline+VLM离线回归：1830 passed / 257 skipped；继续排除既有
  `test_artifact_cache.py` 缺失legacy `autocut_core` 的收集问题。
- 独立设计/代码复审：无P0/P1；最终默认接线专项150 passed。
- 本次所有修改Python文件Ruff通过，5个生产模块BasedPyright为0 errors。
- 新反例覆盖：已在词法范围但未声明的f002、越界ID、旧schema/错parser/digest/adapter、
  JSON布尔数值混淆、旧请求字节保持、仅调用一次后原raw重放及视频缓存复用。
- 0031真实PostgreSQL回归本轮未运行：本机Podman VM已停止，未为此启动8GiB虚拟机。
  已补迁移回归测试，但不能把此前0030或旧fixture验证当作本轮数据库通过证据。

### 10.4 下一实际步骤

1. 数据库启动后验证并应用0031；旧run继续保留原profile。
2. 经确认再从真实pipeline运行同一集prompt6，检查阶段debug中的实际SDK输入、原始输出、
   usage及最终Receipt；先单集成功且语义符合预期，再批量。
3. 单集仍失败时按真实失败原因改进，不能删引用、扩大区间或把frame失败改成video成功。
4. V4 Stage1–3消费接线及其输入局部ID映射、轻量wire的重复字段消除仍是后续任务；
   本轮不宣称故事生成、ASR/VAD、渲染或整条pipeline已跑通。
5. 外部剧集资产只能经[WindowContextPack 设计](vlm-window-context-pack-design.md)的
   不可变快照、显式 episode binding 和反剧透选择器进入后续 prompt v7；不能恢复旧
   `global_context` 注入，也不能向当前 prompt6 追加字段。
