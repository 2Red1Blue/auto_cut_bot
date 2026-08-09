"""集中版本管理。

四层版本模型：
- PIPELINE_VERSION: 整体流水线版本（SemVer）
- SCHEMA_VERSIONS: 各 schema 的版本（dict[str, str]）
- STAGE_VERSIONS: 各 stage 的版本（dict[str, str]）
- REQUEST_SIGNATURE_VERSION: 请求签名版本

版本变更的缓存语义（详见 ARCHITECTURE.md「状态、恢复与缓存设计」）：
- schema_version 或 stage_version 变化 → 对应缓存键自动失效
- 请求签名版本进入语义请求签名，变化后旧模型结果缓存不可复用

仅集中登记 autocut_core/ 与 plugins/ 中的版本常量；
其余脚本中的版本常量在其作为 Stage 插件时逐步加入。
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version

# ── 流水线整体版本（与 pyproject.toml [project].version 同步） ────────────
# 已安装时从包元数据读取；开发模式（未 pip install）回退到字面量，
# 两处必须保持一致（pyproject.toml 是权威来源）。
_FALLBACK_PIPELINE_VERSION = "5.0.0-alpha"

try:
    PIPELINE_VERSION = _package_version("autocut-pipeline")
except PackageNotFoundError:  # 未安装（源码开发模式）
    PIPELINE_VERSION = _FALLBACK_PIPELINE_VERSION


# ── 各 schema 的版本 ──────────────────────────────────────────────────────
# key = schema 名（与 ArtifactBus.put(schema_version=...) / 落盘文件对应）
SCHEMA_VERSIONS: dict[str, str] = {
    # project.json — 流水线状态检查点（io.update_project_stage / orchestrator）
    "project": "1.0",
    # ArtifactBus index.json — 产物索引
    "artifact_index": "1.0",
    # Artifact 默认 schema 版本（contracts.types.Artifact.schema_version）
    "artifact": "1.0",
    # failure.json — 失败记录
    "failure": "1.0",
    # 音频边界策略（contracts.audio_boundary.AudioBoundaryPolicy.policy_version）
    "audio_boundary_policy": "1.4",
    # source_windows 产物（window manifest / batch job）
    "source_windows": "1.0",
}

# ── 各 Stage 的版本 ───────────────────────────────────────────────────────
# key = stage 名（与 registry._PIPELINE_ORDER / contract.stage_name 一致）。
# 只登记已显式声明版本的 Stage; 未登记的 Stage 回退到 PIPELINE_VERSION。
STAGE_VERSIONS: dict[str, str] = {
    # plugins/ac_source_prep/stages/source_windows —
    # 写入 window_analysis batch job 的 stage_version 字段
    "source_windows": "story-first-window-v4-highlight-semantics-v1",
    # plugins/ac_source_prep/stages/source_metadata —
    # Platform API metadata fetch and 8-table DB write
    "source_metadata": "v5-api-metadata-v1",
    # plugins/ac_source_prep/stages/asr_transcript —
    # FunASR Paraformer 转录 + 说话人分离
    "asr_transcript": "v5-funasr-paraformer-v1",
}

# ── 请求签名版本 ──────────────────────────────────────────────────────────
# 与 PIPELINE_VERSION / 相关 policy 版本共同构成语义请求签名；
# 变化后旧的模型结果缓存（.story-cache 等）不得跨版本复用。
REQUEST_SIGNATURE_VERSION = "1.0"


def schema_version_of(name: str) -> str:
    """返回指定 schema 的版本；未登记时回退到 artifact 默认版本。"""
    return SCHEMA_VERSIONS.get(name, SCHEMA_VERSIONS["artifact"])


def stage_version_of(stage: str) -> str:
    """返回指定 Stage 的版本；未登记时回退到流水线整体版本。"""
    return STAGE_VERSIONS.get(stage, PIPELINE_VERSION)


def get_cache_key(stage: str, inputs_hash: str) -> str:
    """构造缓存键: ``{schema_version}/{stage_version}/{stage_name}/{inputs_hash}``。

    schema_version 或 stage_version 变化时, 键前缀随之变化,
    旧缓存条目自然失效（无需显式清理）。
    """
    return "/".join(
        (
            schema_version_of(stage),
            stage_version_of(stage),
            stage,
            inputs_hash,
        )
    )


__all__ = [
    "PIPELINE_VERSION",
    "SCHEMA_VERSIONS",
    "STAGE_VERSIONS",
    "REQUEST_SIGNATURE_VERSION",
    "schema_version_of",
    "stage_version_of",
    "get_cache_key",
]
