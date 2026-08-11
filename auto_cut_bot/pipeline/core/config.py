"""统一配置管理。

优先级（从高到低）:
    CLI 参数 > 环境变量 > config.yaml > 默认值

所有 Stage 通过 PipelineConfig 获取参数,
不再散落环境变量或硬编码常量。

config.yaml 搜索路径优先级:
    显式传入路径 > job_root/config.yaml > 项目根 config.yaml > 默认值
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from autocut_core.logging import get_logger

logger = get_logger(__name__)

# 项目根 = autocut_core/ 的上一级
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── 环境变量映射 ──────────────────────────────────────────────────────────
# env var → (config 字段, 解析器)
_ENV_MAP: dict[str, tuple[str, Any]] = {
    "SD_PIPELINE_MODEL": ("model", str),
    "SD_PIPELINE_API_BASE": ("api_base", str),
    "SD_PIPELINE_API_KEY": ("api_key", str),
    "SD_PIPELINE_WORKERS": ("workers", None),  # int 或 "auto"
    "SD_PIPELINE_BACKEND": ("backend", str),
    "SD_PIPELINE_MODE": ("mode", str),
    "SD_PIPELINE_REQUESTS_PER_MINUTE": ("requests_per_minute", int),
    "SD_PIPELINE_SEMANTIC_RETRIES": ("semantic_retries", int),
    # 保留的历史变量名 (部署环境中已存在的密钥配置)
    "QWEN_AI_API_KEY": ("qwen_api_key", str),
    "ARK_API_KEY": ("ark_api_key", str),
    "SHORT_DRAMA_AI_BACKEND": ("backend", str),
}


def _parse_workers(value: Any) -> int | str:
    """workers 允许整数或字符串 'auto'。"""
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.lower() == "auto":
        return "auto"
    try:
        return int(text)
    except ValueError:
        logger.warning("无法解析 workers=%r, 回退 'auto'", value)
        return "auto"


@dataclass
class PipelineConfig:
    """可变配置容器。推荐通过 ``PipelineConfig.resolve()`` 构建。"""

    # ── 全局 ────────────────────────────────────────────────────────
    backend: str = "qwen"               # qwen | doubao
    mode: str = "interactive"           # interactive | auto
    dry_run: bool = False
    job_root: Path | None = None

    # ── 模型 / API（可覆盖 backends/_base.py 的默认端点与模型名） ───
    model: str | None = None            # 通用模型名覆盖
    api_base: str | None = None         # API 端点覆盖
    api_key: str | None = None          # 通用 API key
    qwen_api_key: str | None = None     # 映射环境变量 QWEN_AI_API_KEY
    ark_api_key: str | None = None      # 映射环境变量 ARK_API_KEY
    backend_base_url: str | None = None        # 覆盖当前 backend 的 base_url
    backend_multimodal_model: str | None = None  # 覆盖多模态模型名
    backend_text_model: str | None = None        # 覆盖文本模型名

    # ── 素材准备 ────────────────────────────────────────────────────
    window_seconds: int = 240
    overlap_seconds: int = 12
    window_min_seconds: int = 150
    window_max_seconds: int = 360
    source_kind: str = "local"          # local | remote

    # ── ASR / 转录 ──────────────────────────────────────────────────
    # asr_mode: 控制 ASR 与 API 字幕的关系
    #   always                  — 始终完整跑 ASR，双源交叉验证（默认）
    #   validate_only           — 仅对 API 字幕做抽样验证（节省时间）
    #   skip_if_api_complete    — API 字幕覆盖率 >80% 时跳过 ASR（不推荐）
    asr_mode: str = "always"
    asr_api_coverage_threshold: float = 0.8  # validate_only / skip_if_api_complete 的阈值
    asr_endpoint: str = "http://localhost:10095/recognition"  # FunASR 自部署端点
    # asr_language: ASR 识别语言, 传给 FunASR 的 language 参数
    #   "zh"  — 中文 (默认, 兼容 paraformer-zh 模型)
    #   "en"  — 英文 (需 paraformer-en 或 sensevoice 模型)
    #   "ja"  — 日文
    #   "ko"  — 韩文
    #   "auto" — 自动检测 (需 sensevoice 等多语言模型)
    #   ""    — 不传 language 参数, 使用 FunASR 服务端默认值
    asr_language: str = ""

    # ── 并发 ────────────────────────────────────────────────────────
    workers: int | str = "auto"
    requests_per_minute: int = 0
    semantic_retries: int = 1

    # ── 剧集 ────────────────────────────────────────────────────────
    episodes_per_chapter: int = 6

    # ── 音频 / QC ───────────────────────────────────────────────────
    audio_boundary_python: Path | None = None
    torch_home: str | None = None

    # ── 扩展 ────────────────────────────────────────────────────────
    extra: dict[str, Any] = field(default_factory=dict)

    # ── DB ──────────────────────────────────────────────────────────
    db_url: str | None = None
    db_schema: str = "autocut"

    # ── 预构建 / 跳过开关 ────────────────────────────────────────────
    pre_build_enabled: bool = False   # 预构建开关: 在 VLM 分析前运行 ⑥-⑪
    skip_source_prep: bool = False    # 跳过源准备阶段: ①-④ 已完成时跳过
    vlm_multisource_arbitration: bool = True  # VLM 多源字幕仲裁: 对比 ASR/API/剧本三源

    @property
    def db_enabled(self) -> bool:
        """DB 可用当且仅当 db_url 非空 (CLI/YAML/env 显式配置)。"""
        return self.db_url is not None

    # ── 构建器 ──────────────────────────────────────────────────────

    @classmethod
    def from_cli(cls, args: Any) -> "PipelineConfig":
        """从 argparse 命名空间填充配置。"""
        init_fields = {f.name for f in cls.__dataclass_fields__.values() if f.init}
        kwargs: dict[str, Any] = {}
        for key in init_fields:
            if hasattr(args, key):
                value = getattr(args, key)
                if value is not None:
                    kwargs[key] = value
        return cls(**kwargs)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "PipelineConfig":
        """从环境变量读取配置（仅覆盖非空变量）。"""
        values = os.environ if environ is None else environ
        kwargs: dict[str, Any] = {}
        for env_name, (field_name, parser) in _ENV_MAP.items():
            raw = values.get(env_name)
            if raw is None or str(raw).strip() == "":
                continue
            if field_name == "workers":
                kwargs[field_name] = _parse_workers(raw)
            elif parser is not None:
                kwargs[field_name] = parser(raw)
        return cls(**kwargs)

    @classmethod
    def from_yaml(cls, path: Path | str) -> "PipelineConfig":
        """从 YAML 文件读取配置。

        依赖 pyyaml；未安装时记录警告并回退到默认值（降级）。
        文件内未知键收入 ``extra``，不报错。
        """
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            logger.warning(
                "未安装 pyyaml, 无法读取 %s — 回退默认配置 (pip install pyyaml)",
                path,
            )
            return cls()

        yaml_path = Path(path).expanduser().resolve()
        if not yaml_path.is_file():
            return cls()
        try:
            with yaml_path.open("r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
        except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
            # YAML 解析/读取失败不应中断流水线 — 回退默认配置
            logger.warning("config.yaml 解析失败 (%s): %s", yaml_path, exc)
            return cls()
        if not isinstance(data, dict):
            return cls()

        field_names = {f.name for f in fields(cls) if f.init}
        kwargs: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        for key, value in data.items():
            if value is None:
                continue
            if key in field_names:
                if key == "workers":
                    kwargs[key] = _parse_workers(value)
                else:
                    kwargs[key] = value
            else:
                extra[key] = value
        if extra:
            kwargs["extra"] = extra
        return cls(**kwargs)

    @classmethod
    def find_yaml(
        cls, job_root: Path | str | None = None
    ) -> Path | None:
        """按优先级搜索 config.yaml:
        job_root/config.yaml > 项目根 config.yaml > None。
        """
        candidates: list[Path] = []
        if job_root is not None:
            candidates.append(Path(job_root).expanduser().resolve() / "config.yaml")
        candidates.append(_PROJECT_ROOT / "config.yaml")
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    @classmethod
    def from_agent_config(cls, config_path: Path | str | None = None) -> "PipelineConfig":
        """从 agent 的 config.json 读取 pipeline 配置段。

        搜索路径:
        1. 显式传入路径
        2. 项目根 auto_cut_bot.config.json
        3. ~/.auto_cut_bot/config.json
        """
        candidates: list[Path] = []
        if config_path is not None:
            candidates.append(Path(config_path).expanduser().resolve())
        # 项目根 (auto_cut_bot 仓库根目录)
        candidates.append(_PROJECT_ROOT.parent.parent / "auto_cut_bot.config.json")
        candidates.append(Path.home() / ".auto_cut_bot" / "config.json")

        for path in candidates:
            if not path.is_file():
                continue
            try:
                import json
                with path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            pipeline_data = data.get("pipeline")
            if not isinstance(pipeline_data, dict):
                continue

            field_names = {f.name for f in fields(cls) if f.init}
            kwargs: dict[str, Any] = {}
            for key, value in pipeline_data.items():
                if value is None:
                    continue
                if key in field_names:
                    if key == "workers":
                        kwargs[key] = _parse_workers(value)
                    else:
                        kwargs[key] = value
            if kwargs:
                return cls(**kwargs)
            break  # pipeline section found but empty — stop searching

        return cls()

    @classmethod
    def resolve(
        cls,
        cli_args: Any = None,
        *,
        env: bool = True,
        yaml_path: Path | str | None = None,
    ) -> "PipelineConfig":
        """按优先级合并全部配置层:
        ``from_cli() > from_env() > from_agent_config() > from_yaml() > 默认值``。

        - cli_args: argparse 命名空间（None 表示无 CLI 层）
        - env: 是否读取环境变量层
        - yaml_path: 显式 YAML 路径；None 时按 job_root/项目根 搜索
        """
        # 1. yaml 层（默认值之上）
        yaml_file: Path | None = None
        if yaml_path is not None:
            yaml_file = Path(yaml_path)
        else:
            job_root = getattr(cli_args, "job_root", None) if cli_args is not None else None
            yaml_file = cls.find_yaml(job_root)
        base = cls.from_yaml(yaml_file) if yaml_file is not None else cls()

        # 2. agent config.json 层覆盖 yaml
        base = _merge(base, cls.from_agent_config())

        # 3. env 层覆盖 agent config
        if env:
            base = _merge(base, cls.from_env())

        # 4. CLI 层覆盖一切
        if cli_args is not None:
            base = _merge(base, cls.from_cli(cli_args))

        return base

    def to_dict(self) -> dict[str, Any]:
        """导出为字典（用于序列化/日志）。"""
        return {
            "backend": self.backend,
            "mode": self.mode,
            "model": self.model,
            "api_base": self.api_base,
            "window_seconds": self.window_seconds,
            "overlap_seconds": self.overlap_seconds,
            "workers": self.workers,
            "requests_per_minute": self.requests_per_minute,
            "episodes_per_chapter": self.episodes_per_chapter,
        }


def _merge(base: PipelineConfig, override: PipelineConfig) -> PipelineConfig:
    """将 override 中相对默认值有变化的字段合并到 base 上。

    判定依据: override 字段值 != 该字段的默认值（dataclass 各层均从默认值
    出发, 只有显式设置的层才会偏离默认值）。
    """
    merged_kwargs: dict[str, Any] = {}
    for f in fields(PipelineConfig):
        if not f.init:
            continue
        default = f.default
        override_value = getattr(override, f.name)
        if f.name == "extra":
            merged = {**base.extra, **override.extra}
            merged_kwargs["extra"] = merged
            continue
        if override_value != default:
            merged_kwargs[f.name] = override_value
        else:
            merged_kwargs[f.name] = getattr(base, f.name)
    return PipelineConfig(**merged_kwargs)
