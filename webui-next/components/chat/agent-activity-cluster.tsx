"use client";

import {
  useState,
  useCallback,
  useMemo,
  useEffect,
  useRef,
  useLayoutEffect,
  type ReactNode,
} from "react";
import {
  ChevronDown,
  ChevronRight,
  Wrench,
  CheckCircle2,
  XCircle,
  Loader2,
  Brain,
  FileText,
  Search,
  Terminal,
  Server,
  Clock,
  Plus,
  Minus,
  Layers,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useTranslation } from "react-i18next";
import { isReasoningOnlyAssistant } from "@/lib/activity-timeline";
import type { UIMessage, ToolProgressEvent, UIFileEdit, UIFileDiff } from "@/lib/types";

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

type ActivityStatus = "running" | "done" | "error" | "idle";

interface ActivityGroup {
  /** Unique key for the group (turnId or activitySegmentId). */
  key: string;
  /** Messages belonging to this group. */
  messages: UIMessage[];
  /** Earliest message timestamp. */
  startedAt: number;
  /** Latest message timestamp. */
  endedAt: number;
}

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

function toolEventName(event: ToolProgressEvent): string {
  return (
    (typeof (event as { function?: { name?: unknown } }).function?.name === "string"
      ? String((event as { function?: { name?: unknown } }).function?.name)
      : "") ||
    (typeof event.name === "string" ? event.name : "")
  );
}

function dedupeToolEvents(events: ToolProgressEvent[]): ToolProgressEvent[] {
  const seen = new Set<string>();
  const out: ToolProgressEvent[] = [];
  for (const e of events) {
    const key = e.call_id ?? `${e.name}:${JSON.stringify(e.arguments)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(e);
  }
  return out;
}

function getToolStatus(events: ToolProgressEvent[]): ActivityStatus {
  if (events.length === 0) return "idle";
  const phases = new Set(events.map((e) => e.phase));
  if (phases.has("error")) return "error";
  if (phases.has("end")) return "done";
  if (phases.has("start")) return "running";
  return "idle";
}

function toolDisplayName(name: string): string {
  if (name.startsWith("mcp_")) {
    const rest = name.slice(4);
    const idx = rest.indexOf("_");
    if (idx > 0) return `${rest.slice(0, idx)} / ${rest.slice(idx + 1)}`;
  }
  return name;
}

function formatDuration(ms: number): string {
  if (ms <= 0) return "";
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
}

function formatArgs(args: unknown): string {
  if (args === undefined || args === null) return "";
  if (typeof args === "string") {
    try {
      const parsed = JSON.parse(args);
      return formatArgs(parsed);
    } catch {
      return args.length > 200 ? args.slice(0, 200) + "..." : args;
    }
  }
  if (typeof args === "object") {
    const str = JSON.stringify(args, null, 2);
    return str.length > 500 ? str.slice(0, 500) + "..." : str;
  }
  return String(args);
}

function formatResult(result: unknown): string {
  if (result === undefined || result === null) return "";
  if (typeof result === "string") {
    try {
      const parsed = JSON.parse(result);
      return formatResult(parsed);
    } catch {
      return result.length > 300 ? result.slice(0, 300) + "..." : result;
    }
  }
  if (typeof result === "object") {
    const str = JSON.stringify(result);
    return str.length > 500 ? str.slice(0, 500) + "..." : str;
  }
  return String(result);
}

function isWebSearchTool(name: string): boolean {
  return /^(web_search|search_web|tavily_search|brave_search|ddg_search|google_search|exa_search|web_fetch)\b/.test(name);
}

function isFileEditTool(name: string): boolean {
  return /^(write_file|edit_file|apply_patch|write_to_file|create_file|replace_in_file)\b/.test(name);
}

function extractReasoningText(messages: UIMessage[]): string | null {
  for (const m of messages) {
    if (isReasoningOnlyAssistant(m) && m.reasoning?.trim()) {
      return m.reasoning;
    }
  }
  return null;
}

/* ------------------------------------------------------------------ */
/*  Status Indicator                                                   */
/* ------------------------------------------------------------------ */

function StatusIcon({ status }: { status: ActivityStatus }) {
  if (status === "running")
    return <Loader2 className="h-4 w-4 shrink-0 animate-spin text-blue-500" />;
  if (status === "done")
    return <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-500" />;
  if (status === "error")
    return <XCircle className="h-4 w-4 shrink-0 text-red-500" />;
  return null;
}

/* ------------------------------------------------------------------ */
/*  Reasoning Row                                                      */
/* ------------------------------------------------------------------ */

function ReasoningRow({
  text,
  isStreaming,
  autoExpand,
}: {
  text: string;
  isStreaming: boolean;
  autoExpand: boolean;
}) {
  const [expanded, setExpanded] = useState(autoExpand);
  const { t } = useTranslation();

  useEffect(() => {
    if (autoExpand && isStreaming) setExpanded(true);
  }, [autoExpand, isStreaming]);

  const displayText = text.length > 300 && !expanded ? text.slice(0, 300) + "..." : text;

  return (
    <div className="rounded-md border border-border bg-muted/30">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-xs text-muted-foreground hover:bg-muted/50 transition-colors"
      >
        <Brain className="h-3.5 w-3.5 shrink-0 text-purple-400" />
        <span className="flex-1 truncate text-left">
          {isStreaming
            ? t("activity.thinking", "Thinking...")
            : t("activity.thought", "Thought")}
        </span>
        {isStreaming && (
          <Loader2 className="h-3 w-3 shrink-0 animate-spin text-purple-400" />
        )}
        {text.length > 300 && (
          expanded ? (
            <ChevronDown className="h-3.5 w-3.5 shrink-0" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5 shrink-0" />
          )
        )}
      </button>
      {expanded && (
        <div className="border-t border-border px-2.5 py-2 text-xs text-muted-foreground leading-relaxed whitespace-pre-wrap">
          {displayText}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Tool Call Row                                                      */
/* ------------------------------------------------------------------ */

function ToolCallRow({
  event,
  isStreaming,
}: {
  event: ToolProgressEvent;
  isStreaming: boolean;
}) {
  const [argsOpen, setArgsOpen] = useState(false);
  const [resultOpen, setResultOpen] = useState(false);
  const name = toolEventName(event);
  const status: ActivityStatus =
    event.phase === "error" ? "error" : event.phase === "end" ? "done" : "running";
  const rowActive = status === "running" && isStreaming;
  const args = event.arguments;
  const result = event.result;
  const error = event.error;
  const displayName = toolDisplayName(name);

  const isWebSearch = isWebSearchTool(name);
  const isFileEdit = isFileEditTool(name);

  let icon: ReactNode = <Wrench className="h-3.5 w-3.5 shrink-0" />;
  if (isWebSearch) icon = <Search className="h-3.5 w-3.5 shrink-0 text-blue-400" />;
  else if (isFileEdit) icon = <FileText className="h-3.5 w-3.5 shrink-0 text-amber-400" />;
  else if (name.startsWith("run_cli_app") || name.startsWith("cli_"))
    icon = <Terminal className="h-3.5 w-3.5 shrink-0 text-cyan-400" />;
  else if (name.startsWith("mcp_"))
    icon = <Server className="h-3.5 w-3.5 shrink-0 text-indigo-400" />;

  return (
    <div
      className={cn(
        "rounded-md border border-border bg-card",
        rowActive && "border-blue-500/30 bg-blue-500/5",
        status === "error" && "border-red-500/30 bg-red-500/5",
      )}
    >
      {/* Header */}
      <button
        type="button"
        onClick={() => setArgsOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-xs hover:bg-muted/30 transition-colors"
      >
        {icon}
        <span className="flex-1 truncate text-left font-mono text-[11px]">
          {displayName}
        </span>
        <StatusIcon status={status} />
        {args !== undefined && args !== null && (
          argsOpen ? (
            <ChevronDown className="h-3 w-3 shrink-0 text-muted-foreground/50" />
          ) : (
            <ChevronRight className="h-3 w-3 shrink-0 text-muted-foreground/50" />
          )
        )}
      </button>

      {/* Args (collapsed) */}
      {argsOpen && args !== undefined && args !== null && (
        <div className="border-t border-border px-2.5 py-1.5">
          <div className="text-[10px] font-semibold text-muted-foreground/60 uppercase tracking-wider mb-1">
            Input
          </div>
          <pre className="text-[11px] text-muted-foreground font-mono whitespace-pre-wrap break-all max-h-32 overflow-y-auto">
            {formatArgs(args)}
          </pre>
        </div>
      )}

      {/* Error */}
      {status === "error" && error !== undefined && (
        <div className="border-t border-red-500/20 px-2.5 py-1.5">
          <div className="text-[10px] font-semibold text-red-400 uppercase tracking-wider mb-1">
            Error
          </div>
          <pre className="text-[11px] text-red-400 font-mono whitespace-pre-wrap break-all max-h-24 overflow-y-auto">
            {typeof error === "string" ? error : JSON.stringify(error)}
          </pre>
        </div>
      )}

      {/* Result (expandable) */}
      {status === "done" && result !== undefined && result !== null && (
        <div className="border-t border-border">
          <button
            type="button"
            onClick={() => setResultOpen((v) => !v)}
            className="flex w-full items-center gap-1.5 px-2.5 py-1 text-[10px] text-muted-foreground/60 hover:text-muted-foreground transition-colors"
          >
            {resultOpen ? (
              <ChevronDown className="h-3 w-3 shrink-0" />
            ) : (
              <ChevronRight className="h-3 w-3 shrink-0" />
            )}
            <span className="uppercase tracking-wider font-semibold">
              Output
            </span>
          </button>
          {resultOpen && (
            <div className="px-2.5 pb-1.5">
              <pre className="text-[11px] text-muted-foreground font-mono whitespace-pre-wrap break-all max-h-32 overflow-y-auto">
                {formatResult(result)}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  File Edit Row                                                      */
/* ------------------------------------------------------------------ */

function FileEditRow({
  edit,
  isStreaming,
}: {
  edit: UIFileEdit;
  isStreaming: boolean;
}) {
  const [diffOpen, setDiffOpen] = useState(false);
  const status: ActivityStatus =
    edit.status === "error" ? "error" : edit.status === "done" ? "done" : "running";
  const rowActive = status === "running" && isStreaming;

  const added = edit.added ?? 0;
  const deleted = edit.deleted ?? 0;
  const hasDiff = !!edit.diff?.text;

  return (
    <div
      className={cn(
        "rounded-md border border-border bg-card",
        rowActive && "border-amber-500/30 bg-amber-500/5",
        status === "error" && "border-red-500/30 bg-red-500/5",
      )}
    >
      {/* Header */}
      <button
        type="button"
        onClick={() => hasDiff && setDiffOpen((v) => !v)}
        className={cn(
          "flex w-full items-center gap-2 px-2.5 py-1.5 text-xs",
          hasDiff && "hover:bg-muted/30 transition-colors cursor-pointer",
        )}
      >
        <FileText className="h-3.5 w-3.5 shrink-0 text-amber-400" />
        <span className="flex-1 truncate text-left font-mono text-[11px]">
          {edit.path || "(unknown path)"}
        </span>
        {added > 0 && (
          <span className="inline-flex items-center gap-0.5 text-[10px] font-mono text-emerald-500">
            <Plus className="h-3 w-3" />
            {added}
          </span>
        )}
        {deleted > 0 && (
          <span className="inline-flex items-center gap-0.5 text-[10px] font-mono text-red-500">
            <Minus className="h-3 w-3" />
            {deleted}
          </span>
        )}
        {edit.binary && (
          <span className="text-[10px] text-muted-foreground/50">binary</span>
        )}
        <StatusIcon status={status} />
        {hasDiff && (
          diffOpen ? (
            <ChevronDown className="h-3 w-3 shrink-0 text-muted-foreground/50" />
          ) : (
            <ChevronRight className="h-3 w-3 shrink-0 text-muted-foreground/50" />
          )
        )}
      </button>

      {/* Error */}
      {status === "error" && edit.error && (
        <div className="border-t border-red-500/20 px-2.5 py-1.5">
          <pre className="text-[11px] text-red-400 font-mono whitespace-pre-wrap break-all">
            {edit.error}
          </pre>
        </div>
      )}

      {/* Diff */}
      {diffOpen && hasDiff && (
        <div className="border-t border-border px-2.5 py-1.5">
          <FileDiffView diff={edit.diff!} />
        </div>
      )}
    </div>
  );
}

function FileDiffView({ diff }: { diff: UIFileDiff }) {
  if (!diff.text) return null;
  const lines = diff.text.split("\n");

  return (
    <div className="text-[11px] font-mono leading-relaxed max-h-64 overflow-y-auto rounded bg-muted/30">
      {lines.map((line, i) => {
        let lineClass = "text-muted-foreground/70";
        if (line.startsWith("+") && !line.startsWith("+++"))
          lineClass = "text-emerald-500 bg-emerald-500/10";
        else if (line.startsWith("-") && !line.startsWith("---"))
          lineClass = "text-red-500 bg-red-500/10";
        else if (line.startsWith("@@"))
          lineClass = "text-blue-400 bg-blue-400/5";

        return (
          <div key={i} className={cn("px-2 py-px", lineClass)}>
            {line || " "}
          </div>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Web Search Row                                                     */
/* ------------------------------------------------------------------ */

function WebSearchRow({
  event,
  isStreaming,
}: {
  event: ToolProgressEvent;
  isStreaming: boolean;
}) {
  const [resultsOpen, setResultsOpen] = useState(false);
  const name = toolEventName(event);
  const status: ActivityStatus =
    event.phase === "error" ? "error" : event.phase === "end" ? "done" : "running";
  const rowActive = status === "running" && isStreaming;
  const args = event.arguments;
  const result = event.result;

  // Try to extract query from args
  let query = "";
  if (args && typeof args === "object") {
    const record = args as Record<string, unknown>;
    query = String(record.query ?? record.q ?? record.search ?? "");
  }

  return (
    <div
      className={cn(
        "rounded-md border border-border bg-card",
        rowActive && "border-blue-500/30 bg-blue-500/5",
        status === "error" && "border-red-500/30 bg-red-500/5",
      )}
    >
      <button
        type="button"
        onClick={() => status === "done" && setResultsOpen((v) => !v)}
        className={cn(
          "flex w-full items-center gap-2 px-2.5 py-1.5 text-xs",
          status === "done" && "hover:bg-muted/30 transition-colors cursor-pointer",
        )}
      >
        <Search className="h-3.5 w-3.5 shrink-0 text-blue-400" />
        <span className="flex-1 truncate text-left">
          {query
            ? `Web search: "${query}"`
            : toolDisplayName(name)}
        </span>
        <StatusIcon status={status} />
        {status === "done" && result !== undefined && (
          resultsOpen ? (
            <ChevronDown className="h-3 w-3 shrink-0 text-muted-foreground/50" />
          ) : (
            <ChevronRight className="h-3 w-3 shrink-0 text-muted-foreground/50" />
          )
        )}
      </button>

      {resultsOpen && result !== undefined && (
        <div className="border-t border-border px-2.5 py-1.5">
          <pre className="text-[11px] text-muted-foreground font-mono whitespace-pre-wrap break-all max-h-48 overflow-y-auto">
            {formatResult(result)}
          </pre>
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Activity Group (collapsible)                                       */
/* ------------------------------------------------------------------ */

function ActivityGroupCard({
  group,
  isStreaming,
  now,
}: {
  group: ActivityGroup;
  isStreaming: boolean;
  now: number;
}) {
  const [expanded, setExpanded] = useState(true);
  const { t } = useTranslation();
  const bodyRef = useRef<HTMLDivElement>(null);
  const autoFollowRef = useRef(true);

  // Auto-expand during streaming; collapse after completion
  const [completionHoldOpen, setCompletionHoldOpen] = useState(false);
  const wasStreamingRef = useRef(isStreaming);

  useEffect(() => {
    const wasStreaming = wasStreamingRef.current;
    wasStreamingRef.current = isStreaming;
    if (isStreaming) {
      setCompletionHoldOpen(false);
      setExpanded(true);
      return;
    }
    if (!wasStreaming) return;
    // Streaming just completed — hold open briefly then collapse
    setCompletionHoldOpen(true);
    const timeout = window.setTimeout(() => setCompletionHoldOpen(false), 2500);
    return () => window.clearTimeout(timeout);
  }, [isStreaming]);

  const actuallyExpanded = expanded || completionHoldOpen;

  // Collect all activity items
  const allEvents = useMemo(() => {
    const events: ToolProgressEvent[] = [];
    for (const m of group.messages) {
      if (m.toolEvents) events.push(...m.toolEvents);
    }
    return dedupeToolEvents(events);
  }, [group.messages]);

  const fileEdits = useMemo(() => {
    const edits: UIFileEdit[] = [];
    for (const m of group.messages) {
      if (m.kind === "trace" && m.fileEdits) edits.push(...m.fileEdits);
    }
    return edits;
  }, [group.messages]);

  const reasoningText = useMemo(() => extractReasoningText(group.messages), [group.messages]);

  // Determine overall status
  const groupStatus: ActivityStatus = useMemo(() => {
    if (allEvents.length === 0 && fileEdits.length === 0) {
      return isStreaming ? "running" : "idle";
    }
    const toolStatus = getToolStatus(allEvents);
    const editStatuses = fileEdits.map((e) =>
      e.status === "error" ? "error" : e.status === "done" ? "done" : "running"
    ) as ActivityStatus[];
    const allStatuses = [toolStatus, ...editStatuses];
    if (allStatuses.includes("error")) return "error";
    if (allStatuses.includes("running")) return "running";
    if (allStatuses.every((s) => s === "done" || s === "idle")) return "done";
    return "idle";
  }, [allEvents, fileEdits, isStreaming]);

  // Duration
  const durationMs = useMemo(() => {
    if (isStreaming && groupStatus === "running") {
      return Math.max(0, now - group.startedAt);
    }
    return Math.max(0, group.endedAt - group.startedAt);
  }, [group, now, isStreaming, groupStatus]);
  const duration = formatDuration(durationMs);

  // Build label
  const label = useMemo(() => {
    const toolNames = allEvents
      .map((e) => toolEventName(e))
      .filter(Boolean)
      .map(toolDisplayName);
    const uniqueNames = [...new Set(toolNames)];

    if (uniqueNames.length > 0) {
      return uniqueNames.length > 2
        ? `${uniqueNames.slice(0, 2).join(", ")} +${uniqueNames.length - 2}`
        : uniqueNames.join(", ");
    }
    if (fileEdits.length > 0) {
      return fileEdits.length === 1
        ? `Edit ${fileEdits[0].path}`
        : `${fileEdits.length} file edits`;
    }
    if (reasoningText) {
      return isStreaming
        ? t("activity.thinking", "Thinking...")
        : t("activity.thought", "Thought");
    }
    return t("activity.agent_label", "Activity");
  }, [allEvents, fileEdits, reasoningText, isStreaming, t]);

  // Auto-scroll to bottom when streaming
  useLayoutEffect(() => {
    if (!isStreaming || !autoFollowRef.current || !bodyRef.current) return;
    bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [isStreaming, allEvents, fileEdits]);

  const handleScroll = useCallback(() => {
    const el = bodyRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    autoFollowRef.current = distance < 48;
  }, []);

  const hasContent = allEvents.length > 0 || fileEdits.length > 0 || !!reasoningText;

  if (!hasContent) return null;

  return (
    <div
      className={cn(
        "rounded-lg border border-border bg-card overflow-hidden",
        groupStatus === "running" && "border-blue-500/30",
        groupStatus === "error" && "border-red-500/30",
      )}
    >
      {/* Header */}
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className={cn(
          "flex w-full items-center gap-2 px-3 py-2 text-sm",
          "hover:bg-muted/30 transition-colors",
          groupStatus === "running" && "bg-blue-500/5",
        )}
      >
        {actuallyExpanded ? (
          <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground/60" />
        ) : (
          <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground/60" />
        )}
        <Layers className="h-4 w-4 shrink-0 text-muted-foreground/60" />
        <span className="flex-1 truncate text-left text-muted-foreground">
          {label}
        </span>
        {duration && (
          <span className="inline-flex items-center gap-1 text-xs text-muted-foreground/50">
            <Clock className="h-3 w-3" />
            {duration}
          </span>
        )}
        <StatusIcon status={groupStatus} />
      </button>

      {/* Body */}
      {actuallyExpanded && (
        <div
          ref={bodyRef}
          onScroll={handleScroll}
          className="border-t border-border px-2 py-2 space-y-1.5 max-h-80 overflow-y-auto"
        >
          {/* Reasoning */}
          {reasoningText && (
            <ReasoningRow
              text={reasoningText}
              isStreaming={isStreaming}
              autoExpand={isStreaming}
            />
          )}

          {/* Tool events */}
          {allEvents.map((event, i) => {
            const name = toolEventName(event);
            if (isWebSearchTool(name)) {
              return (
                <WebSearchRow
                  key={event.call_id ?? `event-${i}`}
                  event={event}
                  isStreaming={isStreaming}
                />
              );
            }
            return (
              <ToolCallRow
                key={event.call_id ?? `event-${i}`}
                event={event}
                isStreaming={isStreaming}
              />
            );
          })}

          {/* File edits */}
          {fileEdits.map((edit, i) => (
            <FileEditRow
              key={edit.call_id ?? `file-${i}`}
              edit={edit}
              isStreaming={isStreaming}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Main Component                                                     */
/* ------------------------------------------------------------------ */

export interface AgentActivityClusterProps {
  messages: UIMessage[];
  /** The turn ID to scope activity to (optional; if omitted, uses all trace messages). */
  turnId?: string;
  /** True while the session turn is still executing. */
  isStreaming?: boolean;
}

export function AgentActivityCluster({
  messages,
  turnId,
  isStreaming,
}: AgentActivityClusterProps) {
  const [now, setNow] = useState(() => Date.now());

  // Tick the clock during streaming
  useEffect(() => {
    if (!isStreaming) return;
    setNow(Date.now());
    const interval = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(interval);
  }, [isStreaming]);

  // Build groups from messages
  const groups = useMemo(() => {
    // Filter to trace messages
    let traceMessages = messages.filter((m) => m.kind === "trace");
    if (turnId) {
      traceMessages = traceMessages.filter((m) => m.turnId === turnId);
    }

    // Also include reasoning-only assistant messages
    const reasoningMessages = messages.filter(
      (m) => isReasoningOnlyAssistant(m) && (!turnId || m.turnId === turnId),
    );

    const allActivityMessages = [...traceMessages, ...reasoningMessages].sort(
      (a, b) => a.createdAt - b.createdAt,
    );

    if (allActivityMessages.length === 0) return [];

    // Group by activitySegmentId when available, otherwise by turnId, fallback to single group
    const groupMap = new Map<string, UIMessage[]>();
    const order: string[] = [];

    for (const msg of allActivityMessages) {
      const key = msg.activitySegmentId ?? msg.turnId ?? "activity";
      if (!groupMap.has(key)) {
        groupMap.set(key, []);
        order.push(key);
      }
      groupMap.get(key)!.push(msg);
    }

    return order.map((key) => {
      const msgs = groupMap.get(key)!;
      const timestamps = msgs.map((m) => m.createdAt).filter((t) => Number.isFinite(t));
      return {
        key,
        messages: msgs,
        startedAt: timestamps.length > 0 ? Math.min(...timestamps) : 0,
        endedAt: timestamps.length > 0 ? Math.max(...timestamps) : 0,
      } satisfies ActivityGroup;
    });
  }, [messages, turnId]);

  if (groups.length === 0) return null;

  return (
    <div className="space-y-2 w-full">
      {groups.map((group) => (
        <ActivityGroupCard
          key={group.key}
          group={group}
          isStreaming={!!isStreaming}
          now={now}
        />
      ))}
    </div>
  );
}

export { isReasoningOnlyAssistant };
export default AgentActivityCluster;