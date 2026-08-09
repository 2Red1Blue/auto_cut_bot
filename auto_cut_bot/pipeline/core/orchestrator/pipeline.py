"""流水线编排器 — autocut CLI 的入口与调度核心。

职责:
  - 解析 CLI (run 整链 / stage 单步两种子命令);
  - 通过 StageRegistry 发现 Stage 并按 _PIPELINE_ORDER 顺序调度;
  - 用 BusStageAdapter 包装 bus-based 插件, 以
    prepare → execute → validate 生命周期执行;
  - 人工节点: interactive 模式暂停等待人工; auto 模式交给
    orchestrator/auto.py 的决策函数接管;
  - 维护 project.json 检查点, 失败时写 failure.json。

使用 StageContext 替代隐式的 self.config/bus,
支持 checkpoint/attempt 状态恢复。
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from typing import Any

from autocut_core.config import PipelineConfig
from autocut_core.contracts.types import (
    ArtifactBus, Attempt, AttemptStatus, Checkpoint, StageContext, StageStatus,
)
from autocut_core.errors import (
    AutoDecisionError,
    ConfigError,
    ContractViolationError,
    PipelineError,
    StageExecutionError,
    StageNotFoundError,
)
from autocut_core.io import (
    atomic_write_json, file_lock, json_sha256, load_json, record_pipeline_failure, utc_now,
)
from autocut_core.cache import StageCache
from autocut_core.logging import configure_logging, fields, get_logger
from autocut_core.rework import ReworkHistory, ReworkManifest, ReworkResolver, create_manifest, load_manifest
from autocut_core.interactive import InteractiveApproval, extract_items_from_input
from autocut_core.orchestrator.recovery import (
    RecoveryLogger,
    RecoveryPlan,
    RecoveryRecord,
    RecoveryResolver,
    recovery_stage_range,
)
from autocut_core.registry import HUMAN_NODES, StageRegistry, _PHASE_BOUNDARY, _PIPELINE_ORDER
from autocut_core.version import schema_version_of, stage_version_of
from autocut_core.viz.events import (
    EV_ARTIFACT_PUBLISHED,
    EV_HUMAN_WAITING,
    EV_PIPELINE_COMPLETED,
    EV_PIPELINE_STARTED,
    EV_STAGE_COMPLETED,
    EV_STAGE_FAILED,
    EV_STAGE_STARTED,
    EventEmitter,
    JsonlEventEmitter,
    NullEventEmitter,
    install_log_handler,
)

logger = get_logger(__name__)


def main() -> None:
    """autocut CLI 入口 — 解析参数、构建配置、发现 Stage 并调度执行。

    统一异常捕获与退出码约定 (见 errors.py):
      0 成功 (含人工节点正常暂停); 1 一般流水线失败;
      2 error 级合同违规阻断; PipelineError 之外的未预期异常
      记录完整堆栈后按 1 退出。
    """
    configure_logging()
    parser = argparse.ArgumentParser(prog="autocut")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run")
    run_p.add_argument("job_name", nargs="?", default=None,
                       help="Job 名称，默认自动生成时间戳。目录创建在 ./jobs/ 下。")
    run_p.add_argument("--job-root", dest="job_root", type=Path, default=None,
                       help="显式指定 job 目录 (覆盖自动生成的路径)")
    run_p.add_argument("--url", dest="video_urls", nargs="*", default=None,
                       help="视频 URL 列表，写入 job 目录的 video_urls.txt")
    run_p.add_argument("--script", dest="script_path", type=Path, default=None,
                       help="剧本文件路径 (.txt/.docx)，复制到 job 目录")
    run_p.add_argument("--from", dest="from_stage")
    run_p.add_argument("--to", dest="to_stage")
    # 默认值统一为 None — 未显式传入时由 env/yaml/内置默认值接管
    run_p.add_argument("--mode", choices=["interactive", "auto"], default=None)
    run_p.add_argument("--backend", default=None)
    run_p.add_argument("--dry-run", action="store_true")
    run_p.add_argument("--workers", default=None)
    run_p.add_argument("--requests-per-minute", type=int, default=None)
    # 可选可视化后端: 事件落盘 {job_root}/.sd-viz/events.jsonl + 日志桥接
    run_p.add_argument("--viz", action="store_true")
    run_p.add_argument("--trace", action="store_true",
                       help="启用运行时埋点，输出调用覆盖报告")
    run_p.add_argument("--force", action="store_true",
                       help="强制执行所有 Stage, 跳过缓存检查")
    run_p.add_argument("--rework", dest="rework_manifest", type=Path, default=None,
                       help="返工清单 JSON 文件路径 — 从指定 Stage 开始局部重跑")

    stage_p = sub.add_parser("stage")
    stage_p.add_argument("stage_name")
    stage_p.add_argument("job_root", type=Path)
    stage_p.add_argument("--backend", default=None)
    stage_p.add_argument("--dry-run", action="store_true")
    stage_p.add_argument("--viz", action="store_true")

    # viz 子命令: 启动本地观测服务 (服务模块由独立任务实现, 延迟导入)
    viz_p = sub.add_parser("viz")
    viz_p.add_argument("job_root", type=Path)
    viz_p.add_argument("--port", type=int, default=8787)

    # rework 子命令: Shot/Clip 级局部返工
    rework_p = sub.add_parser("rework")
    rework_p.add_argument("job_root", type=Path)
    rework_p.add_argument("--target", dest="target_stage", required=True,
                          help="返工起始 Stage 名称")
    rework_p.add_argument("--clips", dest="target_clip_ids", nargs="*", default=[],
                          help="需重做的片段 ID 列表")
    rework_p.add_argument("--reason", dest="rework_reason", default="",
                          help="返工原因说明")
    rework_p.add_argument("--mode", choices=["interactive", "auto"], default=None)
    rework_p.add_argument("--backend", default=None)
    rework_p.add_argument("--dry-run", action="store_true")
    rework_p.add_argument("--viz", action="store_true")
    rework_p.add_argument("--force", action="store_true")

    args = parser.parse_args()
    if args.command == "viz":
        # 观测服务不参与流水线配置链 — 直接启动, 不走 PipelineConfig.resolve
        from autocut_core.viz.server import serve  # type: ignore[import-not-found]  # server 由独立任务实现

        serve(args.job_root, args.port)
        return

    # ── job_root 自动创建 ─────────────────────────────────────────────────
    if args.command == "run":
        _setup_job_dir(args)

    # 配置加载链: CLI > env > config.yaml > 默认值
    config = PipelineConfig.resolve(args)

    registry = StageRegistry()
    registry.discover()

    # --viz: 构造 JSONL 事件发射器并桥接结构化日志; 未启用时全 no-op
    emitter: EventEmitter | None = None
    if getattr(args, "viz", False):
        emitter = JsonlEventEmitter(args.job_root)
        install_log_handler(emitter)

    # --rework: 加载返工清单, 与 --from/--to 互斥
    rework_manifest: ReworkManifest | None = None
    if getattr(args, "rework_manifest", None) is not None:
        if getattr(args, "from_stage", None) is not None:
            raise ConfigError("--rework 与 --from 不能同时使用")
        if getattr(args, "to_stage", None) is not None:
            raise ConfigError("--rework 与 --to 不能同时使用")
        rework_manifest = load_manifest(args.rework_manifest)

    # rework 子命令: 从 CLI 参数构造返工清单
    if args.command == "rework":
        rework_manifest = create_manifest(
            target_stage=args.target_stage,
            target_clip_ids=args.target_clip_ids,
            rework_reason=args.rework_reason,
        )

    orchestrator = PipelineOrchestrator(
        config, registry, emitter=emitter,
        trace=getattr(args, "trace", False),
        force=getattr(args, "force", False),
        rework_manifest=rework_manifest,
    )
    try:
        if args.command == "run":
            orchestrator.run(from_stage=args.from_stage, to_stage=args.to_stage)
        elif args.command == "stage":
            orchestrator.run_single(args.stage_name)
        elif args.command == "rework":
            orchestrator.run()
    except PipelineError as exc:
        # 已知异常类别 — 按异常携带的退出码退出, 不打印堆栈
        logger.error(
            "流水线失败: %s", exc, extra=fields(error_code=exc.error_code)
        )
        sys.exit(exc.exit_code)
    except Exception as exc:  # 未预期异常 — 记录完整堆栈后按一般失败退出
        logger.exception("流水线未预期异常: %r", exc)
        sys.exit(1)


def _setup_job_dir(args: Any) -> None:
    """自动创建 job 目录并处理 URL/剧本输入。

    1. 如果 --job-root 指定了路径，直接使用并创建
    2. 否则在项目根 ./jobs/{job_name}/ 下创建
    3. job_name 默认用时间戳
    4. --url 参数写入 video_urls.txt
    5. --script 参数复制到 job 目录
    """
    import shutil
    from datetime import datetime

    if args.job_root:
        job_root = Path(args.job_root).expanduser().resolve()
    else:
        job_name = args.job_name or datetime.now().strftime("job-%Y%m%d-%H%M%S")
        project_root = Path(__file__).resolve().parents[2]
        job_root = project_root / "jobs" / job_name

    job_root.mkdir(parents=True, exist_ok=True)

    # 写入 URL 清单
    if args.video_urls:
        url_path = job_root / "video_urls.txt"
        url_path.write_text("\n".join(args.video_urls) + "\n", encoding="utf-8")
        logger.info("video_urls.txt written (%d URLs)", len(args.video_urls))

    # 复制剧本文件
    if args.script_path:
        script_src = Path(args.script_path).expanduser().resolve()
        script_dst = job_root / script_src.name
        if script_src != script_dst:
            shutil.copy2(script_src, script_dst)
        logger.info("script copied: %s", script_dst.name)

    args.job_root = job_root
    logger.info("job root: %s", job_root)


class PipelineOrchestrator:
    """编排器 — 发现 Stage, 构建执行计划, 管理 project.json 状态。"""

    def __init__(
        self,
        config: PipelineConfig,
        registry: StageRegistry,
        emitter: EventEmitter | None = None,
        trace: bool = False,
        force: bool = False,
        rework_manifest: ReworkManifest | None = None,
    ):
        self.config = config
        self.registry = registry
        self.force = force
        # 未启用 --viz 时 NullEventEmitter 全 no-op, 插桩点零开销
        self.emitter: EventEmitter = emitter or NullEventEmitter()
        # --trace: 运行时埋点
        self._trace_enabled = trace
        self._tracer: Any = None
        # Stage 执行缓存 — 延迟初始化, 等 job_root 确定
        self._stage_cache: StageCache | None = None
        # --rework: 返工清单 (局部重跑)
        self._rework_manifest = rework_manifest

    # ── run ───────────────────────────────────────────────────────────

    def run(self, from_stage: str | None = None, to_stage: str | None = None) -> None:
        """按流水线顺序执行 [from_stage, to_stage] 区间的 Stage。

        - dry_run: 只打印将执行的 Stage, 不真正执行;
        - 遇到人工节点: interactive 模式打印续跑命令后退出,
          auto 模式调用对应 auto 决策函数继续。
        - --rework: 返工模式下从 target_stage 开始局部重跑,
          上游 Stage 复用缓存; 返工历史记录到 .sd-cache/rework/。
        """
        if self.config.job_root is None:
            raise ConfigError("job_root 未设置")
        root: Path = self.config.job_root

        order = self.registry.pipeline_order()

        # ── 返工模式 ─────────────────────────────────────────────────
        if self._rework_manifest is not None:
            manifest = self._rework_manifest
            resolver = ReworkResolver()
            history = ReworkHistory(root)

            # 解析需要重执行的 Stage 列表
            rework_stages = resolver.resolve(manifest, order)
            upstream = resolver.upstream_stages(manifest, order)

            # 记录返工历史
            history.record(manifest)

            logger.info(
                "返工模式: target_stage=%s reason=%s rework_id=%s",
                manifest.target_stage,
                manifest.rework_reason,
                manifest.rework_id,
            )
            logger.info(
                "返工范围: %d 个 Stage 重跑 (上游 %d 个 Stage 复用缓存)",
                len(rework_stages), len(upstream),
            )

            # 返回清单中的 target_stage 作为 from_stage
            from_stage = manifest.target_stage
            # 不覆盖显式传入的 to_stage (返工模式下 to_stage 通常为空)

        try:
            start = order.index(from_stage) if from_stage else 0
            end = order.index(to_stage) + 1 if to_stage else len(order)
        except ValueError as exc:
            # 未知 Stage 包装为带明确消息的 StageNotFoundError,
            # 避免裸 ValueError 直接暴露 list.index 内部报错
            missing = (
                from_stage
                if from_stage is not None and from_stage not in order
                else to_stage
            )
            raise StageNotFoundError(
                f"--from/--to 指定的 Stage 不存在于流水线顺序中: {missing}"
            ) from exc

        bus = ArtifactBus(root)
        project = self._load_project(root)

        self.emitter.emit(
            EV_PIPELINE_STARTED,
            order=list(order[start:end]),
            stages=self._stage_topology(order),
        )

        # --trace: 启动运行时埋点
        if self._trace_enabled:
            from autocut_core.telemetry import start_trace
            self._tracer = start_trace(
                report_dir=root / ".sd-cache" / "traces",
            )
            logger.info("trace enabled run_id=%s", self._tracer.run_id)

        for name in order[start:end]:
            # ── pre_build_enabled: 在 window_analysis 前先运行 ⑥-⑪ ──
            if self.config.pre_build_enabled and name == "window_analysis":
                self._run_pre_build_phase(order, root, bus, project)
            # ── skip_source_prep: 跳过已完成的 ①-④ ──
            if self.config.skip_source_prep and name in {
                "source_windows", "source_metadata", "source_script", "asr_transcript",
            }:
                if self._is_source_prep_done(project, name):
                    logger.info(
                        "skip_source_prep: %s 已完成, 跳过",
                        name, extra=fields(stage=name),
                    )
                    continue
                logger.warning(
                    "skip_source_prep: %s 未完成, 将重新执行",
                    name, extra=fields(stage=name),
                )
            if name == _PHASE_BOUNDARY:
                logger.info(
                    "── Phase 2 (创作与生产) ── %s", name,
                    extra=fields(phase="2"),
                )
            if self.config.dry_run:
                logger.info("[dry-run] %s", name)
                continue
            # 控制面检查: 无控制文件时仅一次 is_file 即返回, 不改变既有行为
            from autocut_core.viz import control

            control.check_and_wait(root, name, self.emitter)
            if name in HUMAN_NODES:
                if self.config.mode == "auto":
                    # auto 模式: 人工节点不暂停, 由 auto 决策函数接管;
                    # 失败时在此统一落盘 failure.json (error_code 取自异常,
                    # PipelineError 之外的异常用默认码)
                    recovery_log: list[RecoveryRecord] = []
                    recovery_logger = RecoveryLogger(root)
                    resolver = RecoveryResolver()
                    scope_expansion_done = False

                    while True:
                        try:
                            result = self._run_auto_node(name, root)
                        except Exception as exc:
                            record_pipeline_failure(
                                root,
                                stage=name,
                                error=str(exc),
                                error_code=getattr(exc, "error_code", "stage_failed"),
                            )
                            raise

                        # 检查是否有需要恢复的 Story
                        plan = resolver.analyze_rejection(
                            result,
                            human_node=name,
                            job_root=root,
                            scope_expansion_done=scope_expansion_done,
                        )

                        if plan is None:
                            # 无 rejected — 正常继续
                            break

                        plan.attempt += 1
                        if plan.exhausted:
                            # 重试上限 — 接受损失
                            record = recovery_logger.log_record(
                                plan,
                                outcome="exhausted",
                                details={
                                    "reason": (
                                        f"still rejected after {plan.attempt} "
                                        f"attempt(s) at {name}"
                                    ),
                                },
                            )
                            recovery_log.append(record)
                            logger.warning(
                                "recovery exhausted: %s at %s (attempt %d/%d)",
                                plan.story_ids, name, plan.attempt, plan.max_attempts,
                            )
                            break

                        # 执行恢复
                        recovery_result = self._execute_recovery(
                            plan, root, bus, project, name,
                        )
                        record = recovery_logger.log_record(
                            plan,
                            outcome=recovery_result["outcome"],
                            details=recovery_result.get("details", {}),
                        )
                        recovery_log.append(record)

                        # 标记 scope expansion 已执行 (用于 plan_preflight)
                        if name == "story_plans_preflight" and not scope_expansion_done:
                            scope_expansion_done = True

                        # while True 继续 → 重新执行 _run_auto_node(name) 再判一次

                    # 写审计日志
                    if recovery_log:
                        self._write_recovery_log(root, name, recovery_log)
                    continue
                self._pause_for_human(name, root, bus, project)
                # interactive 模式: 审批完成后 Stage 已由 _pause_for_human
                # 内部调用 _run_stage 执行, 继续推进后续 Stage
                continue
            self._run_stage(name, root, bus, project)

        self.emitter.emit(EV_PIPELINE_COMPLETED)

        # --trace: 落盘调用报告
        if self._tracer is not None:
            from autocut_core.telemetry import stop_trace
            report = stop_trace()
            if report is not None:
                path = report._resolve_report_path()
                never = report.report()["never_called_modules"]
                get_logger(__name__).info(
                    "trace report: %s (%d never-called modules)",
                    path, len(never),
                )
        logger.info("Pipeline complete.")

    def run_single(self, stage_name: str) -> None:
        """单独执行指定 Stage (autocut stage 子命令), 不走整链。"""
        if self.config.job_root is None:
            raise ConfigError("job_root 未设置")
        root: Path = self.config.job_root
        bus = ArtifactBus(root)
        project = self._load_project(root)
        self._run_stage(stage_name, root, bus, project)

    # ── internals ─────────────────────────────────────────────────────

    def _stage_topology(self, order: list[str]) -> list[dict[str, Any]]:
        """收集 pipeline.started 事件的 Stage 拓扑 — contract 信息逐个实例化获取。

        任何实例化/contract 读取失败都只降级为仅含 name 的条目,
        拓扑采集绝不阻断流水线执行。
        """
        stages: list[dict[str, Any]] = []
        for name in order:
            info: dict[str, Any] = {"name": name}
            try:
                stage_cls = self.registry.get(name)
                contract = stage_cls(None).contract if stage_cls else None  # type: ignore[call-arg]
                if contract is not None:
                    info["is_human"] = bool(contract.is_human_node)
                    info["inputs"] = list(contract.input_artifacts)
                    info["outputs"] = list(contract.output_artifacts)
            except Exception:  # noqa: BLE001 — 拓扑缺失不阻断流水线
                pass
            stages.append(info)
        return stages

    def _run_stage(
        self, name: str, root: Path, bus: ArtifactBus, project: dict
    ) -> None:
        """执行单个 Stage 的完整生命周期: prepare → execute → validate。

        执行前先计算输入哈希并查询缓存: 输入未变且上次 completed
        则跳过执行, 产物由 ArtifactBus._restore() 自动恢复。
        校验出 error 级违规时中止; 任何异常先落 failure.json
        再向上抛出, 保证失败现场可追溯。
        """
        from autocut_core.stages.adapter import BusStageAdapter

        stage_cls = self.registry.get(name)
        if stage_cls is None:
            raise StageNotFoundError(f"unknown stage: {name}")

        # 用 BusStageAdapter 包装 bus-based 插件, 暴露 ctx-based 接口
        stage = BusStageAdapter(stage_cls, self.config, bus)

        # 延迟初始化 StageCache (需要 job_root)
        if self._stage_cache is None:
            self._stage_cache = StageCache(root / ".sd-cache")

        # ── 缓存门 ──────────────────────────────────────────────
        inputs_hash = self._compute_stage_inputs_hash(name, stage, bus)
        if self._stage_cache.hit(name, inputs_hash, force=self.force):
            logger.info(
                "stage 缓存命中, 跳过",
                extra=fields(stage=name, inputs_hash=inputs_hash[:12]),
            )
            self.emitter.emit(
                EV_STAGE_COMPLETED,
                stage=name,
                artifacts=[{"name": "(cached)", "sha256": inputs_hash[:12]}],
            )
            return

        cp = self._build_checkpoint(name, project, bus, stage)
        ctx = StageContext(
            job_root=root, config=self.config, checkpoint=cp, inputs={},
        )

        if ctx.is_resume():
            logger.info(
                "stage 断点恢复",
                extra=fields(stage=name, status=cp.status.value),
            )

        logger.info("stage starting", extra=fields(stage=name))
        self.emitter.emit(EV_STAGE_STARTED, stage=name)
        try:
            plan = stage.prepare(ctx)
            result = stage.execute(ctx, plan)
            violations = stage.validate(ctx, result)

            if violations:
                for v in violations:
                    logger.warning(
                        "合同违规 %s: %s",
                        v.rule_id, v.message,
                        extra=fields(stage=name, severity=v.severity),
                    )
                if any(v.severity == "error" for v in violations):
                    raise ContractViolationError(
                        f"Stage {name} contract violations"
                    )

            # 填充 outputs_sha 并记录本次 attempt
            cp.outputs_sha = {
                r.name: r.sha256 for r in result.artifacts if hasattr(r, "sha256")
            }
            attempt = Attempt(
                attempt_id=str(uuid.uuid4()),
                status=AttemptStatus.SUCCESS,
                started_at=utc_now(),
                finished_at=utc_now(),
            )
            cp.attempts.append(attempt)
            cp.status = StageStatus.COMPLETED
            cp.inputs_hash = inputs_hash

            self._update_project(
                root, project, name, "completed", result.artifacts, inputs_hash,
            )
            # 持久化检查点到磁盘
            self._stage_cache.save(cp)
            self.emitter.emit(
                EV_STAGE_COMPLETED,
                stage=name,
                artifacts=[
                    {"name": r.name, "sha256": r.sha256}
                    for r in result.artifacts
                    if hasattr(r, "sha256")
                ],
            )
            for r in result.artifacts:
                if hasattr(r, "sha256"):
                    self.emitter.emit(
                        EV_ARTIFACT_PUBLISHED,
                        stage=name,
                        name=r.name,
                        sha256=r.sha256,
                        path=str(getattr(r, "path", "")),
                    )
        except Exception as exc:
            # 顶层兼顾底: 先落 failure.json 保留失败现场, 再原样上抛
            # (PipelineError 携带 error_code, 其余异常用默认码)
            error_code = getattr(exc, "error_code", "stage_failed")
            logger.error(
                "stage 执行失败: %s", exc,
                extra=fields(stage=name, error_code=error_code),
            )
            record_pipeline_failure(
                root, stage=name, error=str(exc), error_code=error_code
            )
            self.emitter.emit(
                EV_STAGE_FAILED, stage=name, error=str(exc), error_code=error_code
            )
            raise

    def _pause_for_human(
        self, name: str, root: Path, bus: ArtifactBus, project: dict
    ) -> None:
        """interactive 模式遇到人工节点: 逐条审批交互, 不停进程。

        1. 加载上游输入产物, 提取待审批条目
        2. 逐条呈现给用户, 收集 accept/reject 决策
        3. 将结构化决策保存到 ArtifactBus
        4. 执行 Stage (读取决策并产出过滤后的下游产物)
        5. 继续推进后续 Stage
        """
        logger.info("=" * 60)
        logger.info("INTERACTIVE APPROVAL: %s", name)
        logger.info("=" * 60)
        self.emitter.emit(EV_HUMAN_WAITING, stage=name)

        approval = InteractiveApproval(root, name)

        # 加载上游输入产物, 提取待审批条目
        stage_cls = self.registry.get(name)
        if stage_cls is None:
            logger.warning("交互式审批: Stage %s 未注册, 跳过", name)
            return

        stage_instance = stage_cls(self.config)
        tasks = stage_instance.prepare(bus) if bus else []
        items: list[dict[str, Any]] = []
        for task in tasks:
            data = task.payload
            items.extend(extract_items_from_input(data, name))

        if not items:
            # 没有可审批条目 — 直接执行 Stage, 不阻塞
            logger.info("交互式审批: %s 无待审批条目, 直接执行", name)
            self._run_stage(name, root, bus, project)
            return

        # 逐条审批交互
        decisions = approval.present_and_collect(items)
        approval.save_artifact(bus, decisions)

        # 执行 Stage — 读取审批决策, 产出过滤后的下游产物
        self._run_stage(name, root, bus, project)

    # ── pre_build / skip ───────────────────────────────────────────────

    def _pre_build_stages(self) -> list[str]:
        """返回预构建阶段的 Stage 列表。

        精简版: 只构建角色卡片 + 场景索引 (不需要 story_catalog 等创作环节)。
        从 source_script (剧本) 和 source_metadata (API) 提取:
        - 角色名字、身份、关系
        - 场景位置、时间、出场角色
        """
        return [
            "series_registry",   # 角色命名 + 关系推断 (从 DB subjects 表)
            "series_bible",      # 角色画像汇总 (persona, traits, visual_features)
        ]

    def _source_prep_stages(self) -> list[str]:
        """返回源准备阶段的 Stage 列表: ①-④。"""
        return [
            "source_windows",
            "source_metadata",
            "source_script",
            "asr_transcript",
        ]

    def _run_pre_build_phase(
        self, order: list[str], root: Path, bus: ArtifactBus, project: dict,
    ) -> None:
        """在 window_analysis 之前运行精简预构建阶段。

        只构建角色卡片 + 场景索引 (高置信度数据，不会出错):
        - 角色名字、身份、关系 → 注入 VLM prompt 帮助识别画面角色
        - 场景位置、时间 → 注入 VLM prompt 提供剧情背景

        预构建产物标记为 provisional, VLM 分析后更新为 verified。
        """
        pre_build = self._pre_build_stages()
        logger.info(
            "pre_build_enabled: 在 window_analysis 前运行 %s",
            ", ".join(pre_build),
        )
        for name in pre_build:
            if name not in order:
                logger.warning("pre_build Stage %s 不在流水线中, 跳过", name)
                continue
            if name in HUMAN_NODES:
                if self.config.mode == "auto":
                    self._run_auto_node(name, root)
                else:
                    self._pause_for_human(name, root, bus, project)
            else:
                self._run_stage(name, root, bus, project)

    @staticmethod
    def _is_source_prep_done(project: dict, stage_name: str) -> bool:
        """检查 source_prep Stage 是否已完成。

        同时检查 project.json 中的状态和产物是否存在。
        """
        stages = project.get("stages", {})
        if not isinstance(stages, dict):
            return False
        entry = stages.get(stage_name)
        if not isinstance(entry, dict):
            return False
        if entry.get("status") != "completed":
            return False
        return True

    def _run_auto_node(self, name: str, root: Path) -> dict[str, Any]:
        """auto 模式下执行 human node 对应的自动决策（不暂停）。

        通过 auto.py 的 _AUTO_NODE_HANDLERS 注册表分发,
        新增 human node 只需在注册表中添加映射即可, 无需修改此方法。

        Returns:
            auto 决策结果字典, 供 recovery 回路分析 rejection
        """
        from autocut_core.orchestrator.auto import auto_node_handler

        try:
            handler, post_handler = auto_node_handler(name)
        except KeyError:
            raise StageExecutionError(f"no auto handler for human node: {name}")

        dry_run = self.config.dry_run
        logger.info("auto decision", extra=fields(stage=name))
        try:
            result = handler(root, dry_run=dry_run)
            if post_handler is not None and result.get("needs_scope_expansion"):
                result["scope_expansion"] = post_handler(root, dry_run=dry_run)
        except AutoDecisionError as exc:
            raise StageExecutionError(
                f"auto decision failed at {name}: {exc}",
                error_code=exc.error_code,
            ) from exc
        for line in result.get("log_lines", []):
            logger.info("  %s", line)
        return result

    def _execute_recovery(
        self,
        plan: RecoveryPlan,
        root: Path,
        bus: ArtifactBus,
        project: dict,
        current_node: str,
    ) -> dict[str, Any]:
        """执行恢复回路: 回溯到 target_stage, 重跑到 current_node。

        步骤:
          1. 重置 [target_stage, current_node) 区间所有 Stage 状态
          2. 注入 recovery context 到 config
          3. 从 target_stage 重跑到 current_node (不含)
          4. 清除 recovery context

        Returns:
            {"outcome": "recovered" | "error", "details": {...}}
        """
        order = self.registry.pipeline_order()
        target_idx = order.index(plan.target_stage)
        current_idx = order.index(current_node)

        stages_to_reset = order[target_idx:current_idx]
        logger.info(
            "recovery triggered: %s -> %s (attempt %d/%d)",
            plan.story_ids, plan.target_stage, plan.attempt, plan.max_attempts,
        )
        logger.info("  resetting stages: %s", ", ".join(stages_to_reset))

        # 1. 重置 [target_stage, current_node) 区间所有 Stage 的状态
        #    清除 inputs_hash 使 _cache_hit() 返回 False
        stages = project.setdefault("stages", {})
        for stage_name in stages_to_reset:
            prev = stages.get(stage_name, {})
            prev["status"] = "pending"
            prev.pop("inputs_hash", None)
            prev["recovery_attempt"] = plan.attempt
            prev["recovery_trigger"] = plan.trigger_stage

        atomic_write_json(root / "project.json", project)

        if self._stage_cache is not None:
            for stage_name in stages_to_reset:
                self._stage_cache.invalidate(stage_name)

        # 2. 注入 recovery context — Stage 执行时可读取
        self.config.recovery_context = plan  # type: ignore[attr-defined]

        # 3. 从 target_stage 到 current_node (不含) 重新执行
        outcome = "recovered"
        details: dict[str, Any] = {"stages_reset": stages_to_reset}
        try:
            for stage_name in order[target_idx:current_idx]:
                if stage_name in HUMAN_NODES:
                    # 中间又遇到 HUMAN NODE → 递归 auto 决策
                    self._run_auto_node(stage_name, root)
                else:
                    self._run_stage(stage_name, root, bus, project)
        except Exception as exc:
            logger.error(
                "recovery execution failed at stage %s: %s",
                stage_name, exc,
                extra=fields(stage=stage_name),
            )
            outcome = "error"
            details["error"] = str(exc)
            details["failed_stage"] = stage_name
        finally:
            # 4. 清除 recovery context
            self.config.recovery_context = None  # type: ignore[attr-defined]

        logger.info(
            "recovery outcome: %s (stories=%s, attempt=%d)",
            outcome, plan.story_ids, plan.attempt,
        )
        return {"outcome": outcome, "details": details}

    def _write_recovery_log(
        self,
        root: Path,
        stage_name: str,
        records: list[RecoveryRecord],
    ) -> None:
        """将 recovery 记录追加写入 recovery-log.json 并记录汇总日志。

        recovery-log.json 由 RecoveryLogger 维护, 此方法仅做汇总日志输出。
        """
        approved = sum(1 for r in records if r.outcome == "approved")
        recovered = sum(1 for r in records if r.outcome == "recovered")
        exhausted = sum(1 for r in records if r.outcome == "exhausted")
        errors = sum(1 for r in records if r.outcome == "error")

        logger.info(
            "recovery summary for %s: %d record(s) — "
            "approved=%d recovered=%d exhausted=%d errors=%d",
            stage_name, len(records), approved, recovered, exhausted, errors,
        )
        return result

    def _build_checkpoint(
        self, name: str, project: dict,
        bus: ArtifactBus | None = None,
        stage: Any = None,
    ) -> Checkpoint:
        """从 project.json 历史状态 + 上游产物构造 Stage 检查点。

        inputs_sha 从 ArtifactBus 上游产物实时计算;
        outputs_sha / attempts 从 StageCache 磁盘检查点恢复;
        历史状态值非法 (如历史版本写入的 "ready") 时记录警告并
        回退 PENDING, 不让脏数据阻断断点续传。
        """
        stages = project.get("stages", {})
        prev = stages.get(name, {}) if isinstance(stages, dict) else {}
        raw_status = prev.get("status", "pending")
        try:
            status = StageStatus(raw_status)
        except ValueError:
            logger.warning(
                "检查点状态非法, 回退 pending: stage=%s status=%r",
                name, raw_status,
            )
            status = StageStatus.PENDING

        # 计算 inputs_sha: 上游产物 name → sha256 映射
        inputs_sha: dict[str, str] = {}
        if bus is not None and stage is not None:
            contract = getattr(stage, "contract", None)
            if contract is not None:
                for dep in sorted(contract.input_artifacts):
                    artifact = bus.latest(dep)
                    if artifact is not None:
                        inputs_sha[f"{artifact.stage}/{artifact.name}"] = artifact.sha256

        # 从 StageCache 加载历史检查点, 恢复 outputs_sha / attempts
        outputs_sha: dict[str, str] = {}
        attempts: list[Attempt] = []
        inputs_hash = prev.get("inputs_hash", "")
        if inputs_hash and self._stage_cache is not None:
            prev_cp = self._stage_cache.load(name, inputs_hash)
            if prev_cp is not None:
                outputs_sha = prev_cp.outputs_sha
                attempts = prev_cp.attempts

        return Checkpoint(
            stage_name=name,
            status=status,
            inputs_sha=inputs_sha,
            outputs_sha=outputs_sha,
            inputs_hash=inputs_hash,
            attempts=attempts,
            note=prev.get("note", ""),
        )

    def _load_project(self, root: Path) -> dict[str, Any]:
        """加载 project.json; 不存在时返回空状态骨架。"""
        pp = root / "project.json"
        if pp.is_file():
            return load_json(pp)
        return {"schema_version": "1.0", "created_at": utc_now(), "stages": {}}

    # ── 缓存 ────────────────────────────────────────────────────────

    def _compute_stage_inputs_hash(
        self, name: str, stage: Any, bus: ArtifactBus,
    ) -> str:
        """计算 Stage 的输入内容哈希。

        组成: 上游产物 SHA 列表 + Stage 版本 + Schema 版本 + 确定性配置。
        任一项变化 → 哈希不同 → 缓存失效。
        """
        parts: list[str] = []

        # 上游产物 SHA (按 stage/name 排序, 保证确定性)
        contract = getattr(stage, "contract", None)
        if contract is not None:
            for dep in sorted(contract.input_artifacts):
                artifact = bus.latest(dep)
                if artifact is not None:
                    parts.append(f"{dep}:{artifact.sha256}")

        # 版本
        parts.append(stage_version_of(name))
        parts.append(schema_version_of(name))

        # 确定性配置 (model / workers / backend / mode)
        config_parts = [
            f"backend:{self.config.backend}",
            f"workers:{self.config.workers}",
            f"mode:{self.config.mode}",
        ]
        parts.append(json_sha256("|".join(sorted(config_parts))))

        return json_sha256("|".join(parts))

    def _cache_hit(
        self, name: str, inputs_hash: str, project: dict,
    ) -> bool:
        """检查 Stage 缓存是否命中: 上次 completed 且输入哈希一致。"""
        stages = project.get("stages", {})
        prev = stages.get(name, {}) if isinstance(stages, dict) else {}
        if prev.get("status") != "completed":
            return False
        if not inputs_hash:
            return False
        return prev.get("inputs_hash") == inputs_hash

    def _update_project(
        self, root: Path, project: dict, stage: str, status: str,
        refs: list[Any], inputs_hash: str = "",
    ) -> None:
        """Stage 完成后更新 project.json — 记录输出产物 {名称: sha256} 和输入哈希。

        并发安全: 通过文件锁保护写入, 防止并行进程间的 lost update。
        """
        stages = project.setdefault("stages", {})
        entry: dict[str, Any] = {
            "status": status,
            "updated_at": utc_now(),
            "outputs": {
                r.name: r.sha256 for r in refs if hasattr(r, 'sha256')
            },
        }
        if inputs_hash:
            entry["inputs_hash"] = inputs_hash
        stages[stage] = entry
        project["updated_at"] = utc_now()
        project_path = root / "project.json"
        with file_lock(project_path):
            atomic_write_json(project_path, project)


if __name__ == "__main__":
    main()
