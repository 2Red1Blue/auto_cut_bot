"""统一配置管理。

优先级（从高到低）:
    CLI 参数 > 环境变量 > config.yaml > 默认值

所有 Stage 通过 PipelineConfig 获取参数,
不再散落环境变量或硬编码常量。

config.yaml 搜索路径优先级:
    显式传入路径 > job_root/config.yaml > 项目根 config.yaml > 默认值
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None  # type: ignore[assignment]

from autocut_core.logging import get_logger

logger = get_logger(__name__)

# 项目根 = autocut_core/ 的上一级
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── 环境变量映射 ──────────────────────────────────────────────────────────
# env var → (config 字段, 解析器)
_ENV_MAP: dict[str, tuple[str, Any]] = {
    "AC_PIPELINE_MODEL": ("model", str),
    "AC_PIPELINE_API_BASE": ("api_base", str),
    "AC_PIPELINE_API_KEY": ("api_key", str),
    "AC_PIPELINE_WORKERS": ("workers", None),  # int 或 "auto"
    "AC_PIPELINE_BACKEND": ("backend", str),
    "AC_PIPELINE_MODE": ("mode", str),
    "AC_PIPELINE_REQUESTS_PER_MINUTE": ("requests_per_minute", int),
    "AC_PIPELINE_SEMANTIC_RETRIES": ("semantic_retries", int),
    # 保留的历史变量名 (部署环境中已存在的密钥配置)
    "QWEN_AI_API_KEY": ("qwen_api_key", str),
    "ARK_API_KEY": ("ark_api_key", str),
    "AC_PIPELINE_BACKEND": ("backend", str),  # generic alias
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
    backend: str | None = None         # must be configured by deployment (e.g. "qwen", "openai", "anthropic")
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
    asr_endpoint: str = ""             # ASR endpoint (e.g. FunASR http://localhost:10095/recognition); empty = no ASR
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

    # ── VAD (Voice Activity Detection) ─────────────────────────────
    # 是否启用 Demucs+Silero VAD 语音感知 fusion（优先于 ffmpeg silencedetect）
    vad_enabled: bool = False
    # VAD venv 的 Python 解释器路径（含 demucs + silero-vad + torch）
    vad_python: Path | None = None
    # VAD venv 目录路径（用于定位 demucs CLI）
    vad_venv: str = ".venv-audio-boundary"
    # torch device: cpu | mps | cuda
    vad_device: str = "cpu"
    # VAD 检测阈值（覆盖 AudioBoundaryPolicy 默认值，fusion 场景推荐 0.25）
    vad_threshold: float = 0.25

    # ── 扩展 ────────────────────────────────────────────────────────
    extra: dict[str, Any] = field(default_factory=dict)

    # ── 剧集标识 ────────────────────────────────────────────────────
    book_id: str | None = None  # 剧集 ID，用于 DB 存储和快照

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
    def from_env(
        cls,
        environ: dict[str, str] | None = None,
        env_map: dict[str, tuple[str, Any]] | None = None,
    ) -> "PipelineConfig":
        """从环境变量读取配置（仅覆盖非空变量）。

        env_map: 可选的自定义环境变量映射，格式同 _ENV_MAP。
                 若未提供则使用默认 _ENV_MAP。
        """
        values = os.environ if environ is None else environ
        mapping = env_map if env_map is not None else _ENV_MAP
        kwargs: dict[str, Any] = {}
        for env_name, (field_name, parser) in mapping.items():
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
    def from_toml(cls, path: Path | str) -> "PipelineConfig":
        """从 TOML 文件读取配置。

        Python 3.11+ 使用内置 tomllib 解析；
        低版本 Python 降级回退到 from_yaml()。
        [env] 节中的映射存入 extra["_toml_env_map"]，
        供 resolve() 传递给 from_env()。
        """
        if tomllib is None:
            logger.warning(
                "tomllib 不可用 (Python < 3.11), 尝试回退 from_yaml: %s",
                path,
            )
            return cls.from_yaml(path)

        toml_path = Path(path).expanduser().resolve()
        if not toml_path.is_file():
            return cls()
        try:
            with toml_path.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("config.toml 解析失败 (%s): %s", toml_path, exc)
            return cls()
        if not isinstance(data, dict):
            return cls()

        # 提取 [env] 节
        env_section: dict[str, str] = {}
        if "env" in data and isinstance(data["env"], dict):
            env_section = data.pop("env")

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

        # 将 [env] 映射存入 extra 供 resolve() 使用
        if env_section:
            kwargs.setdefault("extra", {})
            kwargs["extra"]["_toml_env_map"] = env_section
        if extra:
            kwargs.setdefault("extra", {})
            kwargs["extra"].update(extra)
        return cls(**kwargs)

    @classmethod
    def from_agent_config(
        cls, path: Path | str | None = None
    ) -> "PipelineConfig":
        """从 auto_cut_bot agent 配置 JSON 读取 pipeline 配置。

        搜索优先级:
            显式路径 > 项目根 auto_cut_bot.config.json > ~/.auto_cut_bot/config.json。
        读取 JSON 中的 "pipeline" 节，不存在或为空时返回默认配置。
        健壮处理文件缺失、JSON 无效等异常情况（降级到默认配置）。
        """
        candidates: list[Path] = []
        if path is not None:
            candidates.append(Path(path).expanduser().resolve())
        # 项目根 = _PROJECT_ROOT 往上两级 (packages/autocut-core -> auto_cut_bot)
        repo_root = _PROJECT_ROOT.parent.parent
        candidates.append(repo_root / "auto_cut_bot.config.json")
        candidates.append(Path.home() / ".auto_cut_bot" / "config.json")

        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                with candidate.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    "agent config 读取/解析失败 (%s): %s", candidate, exc
                )
                continue

            if not isinstance(data, dict):
                continue
            pipeline_data = data.get("pipeline")
            if not isinstance(pipeline_data, dict):
                continue

            # 构建 PipelineConfig
            field_names = {f.name for f in fields(cls) if f.init}
            kwargs: dict[str, Any] = {}
            extra: dict[str, Any] = {}
            for key, value in pipeline_data.items():
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

        return cls()

    @classmethod
    def from_pyproject(
        cls, path: Path | str | None = None
    ) -> "PipelineConfig":
        """从 pyproject.toml 的 [tool.pipeline] 段读取配置。

        搜索优先级:
            显式路径 > _PROJECT_ROOT.parent.parent/pyproject.toml
        读取 TOML 中 [tool.pipeline] 节，[tool.pipeline.env] 作为 env_map。
        """
        candidates: list[Path] = []
        if path is not None:
            candidates.append(Path(path).expanduser().resolve())
        repo_root = _PROJECT_ROOT.parent.parent
        candidates.append(repo_root / "pyproject.toml")

        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                import tomllib
                with open(candidate, "rb") as handle:
                    data = tomllib.load(handle)
            except Exception:
                continue

            if not isinstance(data, dict):
                continue
            pipeline_data = data.get("tool", {}).get("pipeline")
            if not isinstance(pipeline_data, dict):
                continue

            field_names = {f.name for f in fields(cls) if f.init}
            kwargs: dict[str, Any] = {}
            extra: dict[str, Any] = {}
            for key, value in pipeline_data.items():
                if key == "env":
                    if isinstance(value, dict):
                        extra["_toml_env_map"] = dict(value)
                    continue
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

        return cls()

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
    def find_toml(
        cls, job_root: Path | str | None = None
    ) -> Path | None:
        """按优先级搜索 config.toml:
        job_root/config.toml > 项目根 config.toml > None。
        """
        candidates: list[Path] = []
        if job_root is not None:
            candidates.append(Path(job_root).expanduser().resolve() / "config.toml")
        candidates.append(_PROJECT_ROOT / "config.toml")
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    @classmethod
    def resolve(
        cls,
        cli_args: Any = None,
        *,
        env: bool = True,
        yaml_path: Path | str | None = None,
    ) -> "PipelineConfig":
        """按优先级合并全部配置层:
        ``from_cli() > from_env() > from_toml()|from_yaml() > 默认值``。

        config.toml 优先于 config.yaml：
        - 先搜索 config.toml，找到则使用 from_toml() 读取
        - config.toml 中的 [env] 节映射会传递给 from_env()
        - 未找到 config.toml 时回退到 config.yaml

        - cli_args: argparse 命名空间（None 表示无 CLI 层）
        - env: 是否读取环境变量层
        - yaml_path: 显式 TOML/YAML 路径；None 时按 job_root/项目根 搜索
        """
        # 确定 job_root
        job_root: Path | str | None = None
        if cli_args is not None:
            job_root = getattr(cli_args, "job_root", None)

        # 1. pyproject.toml [tool.pipeline] — 项目级默认配置 (仅隐式搜索时)
        base = cls()
        if yaml_path is None:
            base = _merge(base, cls.from_pyproject())
            base = _merge(base, cls.from_agent_config())

        # 3. 文件层（覆盖 pyproject.toml）: config.toml 优先于 config.yaml
        file_config: Path | None = None
        use_toml = False
        if yaml_path is not None:
            file_config = Path(yaml_path)
            use_toml = file_config.suffix == ".toml"
        else:
            file_config = cls.find_toml(job_root)
            if file_config is not None:
                use_toml = True
            else:
                file_config = cls.find_yaml(job_root)

        if use_toml and file_config is not None:
            base = _merge(base, cls.from_toml(file_config))
        elif file_config is not None:
            base = _merge(base, cls.from_yaml(file_config))

        # 4. env 层覆盖文件层
        if env:
            # 若 TOML 提供了 [env] 映射，构建自定义 env_map 传递给 from_env()
            toml_env_map: dict[str, tuple[str, Any]] | None = None
            if use_toml and base.extra.get("_toml_env_map"):
                raw_map: dict[str, str] = base.extra["_toml_env_map"]
                toml_env_map = {}
                for env_var, field_name in raw_map.items():
                    toml_env_map[env_var] = (field_name, str)
            base = _merge(base, cls.from_env(env_map=toml_env_map))

        # 5. CLI 层覆盖一切
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
