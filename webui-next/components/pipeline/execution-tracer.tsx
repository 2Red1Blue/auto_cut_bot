"use client";

import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";
import {
  Loader2,
  CheckCircle2,
  XCircle,
  Clock,
  SkipForward,
  Wifi,
  WifiOff,
  AlertTriangle,
  Timer,
} from "lucide-react";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Ordered list of all 22 pipeline stages. */
const PIPELINE_STAGES: string[] = [
  "book_fetch",
  "metadata_parse",
  "source_prep",
  "source_validate",
  "chapter_split",
  "chapter_detect",
  "scene_detect",
  "scene_analyze",
  "shot_detect",
  "shot_classify",
  "audio_extract",
  "audio_transcribe",
  "subtitle_generate",
  "subtitle_align",
  "highlight_detect",
  "highlight_rank",
  "clip_generate",
  "clip_filter",
  "transition_apply",
  "effect_apply",
  "render",
  "output_validate",
];

/** Human-readable labels for each stage. */
const STAGE_LABELS: Record<string, string> = {
  book_fetch: "Book Fetch",
  metadata_parse: "Metadata Parse",
  source_prep: "Source Preparation",
  source_validate: "Source Validation",
  chapter_split: "Chapter Split",
  chapter_detect: "Chapter Detection",
  scene_detect: "Scene Detection",
  scene_analyze: "Scene Analysis",
  shot_detect: "Shot Detection",
  shot_classify: "Shot Classification",
  audio_extract: "Audio Extraction",
  audio_transcribe: "Audio Transcription",
  subtitle_generate: "Subtitle Generation",
  subtitle_align: "Subtitle Alignment",
  highlight_detect: "Highlight Detection",
  highlight_rank: "Highlight Ranking",
  clip_generate: "Clip Generation",
  clip_filter: "Clip Filtering",
  transition_apply: "Transition Apply",
  effect_apply: "Effect Apply",
  render: "Render",
  output_validate: "Output Validation",
};

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type StageStatus = "pending" | "running" | "completed" | "failed" | "skipped";

interface StageState {
  name: string;
  status: StageStatus;
  startedAt: number | null; // epoch ms
  completedAt: number | null; // epoch ms
  error: string | null;
  artifacts: string[];
}

interface SSEEvent {
  type: "stage_started" | "stage_completed" | "stage_failed" | "stage_skipped" | "progress" | "job_complete" | "job_failed" | "job_error";
  stage?: string;
  progress?: number;
  error?: string;
  artifacts?: string[];
  timestamp?: number;
}

interface ExecutionTracerProps {
  /** The pipeline job / session ID to subscribe to SSE events for. */
  jobId: string;
  className?: string;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatDuration(ms: number): string {
  if (ms <= 0) return "--";
  const seconds = Math.floor(ms / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  if (hours > 0) {
    return `${hours}h ${minutes % 60}m ${seconds % 60}s`;
  }
  if (minutes > 0) {
    return `${minutes}m ${seconds % 60}s`;
  }
  return `${seconds}s`;
}

function formatLabel(name: string): string {
  return STAGE_LABELS[name] ?? name;
}

/** Build the initial stage map from the ordered list. */
function buildInitialStages(): Map<string, StageState> {
  const map = new Map<string, StageState>();
  for (const name of PIPELINE_STAGES) {
    map.set(name, {
      name,
      status: "pending",
      startedAt: null,
      completedAt: null,
      error: null,
      artifacts: [],
    });
  }
  return map;
}

// ---------------------------------------------------------------------------
// Status badge variant mapping
// ---------------------------------------------------------------------------

function stageBadgeVariant(status: StageStatus): "default" | "secondary" | "destructive" | "outline" {
  switch (status) {
    case "completed":
      return "default";
    case "running":
      return "secondary";
    case "failed":
      return "destructive";
    case "skipped":
      return "outline";
    default:
      return "outline";
  }
}

function stageStatusLabel(status: StageStatus): string {
  switch (status) {
    case "pending":
      return "Pending";
    case "running":
      return "Running";
    case "completed":
      return "Completed";
    case "failed":
      return "Failed";
    case "skipped":
      return "Skipped";
  }
}

// ---------------------------------------------------------------------------
// Stage icon
// ---------------------------------------------------------------------------

function StageIcon({ status }: { status: StageStatus }) {
  switch (status) {
    case "completed":
      return <CheckCircle2 className="h-4 w-4 text-green-500 flex-shrink-0" />;
    case "running":
      return <Loader2 className="h-4 w-4 text-blue-500 animate-spin flex-shrink-0" />;
    case "failed":
      return <XCircle className="h-4 w-4 text-red-500 flex-shrink-0" />;
    case "skipped":
      return <SkipForward className="h-4 w-4 text-muted-foreground flex-shrink-0" />;
    default:
      return <Clock className="h-4 w-4 text-muted-foreground/50 flex-shrink-0" />;
  }
}

// ---------------------------------------------------------------------------
// Main Component
// ---------------------------------------------------------------------------

export function ExecutionTracer({ jobId, className }: ExecutionTracerProps) {
  // ---- State ----
  const [stages, setStages] = useState<Map<string, StageState>>(buildInitialStages);
  const [overallProgress, setOverallProgress] = useState(0);
  const [connected, setConnected] = useState(false);
  const [jobStatus, setJobStatus] = useState<"running" | "completed" | "failed" | null>(null);
  const [jobError, setJobError] = useState<string | null>(null);
  const [eventCount, setEventCount] = useState(0);

  // ---- Refs ----
  const eventSourceRef = useRef<EventSource | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectAttemptRef = useRef(0);
  const scrollContainerRef = useRef<HTMLDivElement | null>(null);
  const currentStageRef = useRef<HTMLDivElement | null>(null);
  const mountedRef = useRef(true);

  // ---- Derived ----
  const stagesArray = useMemo(() => {
    return PIPELINE_STAGES.map((name) => stages.get(name)!).filter(Boolean);
  }, [stages]);

  const currentStageIndex = useMemo(() => {
    return stagesArray.findIndex((s) => s.status === "running");
  }, [stagesArray]);

  const completedCount = useMemo(() => {
    return stagesArray.filter((s) => s.status === "completed" || s.status === "skipped").length;
  }, [stagesArray]);

  const failedCount = useMemo(() => {
    return stagesArray.filter((s) => s.status === "failed").length;
  }, [stagesArray]);

  // ---- Auto-scroll to current stage ----
  useEffect(() => {
    if (currentStageRef.current && scrollContainerRef.current) {
      currentStageRef.current.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
      });
    }
  }, [currentStageIndex, eventCount]);

  // ---- SSE connection ----
  const connect = useCallback(() => {
    if (!mountedRef.current) return;
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
    }

    const url = `/api/events?jobId=${encodeURIComponent(jobId)}`;
    const es = new EventSource(url);
    eventSourceRef.current = es;

    es.onopen = () => {
      if (!mountedRef.current) return;
      setConnected(true);
      reconnectAttemptRef.current = 0;
    };

    es.addEventListener("stage_started", (e: MessageEvent) => {
      if (!mountedRef.current) return;
      try {
        const data: SSEEvent = JSON.parse(e.data);
        if (data.stage) {
          setStages((prev) => {
            const next = new Map(prev);
            const existing = next.get(data.stage!);
            if (existing) {
              next.set(data.stage!, {
                ...existing,
                status: "running",
                startedAt: data.timestamp ?? Date.now(),
              });
            }
            return next;
          });
          setEventCount((c) => c + 1);
        }
      } catch {
        // ignore parse errors
      }
    });

    es.addEventListener("stage_completed", (e: MessageEvent) => {
      if (!mountedRef.current) return;
      try {
        const data: SSEEvent = JSON.parse(e.data);
        if (data.stage) {
          setStages((prev) => {
            const next = new Map(prev);
            const existing = next.get(data.stage!);
            if (existing) {
              next.set(data.stage!, {
                ...existing,
                status: "completed",
                completedAt: data.timestamp ?? Date.now(),
                artifacts: data.artifacts ?? existing.artifacts,
              });
            }
            return next;
          });
          setEventCount((c) => c + 1);
        }
      } catch {
        // ignore parse errors
      }
    });

    es.addEventListener("stage_failed", (e: MessageEvent) => {
      if (!mountedRef.current) return;
      try {
        const data: SSEEvent = JSON.parse(e.data);
        if (data.stage) {
          setStages((prev) => {
            const next = new Map(prev);
            const existing = next.get(data.stage!);
            if (existing) {
              next.set(data.stage!, {
                ...existing,
                status: "failed",
                completedAt: data.timestamp ?? Date.now(),
                error: data.error ?? null,
              });
            }
            return next;
          });
          setEventCount((c) => c + 1);
        }
      } catch {
        // ignore parse errors
      }
    });

    es.addEventListener("stage_skipped", (e: MessageEvent) => {
      if (!mountedRef.current) return;
      try {
        const data: SSEEvent = JSON.parse(e.data);
        if (data.stage) {
          setStages((prev) => {
            const next = new Map(prev);
            const existing = next.get(data.stage!);
            if (existing) {
              next.set(data.stage!, {
                ...existing,
                status: "skipped",
              });
            }
            return next;
          });
          setEventCount((c) => c + 1);
        }
      } catch {
        // ignore parse errors
      }
    });

    es.addEventListener("progress", (e: MessageEvent) => {
      if (!mountedRef.current) return;
      try {
        const data: SSEEvent = JSON.parse(e.data);
        if (typeof data.progress === "number") {
          setOverallProgress(Math.min(100, Math.max(0, data.progress)));
        }
      } catch {
        // ignore parse errors
      }
    });

    es.addEventListener("job_complete", () => {
      if (!mountedRef.current) return;
      setJobStatus("completed");
      setOverallProgress(100);
      setConnected(false);
      es.close();
    });

    es.addEventListener("job_failed", (e: MessageEvent) => {
      if (!mountedRef.current) return;
      try {
        const data: SSEEvent = JSON.parse(e.data);
        setJobStatus("failed");
        setJobError(data.error ?? "Job failed with unknown error");
      } catch {
        setJobStatus("failed");
        setJobError("Job failed with unknown error");
      }
      setConnected(false);
      es.close();
    });

    es.addEventListener("job_error", (e: MessageEvent) => {
      if (!mountedRef.current) return;
      try {
        const data: SSEEvent = JSON.parse(e.data);
        setJobError(data.error ?? "An unexpected error occurred");
      } catch {
        setJobError("An unexpected error occurred");
      }
    });

    es.onerror = () => {
      if (!mountedRef.current) return;
      setConnected(false);
      es.close();
      eventSourceRef.current = null;

      // Auto-reconnect with exponential backoff (max 30s)
      if (jobStatus !== "completed" && jobStatus !== "failed") {
        const attempt = reconnectAttemptRef.current;
        const delay = Math.min(1000 * Math.pow(2, attempt), 30000);
        reconnectAttemptRef.current = attempt + 1;

        reconnectTimerRef.current = setTimeout(() => {
          if (mountedRef.current) {
            connect();
          }
        }, delay);
      }
    };
  }, [jobId, jobStatus]);

  // ---- Lifecycle ----
  useEffect(() => {
    mountedRef.current = true;
    connect();

    return () => {
      mountedRef.current = false;
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
        eventSourceRef.current = null;
      }
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    };
  }, [connect]);

  // ---- Derived progress (fallback based on stage count) ----
  const displayProgress = overallProgress > 0
    ? overallProgress
    : stagesArray.length > 0
      ? Math.round((completedCount / stagesArray.length) * 100)
      : 0;

  // ---- Render ----
  return (
    <Card className={cn("flex flex-col", className)}>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <div>
            <CardTitle className="text-lg">Pipeline Execution</CardTitle>
            <CardDescription>
              Real-time stage-by-stage progress for job{" "}
              <code className="text-xs bg-muted px-1 py-0.5 rounded">{jobId}</code>
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
            {jobStatus === "completed" && (
              <Badge variant="default" className="gap-1">
                <CheckCircle2 className="h-3 w-3" />
                Done
              </Badge>
            )}
            {jobStatus === "failed" && (
              <Badge variant="destructive" className="gap-1">
                <XCircle className="h-3 w-3" />
                Failed
              </Badge>
            )}
            {connected ? (
              <Badge variant="secondary" className="gap-1">
                <Wifi className="h-3 w-3" />
                Live
              </Badge>
            ) : jobStatus === null ? (
              <Badge variant="outline" className="gap-1 text-muted-foreground">
                <WifiOff className="h-3 w-3" />
                Disconnected
              </Badge>
            ) : null}
          </div>
        </div>

        {/* Progress bar */}
        <div className="mt-4 space-y-1.5">
          <div className="flex items-center justify-between text-sm">
            <span className="text-muted-foreground">
              {completedCount} / {stagesArray.length} stages
            </span>
            <span className="font-mono tabular-nums text-muted-foreground">
              {displayProgress}%
            </span>
          </div>
          <Progress
            value={displayProgress}
            className={cn(
              "h-2",
              jobStatus === "failed" && "[&>div]:bg-destructive"
            )}
          />
        </div>

        {/* Job-level error */}
        {jobError && (
          <div className="mt-3 flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
            <AlertTriangle className="mt-0.5 h-4 w-4 flex-shrink-0" />
            <span>{jobError}</span>
          </div>
        )}
      </CardHeader>

      <CardContent className="flex-1 min-h-0 p-0">
        <ScrollArea className="h-[520px]" ref={scrollContainerRef}>
          <div className="px-6 pb-6">
            {/* Timeline */}
            <div className="relative">
              {/* Vertical connector line */}
              <div className="absolute left-[19px] top-2 bottom-2 w-px bg-border" aria-hidden="true" />

              <ul className="space-y-0.5">
                {stagesArray.map((stage, index) => {
                  const isRunning = stage.status === "running";
                  const isFailed = stage.status === "failed";
                  const duration =
                    stage.startedAt && stage.completedAt
                      ? stage.completedAt - stage.startedAt
                      : stage.startedAt && isRunning
                        ? Date.now() - stage.startedAt
                        : null;

                  return (
                    <li key={stage.name} className="relative">
                      <div
                        ref={isRunning ? currentStageRef : undefined}
                        className={cn(
                          "flex items-start gap-3 rounded-md px-3 py-2 transition-colors",
                          isRunning && "bg-blue-50 dark:bg-blue-950/30",
                          isFailed && "bg-red-50 dark:bg-red-950/20"
                        )}
                      >
                        {/* Icon on the timeline */}
                        <div className="relative z-10 mt-0.5 flex h-5 w-5 items-center justify-center rounded-full bg-background">
                          <StageIcon status={stage.status} />
                        </div>

                        {/* Content */}
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span
                              className={cn(
                                "text-sm font-medium",
                                isRunning && "text-blue-700 dark:text-blue-300",
                                isFailed && "text-red-700 dark:text-red-300",
                                stage.status === "completed" && "text-foreground",
                                stage.status === "pending" && "text-muted-foreground",
                                stage.status === "skipped" && "text-muted-foreground line-through"
                              )}
                            >
                              {index + 1}. {formatLabel(stage.name)}
                            </span>
                            <Badge
                              variant={stageBadgeVariant(stage.status)}
                              className="text-[10px] px-1.5 py-0"
                            >
                              {stageStatusLabel(stage.status)}
                            </Badge>
                          </div>

                          {/* Duration */}
                          {duration !== null && duration > 0 && (
                            <div className="mt-1 flex items-center gap-1 text-xs text-muted-foreground">
                              <Timer className="h-3 w-3" />
                              <span className="font-mono tabular-nums">
                                {formatDuration(duration)}
                              </span>
                            </div>
                          )}

                          {/* Running elapsed counter */}
                          {isRunning && stage.startedAt && (
                            <RunningDuration startedAt={stage.startedAt} />
                          )}

                          {/* Stage error */}
                          {isFailed && stage.error && (
                            <div className="mt-1.5 rounded border border-red-200 dark:border-red-800 bg-red-100/50 dark:bg-red-900/30 p-2 text-xs text-red-800 dark:text-red-200 font-mono whitespace-pre-wrap break-all">
                              {stage.error}
                            </div>
                          )}

                          {/* Artifacts */}
                          {stage.artifacts.length > 0 && (
                            <div className="mt-1 flex flex-wrap gap-1">
                              {stage.artifacts.map((artifact) => (
                                <Badge
                                  key={artifact}
                                  variant="outline"
                                  className="text-[10px] px-1.5 py-0 font-mono"
                                >
                                  {artifact}
                                </Badge>
                              ))}
                            </div>
                          )}
                        </div>
                      </div>
                    </li>
                  );
                })}
              </ul>
            </div>
          </div>
        </ScrollArea>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Running duration counter (ticks every second for the active stage)
// ---------------------------------------------------------------------------

function RunningDuration({ startedAt }: { startedAt: number }) {
  const [elapsed, setElapsed] = useState(() => Date.now() - startedAt);

  useEffect(() => {
    const timer = setInterval(() => {
      setElapsed(Date.now() - startedAt);
    }, 1000);
    return () => clearInterval(timer);
  }, [startedAt]);

  return (
    <div className="mt-1 flex items-center gap-1 text-xs text-muted-foreground">
      <Timer className="h-3 w-3" />
      <span className="font-mono tabular-nums">{formatDuration(elapsed)}</span>
    </div>
  );
}