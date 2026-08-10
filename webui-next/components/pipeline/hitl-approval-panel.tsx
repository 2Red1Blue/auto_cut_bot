"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  CheckCircle,
  XCircle,
  Clock,
  AlertTriangle,
  ChevronDown,
  ChevronUp,
  Loader2,
} from "lucide-react";
import { wsClient } from "@/lib/ws-client";
import { apiClient } from "@/lib/api-client";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Review stages that require human approval. */
export const HITL_REVIEW_STAGES = ["story_approval", "story_qc_review"] as const;
export type HitlReviewStage = (typeof HITL_REVIEW_STAGES)[number];

/** Stage label mapping for display. */
const STAGE_LABELS: Record<HitlReviewStage, string> = {
  story_approval: "文案审核",
  story_qc_review: "成片质检",
};

/** A pending review notification received from the agent via WebSocket. */
export interface ReviewNotification {
  /** Unique ID for this review (generated client-side). */
  id: string;
  /** The pipeline session ID. */
  sessionId: string;
  /** The review stage name. */
  stage: HitlReviewStage;
  /** Human-readable stage label. */
  stageLabel: string;
  /** Optional data payload from the agent (e.g., story text, video URL). */
  data?: Record<string, unknown>;
  /** Timestamp when the review was received. */
  receivedAt: number;
}

/** A completed review action (approve or reject). */
export interface ReviewHistoryEntry {
  id: string;
  sessionId: string;
  stage: HitlReviewStage;
  stageLabel: string;
  decision: "approved" | "rejected";
  reason?: string;
  decidedAt: number;
}

/** The WebSocket payload shape for a pipeline review request. */
interface PipelineReviewRequestPayload {
  session_id: string;
  stage: string;
  data?: Record<string, unknown>;
  timestamp?: string;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function isHitlReviewStage(stage: string): stage is HitlReviewStage {
  return HITL_REVIEW_STAGES.includes(stage as HitlReviewStage);
}

/** Preset reject reasons with quick-select buttons. */
const REJECT_PRESETS = [
  { label: "证据不足", value: "证据不足：生成内容缺乏足够的支撑依据。" },
  { label: "逻辑断裂", value: "逻辑断裂：叙事流程或推理链路不一致。" },
  { label: "时长超限", value: "时长超限：输出内容超出目标格式的长度限制。" },
  { label: "其他", value: "" },
] as const;

function formatTime(ts: number): string {
  return new Date(ts).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatDate(ts: number): string {
  return new Date(ts).toLocaleDateString("zh-CN", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export interface HitlApprovalPanelProps {
  /** Optional className for the outer container. */
  className?: string;
  /** Maximum number of history entries to display. */
  maxHistoryEntries?: number;
}

export function HitlApprovalPanel({
  className,
  maxHistoryEntries = 20,
}: HitlApprovalPanelProps) {
  // -- State ---------------------------------------------------------------
  const [pendingReviews, setPendingReviews] = useState<ReviewNotification[]>([]);
  const [reviewHistory, setReviewHistory] = useState<ReviewHistoryEntry[]>([]);
  const [historyExpanded, setHistoryExpanded] = useState(false);
  const [acting, setActing] = useState<string | null>(null); // review id currently being acted on
  const [error, setError] = useState<string | null>(null);

  // Reject dialog state
  const [rejectDialogOpen, setRejectDialogOpen] = useState(false);
  const [rejectTarget, setRejectTarget] = useState<ReviewNotification | null>(null);
  const [rejectReason, setRejectReason] = useState("");

  // Refs to avoid stale closures in the WebSocket handler
  const pendingReviewsRef = useRef(pendingReviews);
  pendingReviewsRef.current = pendingReviews;

  // -- WebSocket: listen for review notifications ---------------------------
  useEffect(() => {
    const unsub = wsClient.on("pipeline.review_requested", (raw: unknown) => {
      const payload = raw as PipelineReviewRequestPayload;
      if (!payload?.session_id || !payload?.stage) return;

      if (!isHitlReviewStage(payload.stage)) return;

      const notification: ReviewNotification = {
        id: `${payload.session_id}-${payload.stage}-${Date.now()}`,
        sessionId: payload.session_id,
        stage: payload.stage,
        stageLabel: STAGE_LABELS[payload.stage],
        data: payload.data,
        receivedAt: Date.now(),
      };

      setPendingReviews((prev) => {
        // Deduplicate: if there's already a pending review for the same session+stage
        const exists = prev.some(
          (r) => r.sessionId === notification.sessionId && r.stage === notification.stage
        );
        if (exists) {
          // Replace the existing one with updated data
          return prev.map((r) =>
            r.sessionId === notification.sessionId && r.stage === notification.stage
              ? notification
              : r
          );
        }
        return [...prev, notification];
      });
    });

    // Also listen for review resolution from the server (e.g., another client approved)
    const unsubResolved = wsClient.on("pipeline.review_resolved", (raw: unknown) => {
      const payload = raw as { session_id: string; stage: string };
      if (!payload?.session_id) return;

      setPendingReviews((prev) =>
        prev.filter(
          (r) => !(r.sessionId === payload.session_id && r.stage === payload.stage)
        )
      );
    });

    return () => {
      unsub();
      unsubResolved();
    };
  }, []);

  // -- Actions -------------------------------------------------------------

  const handleApprove = useCallback(async (review: ReviewNotification) => {
    setActing(review.id);
    setError(null);

    try {
      await apiClient.resumePipeline(review.sessionId, "approved");

      // Move to history
      const entry: ReviewHistoryEntry = {
        id: review.id,
        sessionId: review.sessionId,
        stage: review.stage,
        stageLabel: review.stageLabel,
        decision: "approved",
        decidedAt: Date.now(),
      };

      setReviewHistory((prev) => [entry, ...prev].slice(0, maxHistoryEntries));
      setPendingReviews((prev) => prev.filter((r) => r.id !== review.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to approve");
    } finally {
      setActing(null);
    }
  }, [maxHistoryEntries]);

  const openRejectDialog = useCallback((review: ReviewNotification) => {
    setRejectTarget(review);
    setRejectReason("");
    setRejectDialogOpen(true);
  }, []);

  const handleRejectConfirm = useCallback(async () => {
    if (!rejectTarget) return;

    setActing(rejectTarget.id);
    setError(null);

    try {
      await apiClient.resumePipeline(
        rejectTarget.sessionId,
        "rejected",
        rejectReason.trim() || undefined
      );

      const entry: ReviewHistoryEntry = {
        id: rejectTarget.id,
        sessionId: rejectTarget.sessionId,
        stage: rejectTarget.stage,
        stageLabel: rejectTarget.stageLabel,
        decision: "rejected",
        reason: rejectReason.trim() || undefined,
        decidedAt: Date.now(),
      };

      setReviewHistory((prev) => [entry, ...prev].slice(0, maxHistoryEntries));
      setPendingReviews((prev) => prev.filter((r) => r.id !== rejectTarget.id));
      setRejectDialogOpen(false);
      setRejectTarget(null);
      setRejectReason("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to reject");
    } finally {
      setActing(null);
    }
  }, [rejectTarget, rejectReason, maxHistoryEntries]);

  const handleDismissReview = useCallback((reviewId: string) => {
    setPendingReviews((prev) => prev.filter((r) => r.id !== reviewId));
  }, []);

  // -- Render helpers ------------------------------------------------------

  const pendingCount = pendingReviews.length;

  return (
    <div className={cn("space-y-4", className)}>
      {/* Pending Reviews Section */}
      {pendingReviews.length > 0 && (
        <div className="space-y-3">
          {pendingReviews.map((review) => (
            <Card
              key={review.id}
              className="border-amber-200 dark:border-amber-800 bg-amber-50/50 dark:bg-amber-950/20"
            >
              <CardHeader className="pb-3">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <AlertTriangle className="h-5 w-5 text-amber-600 dark:text-amber-400" />
                    <CardTitle className="text-base">待审核 — {review.stageLabel}</CardTitle>
                  </div>
                  <Badge variant="secondary" className="animate-pulse">
                    <Clock className="mr-1 h-3 w-3" />
                    等待中
                  </Badge>
                </div>
                <CardDescription>
                  Session: {review.sessionId.slice(0, 8)}... &middot;{" "}
                  {formatTime(review.receivedAt)}
                </CardDescription>
              </CardHeader>

              {/* Review Data */}
              {review.data && Object.keys(review.data).length > 0 && (
                <CardContent className="pb-3">
                  <div className="rounded-md border bg-background p-3 text-sm">
                    <pre className="whitespace-pre-wrap break-all font-mono text-xs text-muted-foreground">
                      {JSON.stringify(review.data, null, 2)}
                    </pre>
                  </div>
                </CardContent>
              )}

              {/* Action Buttons */}
              <CardContent>
                <div className="flex items-center gap-2">
                  <Button
                    variant="default"
                    size="sm"
                    onClick={() => handleApprove(review)}
                    disabled={acting === review.id}
                    className="flex-1"
                  >
                    {acting === review.id ? (
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    ) : (
                      <CheckCircle className="mr-2 h-4 w-4" />
                    )}
                    通过
                  </Button>

                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => openRejectDialog(review)}
                    disabled={acting === review.id}
                    className="flex-1 text-destructive border-destructive hover:bg-destructive/10"
                  >
                    <XCircle className="mr-2 h-4 w-4" />
                    驳回
                  </Button>

                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => handleDismissReview(review.id)}
                    disabled={acting === review.id}
                    className="text-muted-foreground"
                  >
                    忽略
                  </Button>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {/* Empty State */}
      {pendingReviews.length === 0 && reviewHistory.length === 0 && (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center justify-center py-8 text-center">
            <CheckCircle className="h-8 w-8 text-muted-foreground mb-2" />
            <p className="text-sm text-muted-foreground">暂无待审核项</p>
            <p className="text-xs text-muted-foreground mt-1">
              当 Pipeline 运行到需要人工审核的阶段时，会在此处显示
            </p>
          </CardContent>
        </Card>
      )}

      {/* Error Banner */}
      {error && (
        <div className="rounded-md bg-destructive/10 border border-destructive/30 p-3 text-sm text-destructive">
          {error}
          <Button
            variant="ghost"
            size="sm"
            className="ml-2 h-auto p-0 text-destructive underline"
            onClick={() => setError(null)}
          >
            关闭
          </Button>
        </div>
      )}

      {/* Review History */}
      {reviewHistory.length > 0 && (
        <Card>
          <CardHeader
            className="cursor-pointer select-none pb-3"
            onClick={() => setHistoryExpanded((v) => !v)}
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <CardTitle className="text-sm font-medium">审核历史</CardTitle>
                <Badge variant="secondary" className="text-xs">
                  {reviewHistory.length}
                </Badge>
              </div>
              {historyExpanded ? (
                <ChevronUp className="h-4 w-4 text-muted-foreground" />
              ) : (
                <ChevronDown className="h-4 w-4 text-muted-foreground" />
              )}
            </div>
          </CardHeader>

          {historyExpanded && (
            <CardContent className="pt-0">
              <ScrollArea className="h-[240px]">
                <div className="space-y-2">
                  {reviewHistory.map((entry) => (
                    <div
                      key={entry.id}
                      className="flex items-center justify-between rounded-md border p-3 text-sm"
                    >
                      <div className="flex items-center gap-2 min-w-0">
                        {entry.decision === "approved" ? (
                          <CheckCircle className="h-4 w-4 shrink-0 text-green-600" />
                        ) : (
                          <XCircle className="h-4 w-4 shrink-0 text-red-600" />
                        )}
                        <div className="min-w-0">
                          <div className="font-medium truncate">{entry.stageLabel}</div>
                          <div className="text-xs text-muted-foreground">
                            {entry.sessionId.slice(0, 8)}... &middot; {formatDate(entry.decidedAt)}
                          </div>
                          {entry.reason && (
                            <div className="text-xs text-muted-foreground mt-1 truncate">
                              原因: {entry.reason}
                            </div>
                          )}
                        </div>
                      </div>
                      <Badge
                        variant={entry.decision === "approved" ? "default" : "destructive"}
                        className="shrink-0 ml-2 text-xs"
                      >
                        {entry.decision === "approved" ? "已通过" : "已驳回"}
                      </Badge>
                    </div>
                  ))}
                </div>
              </ScrollArea>
            </CardContent>
          )}
        </Card>
      )}

      {/* Reject Reason Dialog */}
      <Dialog open={rejectDialogOpen} onOpenChange={setRejectDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>驳回审核</DialogTitle>
            <DialogDescription>
              {rejectTarget && (
                <>
                  驳回 <span className="font-medium">{rejectTarget.stageLabel}</span>{" "}
                  阶段的审核结果。请填写驳回原因以便 Agent 了解需要如何修改。
                </>
              )}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-3">
            {/* Preset reason buttons */}
            <div className="space-y-1.5">
              <label className="text-sm font-medium">快捷原因</label>
              <div className="flex flex-wrap gap-1.5">
                {REJECT_PRESETS.map((preset) => (
                  <Button
                    key={preset.label}
                    variant="outline"
                    size="xs"
                    className={cn(
                      "text-xs",
                      rejectReason === preset.value &&
                        "border-ring bg-accent text-accent-foreground"
                    )}
                    onClick={() => setRejectReason(preset.value)}
                  >
                    {preset.label}
                  </Button>
                ))}
              </div>
            </div>

            {/* Custom reason textarea */}
            <div className="space-y-1.5">
              <label className="text-sm font-medium">驳回原因</label>
              <Textarea
                placeholder="例如: 文案中存在事实错误，请重新生成..."
                value={rejectReason}
                onChange={(e) => setRejectReason(e.target.value)}
                rows={4}
                className="resize-none"
              />
            </div>
          </div>

          <DialogFooter showCloseButton>
            <Button
              variant="destructive"
              onClick={handleRejectConfirm}
              disabled={acting === rejectTarget?.id}
            >
              {acting === rejectTarget?.id ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              ) : null}
              确认驳回
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Badge sub-component for the pending count
// ---------------------------------------------------------------------------

export interface HitlApprovalBadgeProps {
  /** Optional className. */
  className?: string;
}

export function HitlApprovalBadge({ className }: HitlApprovalBadgeProps) {
  const [count, setCount] = useState(0);

  useEffect(() => {
    const unsub = wsClient.on("pipeline.review_requested", (raw: unknown) => {
      const payload = raw as PipelineReviewRequestPayload;
      if (!payload?.stage || !isHitlReviewStage(payload.stage)) return;
      setCount((prev) => prev + 1);
    });

    const unsubResolved = wsClient.on("pipeline.review_resolved", () => {
      setCount((prev) => Math.max(0, prev - 1));
    });

    return () => {
      unsub();
      unsubResolved();
    };
  }, []);

  if (count === 0) return null;

  return (
    <Badge
      variant="destructive"
      className={cn("animate-pulse", className)}
    >
      <AlertTriangle className="mr-1 h-3 w-3" />
      {count} 待审核
    </Badge>
  );
}
