"""KnowledgeChainV2Stage — 知识链 v2 唯一实现。

v2 架构下直接输出叙事蓝图 (narrative-blueprint.json)，
供制作层 v2 (story_design_v2) 直接消费。

输入: episode_digests + event_cards + highlight_hook_catalog
输出: narrative_blueprint (v2 Schema)

三层流水线:
  Layer1: GlobalSegmenter  — 1 次 LLM，全局分割 + 重要性打分
  Layer2: ChapterProcessor — N×2 次 LLM，逐章 beat 填充 + 实体提取
  Layer3: GlobalAssembler  — 零 LLM，本地汇编 + 严格合并

调试产物 (写入 job_root/kc-v2-debug/):
  layer1_prompt.md          — Layer1 完整 prompt
  layer1_output.json        — Layer1 LLM 原始输出
  layer2_ch{N}_pass1_prompt.md  — 每章 Pass1 prompt
  layer2_ch{N}_pass1_output.json
  layer2_ch{N}_pass2_prompt.md  — 每章 Pass2 prompt
  layer2_ch{N}_pass2_output.json
  layer3_input_summary.json — Layer3 输入摘要
  narrative-blueprint.json  — 最终产物
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from autocut_core import Artifact, ArtifactBus, PipelineConfig, Stage, StageContract, Task
from autocut_core.io import atomic_write_json, update_project_stage
from autocut_core.logging import get_logger
from autocut_core.stages.ports import LLMPort, get_llm_port

from .layer1_global_segmenter import GlobalSegmenter
from .layer2_chapter_processor import ChapterProcessor
from .layer3_global_assembly import GlobalAssembler
from .metrics import MetricsCollector
from .schemas import KnowledgeChainV2ExtraConfig, KnowledgeChainV2Output
from .types import (
    Chapter,
    ChapterOutput,
    EpisodeSummary,
    EventCard,
    GlobalFramework,
    JSONObject,
    JSONValue,
)

logger = get_logger(__name__)


def _json_value(value: object) -> JSONValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in cast(list[object], value)]
    if isinstance(value, Mapping):
        result: JSONObject = {}
        for key, item in cast(Mapping[object, object], value).items():
            if not isinstance(key, str):
                raise TypeError(f"JSON key must be str, got {type(key).__name__}")
            result[key] = _json_value(item)
        return result
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def _json_object(value: object, label: str) -> JSONObject:
    parsed = _json_value(value)
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object")
    return parsed


def _read_json(path: Path) -> JSONValue:
    parsed: object = json.loads(path.read_text(encoding="utf-8"))
    return _json_value(parsed)


def _read_jsonl(path: Path) -> list[JSONObject]:
    records: list[JSONObject] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        parsed: object = json.loads(line)
        records.append(_json_object(parsed, f"{path}:{line_number}"))
    return records


def _string(value: JSONValue | None, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _integer(value: JSONValue | None, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _payload_path(payload: Mapping[str, object], key: str) -> Path:
    value = payload.get(key)
    if isinstance(value, Path):
        return value
    if isinstance(value, str) and value:
        return Path(value)
    raise ValueError(f"task payload {key!r} must be a path")


def _framework(value: object) -> GlobalFramework:
    parsed = _json_object(value, "global_framework")
    chapters = parsed.get("chapters")
    if not isinstance(chapters, list):
        raise ValueError("global_framework.chapters must be an array")
    return cast(GlobalFramework, parsed)


def _chapter_outputs(value: JSONValue | None) -> list[ChapterOutput]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("chapter_outputs must be an array")
    outputs: list[ChapterOutput] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"chapter_outputs[{index}] must be an object")
        outputs.append(cast(ChapterOutput, item))
    return outputs


def _chapters(framework: GlobalFramework) -> list[Chapter]:
    chapters = framework.get("chapters", [])
    if not chapters:
        raise ValueError("global_framework must contain at least one chapter")
    return chapters


def _chapter_int(chapter: Chapter, key: str) -> int:
    value = chapter.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"chapter.{key} must be an integer")
    return value


def _chapter_id(chapter: Chapter, fallback: str) -> str:
    value = chapter.get("chapter_id")
    return value if isinstance(value, str) and value else fallback


def _event_episode(event: EventCard) -> int:
    value = event.get("ep", event.get("episode", 0))
    if isinstance(value, bool):
        raise ValueError("event episode must be an integer")
    return value


def _sequence_length(value: object) -> int:
    return len(cast(list[object], value)) if isinstance(value, list) else 0


class KnowledgeChainV2Stage(Stage):
    """知识链 v2 主 Stage。

    输入: episode_digests, event_cards, highlight_hook_catalog
    输出: narrative_blueprint
    """

    def __init__(self, config: PipelineConfig, llm_port: LLMPort | None = None) -> None:
        super().__init__(config)
        self._llm_port: LLMPort | None = llm_port
        self._debug_dir: Path | None = None
        self._checkpoint_dir: Path | None = None
        self._metrics = MetricsCollector()

    @property
    def llm_port(self) -> LLMPort:
        if self._llm_port is None:
            self._llm_port = get_llm_port()
        return self._llm_port

    @property
    def contract(self) -> StageContract:
        return StageContract(
            stage_name="knowledge_chain_v2",
            input_artifacts=["episode_digests", "event_cards", "highlight_hook_catalog"],
            output_artifacts=["narrative_blueprint"],
            description="知识链v2：三层架构，输出叙事蓝图",
        )

    def prepare(self, bus: ArtifactBus) -> list[Task]:
        ec_art = bus.resolve("event_cards", "event_cards")
        hc_art = bus.resolve("event_cards", "highlight_hook_catalog")
        ed_art = bus.latest("episode_digests")
        if not ec_art or not hc_art or not ed_art:
            raise RuntimeError(
                f"Missing upstream artifacts: event_cards={ec_art}, "
                f"highlight_hook_catalog={hc_art}, episode_digests={ed_art}"
            )
        return [
            Task(
                type="local",
                payload={
                    "episode_digests": ed_art.path,
                    "event_cards": ec_art.path,
                    "catalog": hc_art.path,
                },
            )
        ]

    # ── 流式 LLM 适配器 ───────────────────────────────────────────────────

    async def _llm_adapter(
        self,
        prompt: str,
        *,
        response_format: Mapping[str, str] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """流式 LLM 调用：实时打印生成内容，返回完整文本。"""
        model = (
            getattr(self.config, "model", None) or getattr(self.config, "backend", None) or "qwen"
        )
        requested_format = dict(response_format or {"type": "json_object"})
        requested_tokens = max_tokens or 16384

        loop = asyncio.get_running_loop()

        # 用 stream_llm 获取生成器，在 executor 中消费
        def _stream_and_collect() -> str:
            chunks: list[str] = []
            try:
                gen = self.llm_port.stream_llm(
                    prompt=prompt,
                    model=model,
                    temperature=0.1,
                    max_tokens=requested_tokens,
                    response_format=requested_format,
                    timeout=300.0,
                )
                for chunk in gen:
                    if isinstance(chunk, str):
                        chunks.append(chunk)
                        sys.stderr.write(chunk)
                        sys.stderr.flush()
                        continue
                    usage = chunk.get("usage", {})
                    if usage:
                        logger.info(
                            "    LLM done: input=%s output=%s tokens",
                            usage.get("prompt_tokens", "?"),
                            usage.get("completion_tokens", "?"),
                        )
                    if chunk.get("done"):
                        break
            except Exception as e:
                # stream 清理阶段可能报错（如 concurrency.release），
                # 但只要已收集到内容就继续
                logger.warning("    stream cleanup error (content collected): %s", e)
            finally:
                sys.stderr.write("\n")
                sys.stderr.flush()
            return "".join(chunks)

        content = await loop.run_in_executor(None, _stream_and_collect)
        if not content:
            raise ValueError("LLM stream returned empty content")
        return content

    # ── 调试落盘 ──────────────────────────────────────────────────────────

    def _save_debug(self, filename: str, content: object) -> None:
        """保存调试产物到 kc-v2-debug/ 目录。"""
        if self._debug_dir is None:
            return
        path = self._debug_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            atomic_write_json(path, _json_value(content))
        logger.info("  [debug] saved %s (%d bytes)", filename, path.stat().st_size)

    # ── 数据加载 ──────────────────────────────────────────────────────────

    @staticmethod
    def _load_episode_summaries(digests_path: Path) -> list[EpisodeSummary]:
        summaries: list[EpisodeSummary] = []
        for digest in _read_jsonl(digests_path):
            episode = _integer(digest.get("episode"), _integer(digest.get("ep")))
            summaries.append({"ep": episode, "summary": _string(digest.get("summary"))})
        return sorted(summaries, key=lambda item: item["ep"])

    @staticmethod
    def _load_event_cards(cards_path: Path) -> list[EventCard]:
        events: list[EventCard] = []
        for raw_event in _read_jsonl(cards_path):
            raw_event.setdefault(
                "ep", _integer(raw_event.get("episode"), _integer(raw_event.get("ep")))
            )
            raw_event.setdefault("content", _string(raw_event.get("summary")))
            events.append(cast(EventCard, raw_event))
        return events

    # ── 执行 ──────────────────────────────────────────────────────────────

    def execute(
        self, bus: ArtifactBus, tasks: list[Task], resume: bool = False, force: bool = False
    ) -> list[Artifact]:
        root = self.config.job_root
        if root is None:
            raise RuntimeError("knowledge_chain_v2 requires config.job_root")
        if not tasks:
            raise ValueError("knowledge_chain_v2 requires one prepared task")
        payload = cast(Mapping[str, object], tasks[0].payload)

        # 初始化调试目录
        self._debug_dir = root / "kc-v2-debug"
        self._debug_dir.mkdir(parents=True, exist_ok=True)
        self._checkpoint_dir = root / "kc-v2-checkpoints"
        self._checkpoint_dir.mkdir(parents=True, exist_ok=True)

        # 1. 加载输入
        episode_summaries = self._load_episode_summaries(_payload_path(payload, "episode_digests"))
        event_cards = self._load_event_cards(_payload_path(payload, "event_cards"))

        logger.info(
            "knowledge_chain_v2: %d episodes, %d events, resume=%s, force=%s",
            len(episode_summaries),
            len(event_cards),
            resume,
            force,
        )

        extra_config = KnowledgeChainV2ExtraConfig()
        self._metrics = MetricsCollector()

        # 2. 运行三层流水线
        v2_result = asyncio.run(
            self._run_pipeline(
                episode_summaries,
                event_cards,
                extra_config,
                resume=resume,
                force=force,
            )
        )

        # 3. 序列化输出
        result_dict = _json_object(json.loads(v2_result.model_dump_json()), "narrative_blueprint")

        output_path = root / "narrative-blueprint.json"
        atomic_write_json(output_path, result_dict)

        logger.info(
            "knowledge_chain_v2: %d threads, %d chars, %d beats → %s",
            _sequence_length(result_dict.get("story_threads")),
            _sequence_length(result_dict.get("characters")),
            _sequence_length(result_dict.get("beats")),
            output_path,
        )

        ref = bus.put(
            "narrative_blueprint",
            {"path": str(output_path)},
            stage="knowledge_chain_v2",
        )
        update_project_stage(root / "project.json", "knowledge_chain_v2", "completed")
        return [ref]

    async def _run_pipeline(
        self,
        episode_summaries: list[EpisodeSummary],
        event_cards: list[EventCard],
        extra_config: KnowledgeChainV2ExtraConfig,
        resume: bool = False,
        force: bool = False,
    ) -> KnowledgeChainV2Output:
        """运行完整的三层知识链 v2 流水线，每步落盘 + 流式输出 + 断点续跑。"""

        t_pipeline_start = time.monotonic()
        global_framework: GlobalFramework | None = None
        chapter_outputs: list[ChapterOutput] = []
        start_chapter = 0

        # ── 断点续跑：检查checkpoint ──────────────────────────────────────
        checkpoint_dir = self._checkpoint_dir
        if checkpoint_dir is None:
            raise RuntimeError("checkpoint directory is not initialized")
        checkpoint_path = checkpoint_dir / "checkpoint.json"
        if resume and not force and checkpoint_path.exists():
            try:
                checkpoint = _json_object(_read_json(checkpoint_path), "checkpoint")
                framework_value = checkpoint.get("global_framework")
                if framework_value is None:
                    raise ValueError("checkpoint.global_framework is missing")
                global_framework = _framework(framework_value)
                chapter_outputs = _chapter_outputs(checkpoint.get("chapter_outputs"))
                start_chapter = len(chapter_outputs)
                logger.info(f"Resuming from checkpoint: {start_chapter} chapters already completed")
            except Exception as e:
                logger.warning(f"Failed to load checkpoint, starting from scratch: {e}")
                global_framework = None

        # ── Layer1: 全局分割 ──────────────────────────────────────────────
        t0 = time.monotonic()
        layer1_duration = 0
        if global_framework is None:
            logger.info("knowledge_chain_v2: Layer1 Global Segmenter (streaming)")

            segmenter = GlobalSegmenter(
                llm_provider=self._llm_adapter,
                extra_config=extra_config,
            )
            framework_result: object = await segmenter.run(episode_summaries)
            global_framework = _framework(framework_result)
            layer1_duration = time.monotonic() - t0

            # 保存实际发送给LLM的prompt（从segmenter获取）
            self._save_debug("layer1_prompt.md", segmenter.last_prompt)
            self._save_debug("layer1_output.json", global_framework)
            logger.info(
                "  Layer1 done (%.1fs): %d chapters, %d threads, %d chars",
                layer1_duration,
                len(global_framework.get("chapters", [])),
                len(global_framework.get("global_story_threads", [])),
                len(global_framework.get("global_character_preview", [])),
            )
            self._metrics.data["layer1_duration_ms"] = int(layer1_duration * 1000)
        else:
            logger.info("Loaded Layer1 result from checkpoint, skipping Layer1")

        # ── Layer2: 逐章处理 ──────────────────────────────────────────────
        t1 = time.monotonic()
        logger.info("knowledge_chain_v2: Layer2 Chapter Processor (streaming)")
        processor = ChapterProcessor(
            llm_provider=self._llm_adapter,
            global_framework=global_framework,
            extra_config=extra_config,
            debug_callback=self._save_debug,
            debug_dir=self._debug_dir,  # 传递debug目录用于分章节存储
        )
        chapter_durations: list[JSONObject] = []
        prev_summary = ""
        if chapter_outputs:
            # 从checkpoint恢复rolling context
            for ch_out in chapter_outputs:
                processor.update_rolling_context(ch_out)
            prev_summary = _string(chapter_outputs[-1].get("summary"))[:100]

        framework_chapters = _chapters(global_framework)
        for ch_idx in range(start_chapter, len(framework_chapters)):
            chapter = framework_chapters[ch_idx]
            overlap_eps = self._overlap_episodes(framework_chapters, ch_idx)
            chapter_events = [
                e
                for e in event_cards
                if _chapter_int(chapter, "start_ep")
                <= _event_episode(e)
                <= _chapter_int(chapter, "end_ep")
                and _event_episode(e) not in overlap_eps
            ]

            ch_id = _chapter_id(chapter, f"ch{ch_idx}")
            logger.info(
                "  Chapter %d/%d (%s): %d events",
                ch_idx + 1,
                len(framework_chapters),
                ch_id,
                len(chapter_events),
            )

            t_ch = time.monotonic()
            chapter_out = cast(
                ChapterOutput,
                _json_object(
                    await processor.process_chapter(
                        chapter,
                        chapter_events,
                        ch_idx,
                        prev_summary,
                    ),
                    "chapter_output",
                ),
            )
            ch_duration = time.monotonic() - t_ch
            chapter_durations.append(
                {
                    "chapter_id": ch_id,
                    "duration_ms": int(ch_duration * 1000),
                    "beats": _sequence_length(chapter_out.get("beats")),
                    "characters": _sequence_length(chapter_out.get("character_rollup")),
                }
            )
            chapter_outputs.append(chapter_out)
            processor.update_rolling_context(chapter_out)
            prev_summary = _string(chapter_out.get("summary"))[:100]

            # 每处理完一个章节立即保存checkpoint
            checkpoint = _json_object(
                {
                    "global_framework": global_framework,
                    "chapter_outputs": chapter_outputs,
                    "completed_at": time.time(),
                },
                "checkpoint",
            )
            atomic_write_json(checkpoint_path, checkpoint)

            logger.info(
                "  Chapter %s done (%.1fs): %d beats, %d chars, checkpoint saved",
                ch_id,
                ch_duration,
                _sequence_length(chapter_out.get("beats")),
                _sequence_length(chapter_out.get("character_rollup")),
            )

        layer2_duration = time.monotonic() - t1
        logger.info(
            "  Layer2 done (%.1fs): %d chapters processed", layer2_duration, len(chapter_outputs)
        )
        self._metrics.data["layer2_duration_ms"] = int(layer2_duration * 1000)
        self._metrics.data["chapter_durations"] = _json_value(chapter_durations)

        # ── Layer3: 本地汇编 ──────────────────────────────────────────────
        t2 = time.monotonic()
        logger.info("knowledge_chain_v2: Layer3 Global Assembly (zero-LLM)")

        # 保存 Layer3 输入摘要
        self._save_debug(
            "layer3_input_summary.json",
            {
                "global_framework_keys": list(global_framework.keys()),
                "chapter_count": len(chapter_outputs),
                "total_beats": sum(len(c.get("beats", [])) for c in chapter_outputs),
                "total_characters": sum(
                    len(c.get("character_rollup", [])) for c in chapter_outputs
                ),
                "event_cards_count": len(event_cards),
                "resumed_from_chapter": start_chapter,
            },
        )

        assembler = GlobalAssembler(global_framework, chapter_outputs, event_cards)
        result = assembler.assemble()

        layer3_duration = time.monotonic() - t2
        total_duration = time.monotonic() - t_pipeline_start

        # 补充完整metrics
        runtime_metrics = _json_object(
            {
                "total_duration_ms": int(total_duration * 1000),
                "layer1_duration_ms": self._metric_integer(
                    "layer1_duration_ms", int(layer1_duration * 1000)
                ),
                "layer2_duration_ms": int(layer2_duration * 1000),
                "layer3_duration_ms": int(layer3_duration * 1000),
                "chapter_durations": chapter_durations,
                "resumed": resume and start_chapter > 0,
            },
            "runtime_metrics",
        )
        result = self._with_runtime_metrics(result, runtime_metrics)

        # 保存最终metrics报告
        self._save_debug("metrics_report.json", self._result_metrics(result))
        self._metrics.log_report()

        logger.info("  Layer3 done (%.1fs)", layer3_duration)
        logger.info("  Total pipeline time: %.1fs", total_duration)

        return result

    @staticmethod
    def _overlap_episodes(chapters: list[Chapter], index: int) -> list[int]:
        if index == 0:
            return []
        previous = chapters[index - 1]
        current = chapters[index]
        start = _chapter_int(current, "start_ep")
        end = min(_chapter_int(previous, "end_ep"), _chapter_int(current, "end_ep"))
        return list(range(start, end + 1)) if start <= end else []

    def _metric_integer(self, key: str, default: int) -> int:
        value = self._metrics.data.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else default

    @staticmethod
    def _result_payload(result: KnowledgeChainV2Output) -> JSONObject:
        parsed: object = json.loads(result.model_dump_json())
        return _json_object(parsed, "narrative_blueprint")

    @classmethod
    def _result_metrics(cls, result: KnowledgeChainV2Output) -> JSONObject:
        payload = cls._result_payload(result)
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("narrative_blueprint.metadata must be an object")
        metrics = metadata.get("metrics")
        if not isinstance(metrics, dict):
            raise ValueError("narrative_blueprint.metadata.metrics must be an object")
        return metrics

    @classmethod
    def _with_runtime_metrics(
        cls, result: KnowledgeChainV2Output, runtime_metrics: JSONObject
    ) -> KnowledgeChainV2Output:
        payload = cls._result_payload(result)
        metadata_value = payload.get("metadata")
        metadata = metadata_value if isinstance(metadata_value, dict) else {}
        metrics_value = metadata.get("metrics")
        metrics = metrics_value if isinstance(metrics_value, dict) else {}
        metrics.update(runtime_metrics)
        metadata["metrics"] = metrics
        payload["metadata"] = metadata
        return KnowledgeChainV2Output.model_validate(payload)

    def validate(self, bus: ArtifactBus, refs: list[Artifact]) -> bool:
        try:
            # 直接从 job_root 读取，不依赖 bus（避免 artifact 注册时序问题）
            root = self.config.job_root
            if root is None:
                logger.error("knowledge_chain_v2 requires config.job_root")
                return False
            path = root / "narrative-blueprint.json"
            if not path.exists():
                logger.error("narrative_blueprint file missing: %s", path)
                return False
            data = _json_object(_read_json(path), "narrative_blueprint")
            required = ("story_threads", "characters", "beats")
            missing = [field for field in required if field not in data]
            if missing:
                logger.error("narrative_blueprint missing required fields: %s", missing)
                return False
            output = KnowledgeChainV2Output.model_validate(data)
            if not (output.story_threads or output.characters or output.beats):
                logger.error("narrative_blueprint has no narrative content")
                return False
            logger.info(
                "validation OK: %d threads, %d chars, %d beats",
                len(output.story_threads),
                len(output.characters),
                len(output.beats),
            )
            return True
        except Exception as e:
            logger.error("knowledge_chain_v2 validation failed: %s", e)
            return False
