# VLM 视频语义支持 v4 — 修正模型接口，不改物理剪切职责

状态：详细设计/实施中，**尚未启用新模型调用**。v3历史仍按原规则读取。
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

## 4. 兼容与持久化

- 旧`vlm/models.py/parser.py/window.py`及其v3实现bundle hash保持原样；新增模块。
- 原raw响应、失败Receipt、旧request/profile/hash均不重写。
- 新parser在request/profile/reuse identity中显式登记，并绑定新实现、时间/alias策略。
- Provider继续用Ark SDK流式、explicitthinkingdisabled与已有视频Files缓存；
  wire/prompt不同只影响语义身份，不导致相同视频重传。
- Generation解析和已提交重放必须分到同一个新版parser，raw_response hash始终
  指向实际provider bytes，不指向归转换器伪装的v3 JSON。
- 新pack需要独立严格decoder；新Batch策略必须包含parser/wire版本，旧finalizer
  不能把v4成员认成v3。下游若不支持新证据类型，应返回明确unsupported，不静默降级。
- SourcePrep采样不改变时不重写其身份；本修正不声称9帧是密集视觉证明。
- Store沿用不可变Blob/Artifact/Command/Receipt事务，不新增第二套状态系统。
  是否需要增量DDL由真实SQL闭合约束决定，不为版本号先重建数据库。

## 5. 交付顺序和验收

1. 纯Kernel v4 support/time/alias解码与测试，不修改旧v3源文件。
2. 完整v4 pack/parser/reader及Generation/Batch版本分派，明确消费者边界。
3. prompt/schema/factory、安装authority、复用身份和debug接线。
4. PostgreSQL真实提交/重放、旧v3固定hash回归和独立对抗审查。
5. 新单集真实调用；仍保留失败关闭、有限重试与原始debug。

必须覆盖：非零proxy起点、非整数毫秒time_base、半开区间、量化误差、未知/错帧alias、
视频观察不伪造帧证明、越界/倒置反例、rawhash不被投影覆盖、旧历史逐字节不变。
真实语义仍需与视频抽查；parser通过不能证明模型没有幻觉，更不是ASR/剪切/QC通过。
