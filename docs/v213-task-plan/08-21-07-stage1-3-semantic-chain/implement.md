# Implementation

Current code wave: [Stage 1 production preparation](stage1-production-wave.md).
Generic generation persistence and strict draft decoding are delivered before
the real compiler/Command; the inactive prototype is not production authority.

Next compiler wave: [production model and reference binding](stage1-model-binding.md).
This closes the precommit identity model without inventing database artifact IDs;
the actual Graph model, KC evaluators and eight-member Command remain required.

1. 冻结 Keep/Replace/Delete 边界；禁止继续激活现有
   `production_stage1/2/3.py` 的关闭路径。
2. 同一波把 Prompt、Schema、Parser、Decoder、Artifact、Store reader、batch
   finalizer 和 tests 切换为 VLM Semantic Pack v3；保留 Ark streaming、attempt、
   retry/reconcile 与 Window timeline mapping。
3. 把 Source/Window canonical decoder 迁到 Kernel owner，让 source-prep 与 Store
   共用并通过 import firewall；随后 Source preparation 增加 content-bound
   operation grant，Store reader 只返回 verified typed authorization；补
   purpose/source/hash tamper tests。
4. 建立新的 `semantic_chain/{authority,rules,stage1,stage2,stage3}.py` 最小 API，
   不暴露 draft/evaluator witness constructors。
5. 实现 Stage 1 strict-global compiler/Command、8-member ArtifactSet 和显式
   indeterminate-first CoverageAdmission。
6. 实现 Stage 2 committed candidate hypothesis 投影、deterministic Portfolio、
   target freeze 和 5-member ArtifactSet；所有物理可行性 deferred 到 Stage 4。
7. 实现 Stage 3 unpartitioned all-or-nothing batch、per-story Blueprint/Closure/
   Context 和唯一 batch Admission。
8. 增加 exact Stage 1/2/3 output readers、PostgreSQL restart/replay 与
   Pipeline/Agent conformance tests。
9. 迁移两个 Runtime 后，在同一波删除 fixture semantic command、v2 adapter、
   old production façade/dead prototypes 与 production fixture imports。
10. 使用真实剧集一集 Doubao semantic pack 跑通到 admitted Blueprint，证明
    ASR/VAD/物理 endpoint 对 Stage 1–3 不可达；双重独立审查通过后再进入 Stage 4。

## 分波与文件所有权

Wave A 必须先冻结并替换共享契约：

- VLM owner：负责 v3 prompt/schema/parser/decoder/persisted type 和 provider contract
  tests，不修改 Store transaction kernel；
- Reader/authorization owner：只负责 source grant、`store/models.py`、
  `store/postgres.py` 中 Source/Window/VLM committed readers 与 typed-reader tests；
  `media/root_evidence.py`、`media/timed_evidence.py` 归 Stage 4 owner；
- DTO owner：新的 `semantic_chain/authority.py`、`rules.py` 与模型/Schema tests；
  不继续扩展 `production_models.py` façade。

Wave B 在 v3 与 Source authorization 通过后并行：

- Stage 1 owner：Narrative compiler/Command 与 Stage 1 tests；
- Stage 2 owner：Portfolio compiler/Command、draft provider port 与 Stage 2 tests；
- Stage 3 owner：Blueprint/compiler/Command、context closure 与 Stage 3 tests。

最后由唯一 integration owner 修改所有 `__init__.py`、runtime
models/composition/postgres/stage plan、migration 和 conformance tests。不同 Agent
不得同时写 Store、共享导出或 Runtime composition。

真实验收先跑“一集 committed VLM semantic pack → admitted Blueprint”，
再接 Stage 4/Render，最后扩到 45 集。fixture registry、FixtureBeatResolver 和旧
MediaEvidence 只保留测试 oracle，不进入任何生产 composition。
