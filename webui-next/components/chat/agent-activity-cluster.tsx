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
import dynamic from "next/dynamic";
import {
  ChevronDown,
  ChevronRight,
  ChevronUp,
  CheckCircle2,
  XCircle,
  Loader2,
  FileText,
  Search,
  Clock,
  Layers,
  Globe2,
  ExternalLink,
  FileSearch,
  FolderOpen,
  ListTree,
  MemoryStick,
  Play,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useTranslation } from "react-i18next";
import { parsePatch } from "diff";
import { isReasoningOnlyAssistant } from "@/lib/activity-timeline";
import { useThemeValue } from "@/hooks/use-theme";
import { codeLanguageFromPath } from "@/lib/code-language";
import type { UIMessage, ToolProgressEvent, UIFileEdit, UIFileDiff } from "@/lib/types";

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

type ActivityStatus = "running" | "done" | "error" | "idle";

interface ActivityGroup {
  key: string;
  messages: UIMessage[];
  startedAt: number;
  endedAt: number;
}

/* ------------------------------------------------------------------ */
/*  Diff Types (inlined from file-diff)                                */
/* ------------------------------------------------------------------ */

interface RenderableFileDiffLine {
  kind: "context" | "add" | "delete";
  old_lineno?: number | null;
  new_lineno?: number | null;
  content: string;
}

interface RenderableFileDiffHunk {
  old_start: number;
  old_lines: number;
  new_start: number;
  new_lines: number;
  lines: RenderableFileDiffLine[];
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
  return /^(web_search|search_web|tavily_search|brave_search|ddg_search|google_search|exa_search|web_fetch|x_search)\b/.test(name);
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

function compactReasoningPreview(value: string): string {
  return value
    .replace(/\[([^\]]+)]\([^)]+\)/g, "$1")
    .replace(/[*_#`~]+/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function hasRenderableDiff(diff?: UIFileDiff): boolean {
  if (!diff) return false;
  return typeof diff.text === "string" && diff.text.trim().length > 0;
}

function parseDiffText(text: string): RenderableFileDiffHunk[] {
  try {
    const files = parsePatch(text);
    return files.flatMap((file) =>
      file.hunks.map((hunk) => {
        let oldLineno = hunk.oldStart;
        let newLineno = hunk.newStart;
        const lines: RenderableFileDiffLine[] = [];
        for (const rawLine of hunk.lines) {
          if (rawLine.startsWith("\\")) continue;
          const marker = rawLine[0];
          const content = rawLine.slice(1);
          if (marker === "+") {
            lines.push({ kind: "add", old_lineno: null, new_lineno: newLineno, content });
            newLineno++;
          } else if (marker === "-") {
            lines.push({ kind: "delete", old_lineno: oldLineno, new_lineno: null, content });
            oldLineno++;
          } else {
            lines.push({ kind: "context", old_lineno: oldLineno, new_lineno: newLineno, content });
            oldLineno++;
            newLineno++;
          }
        }
        return {
          old_start: hunk.oldStart,
          old_lines: hunk.oldLines,
          new_start: hunk.newStart,
          new_lines: hunk.newLines,
          lines,
        };
      }),
    );
  } catch {
    // Fallback: split by lines and color by prefix
    const lines = text.split("\n");
    let oldLineno = 1;
    let newLineno = 1;
    const result: RenderableFileDiffLine[] = [];
    for (const line of lines) {
      if (line.startsWith("+") && !line.startsWith("+++")) {
        result.push({ kind: "add", old_lineno: null, new_lineno: newLineno, content: line.slice(1) });
        newLineno++;
      } else if (line.startsWith("-") && !line.startsWith("---")) {
        result.push({ kind: "delete", old_lineno: oldLineno, new_lineno: null, content: line.slice(1) });
        oldLineno++;
      } else if (line.startsWith("@@")) {
        result.push({ kind: "context", old_lineno: null, new_lineno: null, content: line });
      } else {
        result.push({ kind: "context", old_lineno: oldLineno, new_lineno: newLineno, content: line.startsWith(" ") ? line.slice(1) : line });
        oldLineno++;
        newLineno++;
      }
    }
    return [{ old_start: 1, old_lines: oldLineno - 1, new_start: 1, new_lines: newLineno - 1, lines: result }];
  }
}

function countDiffLines(hunks: RenderableFileDiffHunk[]): number {
  return hunks.reduce((total, hunk) => total + hunk.lines.length, 0);
}

/* ------------------------------------------------------------------ */
/*  Generic Tool Family Classification                                 */
/* ------------------------------------------------------------------ */

type ToolFamily = "content-search" | "file-search" | "list" | "read" | "memory" | "generic";

const CONTENT_SEARCH_TOOLS = new Set([
  "grep", "rg", "ripgrep", "search_code", "search_content", "search_files_content", "find_text",
]);
const FILE_SEARCH_TOOLS = new Set([
  "find", "find_file", "find_files", "glob", "search_files",
]);
const LIST_TOOLS = new Set(["list_dir", "list_directory", "list_files", "ls"]);
const READ_TOOLS = new Set(["read", "read_file", "read_text_file"]);
const MEMORY_TOOLS = new Set(["memory_search", "search_memory", "recall_memory"]);

function classifyToolFamily(name: string): ToolFamily {
  if (CONTENT_SEARCH_TOOLS.has(name)) return "content-search";
  if (FILE_SEARCH_TOOLS.has(name)) return "file-search";
  if (LIST_TOOLS.has(name)) return "list";
  if (READ_TOOLS.has(name)) return "read";
  if (MEMORY_TOOLS.has(name)) return "memory";
  return "generic";
}

function genericToolIcon(family: ToolFamily): LucideIcon {
  switch (family) {
    case "content-search":
    case "file-search":
      return FileSearch;
    case "list":
      return ListTree;
    case "read":
      return FolderOpen;
    case "memory":
      return MemoryStick;
    default:
      return Play;
  }
}

function genericToolLabel(family: ToolFamily, status: ActivityStatus, name: string): string {
  const verb = status === "running" ? "Running" : status === "error" ? "Could not run" : "Completed";
  if (family === "content-search") return `${verb} content search`;
  if (family === "file-search") return `${verb} file search`;
  if (family === "list") return `${verb} directory listing`;
  if (family === "read") return `${verb} file read`;
  if (family === "memory") return `${verb} memory search`;
  return `${verb} ${toolDisplayName(name)}`;
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
/*  DiffPair                                                            */
/* ------------------------------------------------------------------ */

function DiffPair({ added, deleted }: { added: number; deleted: number }) {
  return (
    <span className="inline-flex shrink-0 items-baseline gap-1.5 leading-[inherit] tabular-nums">
      <DiffValue sign="+" value={added} className="text-emerald-500" />
      <DiffValue sign="-" value={deleted} className="text-red-500" />
    </span>
  );
}

function DiffValue({ sign, value, className }: { sign: string; value: number; className: string }) {
  const safeValue = Number.isFinite(value) ? Math.max(0, Math.round(value)) : 0;
  return (
    <span className={cn("inline-flex items-baseline leading-[inherit]", className)} aria-label={`${sign}${safeValue}`}>
      <span className="inline-flex items-baseline leading-none" aria-hidden>{sign}{safeValue}</span>
    </span>
  );
}

/* ------------------------------------------------------------------ */
/*  DiffSyntaxHighlight — Syntax-highlighted diff with green/red lines */
/* ------------------------------------------------------------------ */

const CODE_FONT_STACK = [
  '"JetBrains Mono"',
  '"SFMono-Regular"',
  '"SF Mono"',
  '"Fira Code"',
  '"Cascadia Code"',
  '"Source Code Pro"',
  "Menlo",
  "Consolas",
  "monospace",
].join(", ");

const INITIAL_VISIBLE_DIFF_LINES = 160;
const AUTO_COLLAPSE_DIFF_LINES = 160;

const SyntaxHighlighter = dynamic(
  () =>
    import("react-syntax-highlighter/dist/esm/prism-async-light").then((mod) => {
      // When loaded, register common languages
      return Promise.all([
        import("react-syntax-highlighter/dist/esm/languages/prism/javascript"),
        import("react-syntax-highlighter/dist/esm/languages/prism/typescript"),
        import("react-syntax-highlighter/dist/esm/languages/prism/python"),
        import("react-syntax-highlighter/dist/esm/languages/prism/bash"),
        import("react-syntax-highlighter/dist/esm/languages/prism/json"),
        import("react-syntax-highlighter/dist/esm/languages/prism/markdown"),
        import("react-syntax-highlighter/dist/esm/languages/prism/css"),
        import("react-syntax-highlighter/dist/esm/languages/prism/yaml"),
        import("react-syntax-highlighter/dist/esm/languages/prism/sql"),
      ]).then(() => mod);
    }),
  { ssr: false, loading: () => null },
);

function DiffSyntaxHighlight({
  language,
  lines,
  diffText,
}: {
  language: string;
  lines: RenderableFileDiffLine[];
  diffText: string;
}) {
  const isDark = useThemeValue() === "dark";
  const [themeModule, setThemeModule] = useState<{
    theme: Record<string, React.CSSProperties>;
  } | null>(null);
  const [createElement, setCreateElement] = useState<
    ((props: Record<string, unknown>) => ReactNode) | null
  >(null);
  const [renderer, setRenderer] = useState<
    ((args: { rows: unknown[]; stylesheet: unknown; useInlineStyles: boolean }) => ReactNode) | null
  >(null);

  useEffect(() => {
    const themeName = isDark ? "one-dark" : "one-light";
    import(`react-syntax-highlighter/dist/esm/styles/prism/${themeName}`)
      .then((m) => setThemeModule({ theme: m.default }))
      .catch(() => setThemeModule(null));
  }, [isDark]);

  useEffect(() => {
    import("react-syntax-highlighter/dist/esm/create-element")
      .then((m) => setCreateElement(() => m.default))
      .catch(() => {});
  }, []);

  // Build renderer once createElement is available
  useEffect(() => {
    if (!createElement || !lines) {
      setRenderer(null);
      return;
    }
    const localLines = lines;
    const localCreateEl = createElement;
    setRenderer(() => (args: { rows: unknown[]; stylesheet: unknown; useInlineStyles: boolean }) => (
      <DiffLineTable
        lines={localLines}
        renderCode={(line, index) => {
          const node = (args.rows as Record<string, unknown>[])[index];
          if (!node) return line.content || " ";
          return localCreateEl({
            node,
            stylesheet: args.stylesheet,
            useInlineStyles: args.useInlineStyles,
            key: `diff-code-${index}`,
          });
        }}
      />
    ));
  }, [createElement, lines]);

  if (!themeModule) {
    return <PlainDiffLines lines={lines} />;
  }

  return (
    <SyntaxHighlighter
      language={language}
      style={themeModule.theme}
      PreTag="div"
      CodeTag="div"
      customStyle={{
        background: "transparent",
        margin: 0,
        padding: 0,
        overflow: "visible",
        fontFamily: CODE_FONT_STACK,
        fontSize: "11px",
        lineHeight: "1.25rem",
      }}
      codeTagProps={{ style: { background: "transparent", fontFamily: CODE_FONT_STACK } }}
      renderer={renderer ?? undefined}
    >
      {diffText}
    </SyntaxHighlighter>
  );
}

function PlainDiffLines({ lines }: { lines: RenderableFileDiffLine[] }) {
  return (
    <DiffLineTable lines={lines} renderCode={(line) => line.content || " "} />
  );
}

function DiffLineTable({
  lines,
  renderCode,
}: {
  lines: RenderableFileDiffLine[];
  renderCode: (line: RenderableFileDiffLine, index: number) => ReactNode;
}) {
  return (
    <table className="w-full border-collapse font-mono text-[11px] leading-5">
      <tbody>
        {lines.map((line, index) => (
          <DiffLineRow key={`${line.old_lineno ?? ""}:${line.new_lineno ?? ""}:${index}`} line={line}>
            {renderCode(line, index)}
          </DiffLineRow>
        ))}
      </tbody>
    </table>
  );
}

function DiffLineRow({
  line,
  children,
}: {
  line: RenderableFileDiffLine;
  children: ReactNode;
}) {
  const kind = line.kind === "add" || line.kind === "delete" ? line.kind : "context";
  const marker = kind === "add" ? "+" : kind === "delete" ? "-" : " ";
  return (
    <tr
      className={cn(
        "border-0",
        kind === "add" && "bg-emerald-500/[0.09]",
        kind === "delete" && "bg-rose-500/[0.09]",
      )}
    >
      <td className="w-10 select-none border-r border-border/35 px-1.5 text-right text-muted-foreground/55">
        {line.old_lineno ?? ""}
      </td>
      <td className="w-10 select-none border-r border-border/35 px-1.5 text-right text-muted-foreground/55">
        {line.new_lineno ?? ""}
      </td>
      <td
        className={cn(
          "w-5 select-none px-1 text-center",
          kind === "add" && "text-emerald-500",
          kind === "delete" && "text-rose-500",
          kind === "context" && "text-muted-foreground/45",
        )}
      >
        {marker}
      </td>
      <td className="min-w-[16rem] px-1.5 text-foreground/86">
        <span className="whitespace-pre">{children}</span>
      </td>
    </tr>
  );
}

/* ------------------------------------------------------------------ */
/*  ReasoningRow — Expandable thinking text with preview               */
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
  const wasStreamingRef = useRef(isStreaming);
  const [justCompleted, setJustCompleted] = useState(false);
  const { t } = useTranslation();

  useEffect(() => {
    if (autoExpand && isStreaming) setExpanded(true);
  }, [autoExpand, isStreaming]);

  // Completion animation
  useEffect(() => {
    if (wasStreamingRef.current && !isStreaming) {
      setJustCompleted(true);
      const timeout = window.setTimeout(() => setJustCompleted(false), 300);
      wasStreamingRef.current = isStreaming;
      return () => window.clearTimeout(timeout);
    }
    wasStreamingRef.current = isStreaming;
  }, [isStreaming]);

  const preview = compactReasoningPreview(text);
  const fallback = isStreaming
    ? t("activity.thinking", "Thinking...")
    : t("activity.thought", "Thought");
  const displayPreview = preview || fallback;
  const displayText = text.length > 300 && !expanded ? text.slice(0, 300) + "..." : text;

  return (
    <div className="rounded-md border border-border bg-muted/30">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-xs text-muted-foreground hover:bg-muted/50 transition-colors"
      >
        {/* Marker */}
        <span
          className={cn(
            "grid h-3.5 w-3.5 shrink-0 place-items-center rounded-full border transition-colors",
            isStreaming
              ? "border-muted-foreground/28 text-muted-foreground/55"
              : "border-emerald-500/28 text-emerald-500/78",
            justCompleted && "shadow-[0_0_0_3px_rgba(16,185,129,0.10)]",
          )}
        >
          {isStreaming ? (
            <Loader2 className="h-2.5 w-2.5 animate-spin" />
          ) : (
            <CheckCircle2 className="h-2.5 w-2.5" />
          )}
        </span>
        <span className="flex-1 truncate text-left italic text-muted-foreground/78">
          {displayPreview}
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
/*  GenericToolRun — Generic tool call display                         */
/* ------------------------------------------------------------------ */

function GenericToolRun({
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
  const family = classifyToolFamily(name);
  const Icon = genericToolIcon(family);
  const label = genericToolLabel(family, status, name);
  const displayName = toolDisplayName(name);

  // Extract detail from args
  let detail = "";
  if (args && typeof args === "object") {
    const record = args as Record<string, unknown>;
    if (family === "content-search") {
      detail = String(record.query ?? record.pattern ?? "");
    } else if (family === "file-search") {
      detail = String(record.glob ?? record.query ?? record.pattern ?? record.path ?? "");
    } else if (family === "list" || family === "read") {
      detail = String(record.path ?? record.file_path ?? "");
    } else if (family === "memory") {
      detail = String(record.query ?? "");
    }
  }
  if (detail) detail = detail.length > 80 ? detail.slice(0, 80) + "..." : detail;

  let iconColor = "text-muted-foreground";
  if (name.startsWith("run_cli_app") || name.startsWith("cli_"))
    iconColor = "text-cyan-400";
  else if (name.startsWith("mcp_"))
    iconColor = "text-indigo-400";

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
        <Icon className={cn("h-3.5 w-3.5 shrink-0", iconColor)} />
        <span className="flex-1 truncate text-left">
          <span className="font-medium text-muted-foreground">{label}</span>
          {detail ? (
            <span className="ml-1 text-muted-foreground/60">{"·"} {detail}</span>
          ) : (
            <span className="ml-1 text-[11px] font-mono text-muted-foreground/60">
              {displayName}
            </span>
          )}
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
            <span className="uppercase tracking-wider font-semibold">Output</span>
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
/*  FileEditRow — File path, edit summary, diff toggle                 */
/* ------------------------------------------------------------------ */

function FileEditRow({
  edit,
  isStreaming,
}: {
  edit: UIFileEdit;
  isStreaming: boolean;
}) {
  const [diffOpen, setDiffOpen] = useState(false);
  const [expandedLines, setExpandedLines] = useState(false);
  const status: ActivityStatus =
    edit.status === "error" ? "error" : edit.status === "done" ? "done" : "running";
  const rowActive = status === "running" && isStreaming;
  const editing = edit.status === "editing";

  const added = edit.added ?? 0;
  const deleted = edit.deleted ?? 0;
  const hasDiff = hasRenderableDiff(edit.diff);
  const showDiff = status === "done" && hasDiff;
  const deleting = edit.operation === "delete";

  const action = deleting
    ? (editing ? "Deleting" : "Deleted")
    : (editing ? "Editing" : "Edited");

  // Parse diff
  const diffHunks = useMemo(() => {
    if (!showDiff || !edit.diff?.text) return [];
    return parseDiffText(edit.diff.text);
  }, [showDiff, edit.diff?.text]);

  const totalLineCount = useMemo(() => countDiffLines(diffHunks), [diffHunks]);
  const shouldAutoCollapse = totalLineCount > AUTO_COLLAPSE_DIFF_LINES || !!edit.diff?.truncated;
  const startsCollapsed = shouldAutoCollapse;
  const shouldRenderDiff = !startsCollapsed || diffOpen;
  const lineLimit = expandedLines || totalLineCount <= INITIAL_VISIBLE_DIFF_LINES
    ? totalLineCount
    : INITIAL_VISIBLE_DIFF_LINES;
  const visibleHunks = useMemo(() => {
    if (!shouldRenderDiff) return [];
    let remaining = lineLimit;
    const hunks: RenderableFileDiffHunk[] = [];
    for (const hunk of diffHunks) {
      if (remaining <= 0) break;
      if (hunk.lines.length <= remaining) {
        hunks.push(hunk);
        remaining -= hunk.lines.length;
      } else {
        hunks.push({ ...hunk, lines: hunk.lines.slice(0, remaining) });
        remaining = 0;
      }
    }
    return hunks;
  }, [diffHunks, shouldRenderDiff, lineLimit]);

  const hiddenLineCount = Math.max(0, totalLineCount - lineLimit);
  const language = useMemo(() => codeLanguageFromPath(edit.path), [edit.path]);
  const { t } = useTranslation();

  // Reset expansion on diff change
  useEffect(() => {
    setDiffOpen(false);
    setExpandedLines(false);
  }, [edit.diff?.text]);

  const diffText = edit.diff?.text ?? "";

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
        onClick={() => {
          if (showDiff && shouldAutoCollapse) setDiffOpen((v) => !v);
        }}
        className={cn(
          "flex w-full items-center gap-2 px-2.5 py-1.5 text-xs",
          showDiff && shouldAutoCollapse && "hover:bg-muted/30 transition-colors cursor-pointer",
        )}
      >
        <FileText className="h-3.5 w-3.5 shrink-0 text-amber-400" />
        <span className="shrink-0 text-muted-foreground">{action}</span>
        <span className="min-w-0 flex-1 truncate text-left font-mono text-[11px]">
          {edit.path || "(unknown path)"}
        </span>
        {hasDiff && !edit.binary && (
          <DiffPair added={added} deleted={deleted} />
        )}
        {edit.binary && (
          <span className="text-[10px] text-muted-foreground/50">binary</span>
        )}
        <StatusIcon status={status} />
        {showDiff && shouldAutoCollapse && (
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

      {/* Diff (always visible when not auto-collapsed, toggleable otherwise) */}
      {showDiff && shouldRenderDiff && visibleHunks.length > 0 && (
        <div className="border-t border-border">
          <div className="overflow-hidden rounded-b-md bg-background/80">
            {visibleHunks.map((hunk, i) => (
              <div key={`${hunk.old_start}-${hunk.new_start}-${i}`} className={cn(i > 0 && "border-t border-border/45")}>
                <div className="overflow-x-auto">
                  <DiffSyntaxHighlight
                    language={language}
                    lines={hunk.lines}
                    diffText={diffText}
                  />
                </div>
              </div>
            ))}
            {hiddenLineCount > 0 && (
              <div className="border-t border-border/45 bg-muted/30 px-2 py-1">
                <button
                  type="button"
                  className="inline-flex items-center gap-1 rounded px-1 py-0.5 text-[11px] font-medium text-muted-foreground hover:bg-muted/65 hover:text-foreground transition-colors"
                  onClick={() => setExpandedLines(true)}
                >
                  <ChevronDown className="h-3 w-3" />
                  {t("activity.showMoreLines", "Show {{count}} more lines", { count: hiddenLineCount })}
                </button>
              </div>
            )}
            {expandedLines && hiddenLineCount === 0 && totalLineCount > INITIAL_VISIBLE_DIFF_LINES && (
              <div className="border-t border-border/45 bg-muted/30 px-2 py-1">
                <button
                  type="button"
                  className="inline-flex items-center gap-1 rounded px-1 py-0.5 text-[11px] font-medium text-muted-foreground hover:bg-muted/65 hover:text-foreground transition-colors"
                  onClick={() => setExpandedLines(false)}
                >
                  <ChevronUp className="h-3 w-3" />
                  {t("activity.showFewerLines", "Show fewer lines")}
                </button>
              </div>
            )}
            {edit.diff?.truncated && (
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1 border-t border-border/45 bg-muted/35 px-2 py-1 text-[11px] text-muted-foreground">
                <span>{t("activity.diffTruncated", "Diff truncated. Open the file for the full change.")}</span>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Collapsed diff toggle */}
      {showDiff && shouldAutoCollapse && !shouldRenderDiff && (
        <div className="border-t border-border px-2.5 py-1">
          <button
            type="button"
            onClick={() => setDiffOpen((v) => !v)}
            className="flex w-full items-center gap-2 rounded-md border border-border/45 bg-muted/35 px-2 py-1 text-left text-[11px] font-medium text-muted-foreground hover:bg-muted/50 transition-colors"
          >
            <ChevronRight className={cn("h-3 w-3 shrink-0 transition-transform", diffOpen && "rotate-90")} />
            <span className="min-w-0 flex-1">
              {shouldAutoCollapse
                ? t("activity.viewLargeDiff", "View large diff")
                : t("activity.viewDiff", "View diff")}
            </span>
            <span className="shrink-0 text-muted-foreground/65">
              {edit.diff?.truncated ? `${totalLineCount}+` : totalLineCount} {t("activity.lines", "lines")}
            </span>
          </button>
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  WebSearchRow — Search query, results summary, source list          */
/* ------------------------------------------------------------------ */

interface WebSearchSource {
  title: string;
  href: string;
  host: string;
  displayUrl: string;
}

function extractSearchQuery(event: ToolProgressEvent): string {
  const args = event.arguments;
  if (args && typeof args === "object") {
    const record = args as Record<string, unknown>;
    return String(record.query ?? record.q ?? record.search ?? "");
  }
  return "";
}

function extractSearchSources(result: unknown): WebSearchSource[] {
  if (!result) return [];

  let items: unknown[] = [];

  // Try to extract structured results
  if (Array.isArray(result)) {
    items = result;
  } else if (result && typeof result === "object") {
    const record = result as Record<string, unknown>;
    for (const key of ["results", "items", "sources", "data"]) {
      if (Array.isArray(record[key])) {
        items = [...items, ...record[key]];
      }
    }
    // If no structured results, try text parsing
    if (items.length === 0) {
      for (const key of ["content", "text", "result"]) {
        if (typeof record[key] === "string") {
          items = [...items, ...parseTextSources(record[key])];
        }
      }
    }
  } else if (typeof result === "string") {
    items = parseTextSources(result);
  }

  const seen = new Set<string>();
  const sources: WebSearchSource[] = [];
  for (const item of items) {
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    const record = item as Record<string, unknown>;
    const title = String(record.title ?? record.name ?? "");
    const href = String(record.url ?? record.href ?? record.link ?? "");
    if (!href || seen.has(href)) continue;
    seen.add(href);
    try {
      const url = new URL(href);
      sources.push({
        title: title || url.hostname,
        href,
        host: url.hostname.replace(/^www\./, ""),
        displayUrl: url.hostname.replace(/^www\./, "") + url.pathname.replace(/\/$/, ""),
      });
    } catch {
      sources.push({ title: title || href, href, host: href, displayUrl: href });
    }
    if (sources.length >= 8) break;
  }
  return sources;
}

function parseTextSources(text: string): Array<{ title: string; url: string }> {
  const sources: Array<{ title: string; url: string }> = [];
  const lines = text.split(/\r?\n/);

  for (const line of lines) {
    // Markdown links: [title](url)
    const mdMatch = /\[([^\]]+)]\((https?:\/\/[^)]+)\)/.exec(line);
    if (mdMatch) {
      sources.push({ title: mdMatch[1].trim(), url: mdMatch[2] });
      continue;
    }
    // Inline URLs
    const urlMatch = line.match(/https?:\/\/[^\s<>"']+/i);
    if (urlMatch) {
      const url = urlMatch[0].replace(/[),.;\]}]+$/, "");
      const title = line.replace(url, "").replace(/[\s:|\-–—]+$/, "").trim() || url;
      sources.push({ title, url });
    }
  }
  return sources;
}

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
  const query = extractSearchQuery(event);
  const sources = useMemo(() => extractSearchSources(event.result), [event.result]);
  const isXSearch = /x_search/i.test(name);

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
        onClick={() => status === "done" && sources.length > 0 && setResultsOpen((v) => !v)}
        className={cn(
          "flex w-full items-center gap-2 px-2.5 py-1.5 text-xs",
          status === "done" && sources.length > 0 && "hover:bg-muted/30 transition-colors cursor-pointer",
        )}
      >
        {isXSearch ? (
          <span className="h-3.5 w-3.5 shrink-0 flex items-center justify-center text-[10px] font-bold text-blue-400">X</span>
        ) : (
          <Search className="h-3.5 w-3.5 shrink-0 text-blue-400" />
        )}
        <span className="flex-1 truncate text-left">
          {query
            ? `${isXSearch ? "X search" : "Web search"}: "${query}"`
            : toolDisplayName(name)}
        </span>
        <StatusIcon status={status} />
        {status === "done" && sources.length > 0 && (
          resultsOpen ? (
            <ChevronDown className="h-3 w-3 shrink-0 text-muted-foreground/50" />
          ) : (
            <ChevronRight className="h-3 w-3 shrink-0 text-muted-foreground/50" />
          )
        )}
      </button>

      {/* Source list */}
      {resultsOpen && sources.length > 0 && (
        <div className="border-t border-border">
          {sources.map((source, i) => (
            <a
              key={source.href}
              href={source.href}
              target="_blank"
              rel="noreferrer noopener"
              className={cn(
                "flex items-center gap-2 px-2.5 py-1.5 text-xs hover:bg-muted/30 transition-colors",
                i > 0 && "border-t border-border/40",
              )}
            >
              <Globe2 className="h-3.5 w-3.5 shrink-0 text-muted-foreground/50" />
              <span className="flex-1 min-w-0">
                <span className="block truncate font-medium text-foreground/82">
                  {source.title}
                </span>
                <span className="block truncate font-mono text-[10px] text-muted-foreground/60">
                  {source.displayUrl}
                </span>
              </span>
              <ExternalLink className="h-3 w-3 shrink-0 text-muted-foreground/30" />
            </a>
          ))}
        </div>
      )}

      {/* Result preview (when no sources extracted) */}
      {resultsOpen && sources.length === 0 && status === "done" && event.result !== undefined && (
        <div className="border-t border-border px-2.5 py-1.5">
          <pre className="text-[11px] text-muted-foreground font-mono whitespace-pre-wrap break-all max-h-48 overflow-y-auto">
            {formatResult(event.result)}
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
                  key={event.call_id ?? `web-${i}`}
                  event={event}
                  isStreaming={isStreaming}
                />
              );
            }
            if (isFileEditTool(name)) {
              // File edit tools are handled via fileEdits, not tool events
              return (
                <GenericToolRun
                  key={event.call_id ?? `generic-${i}`}
                  event={event}
                  isStreaming={isStreaming}
                />
              );
            }
            return (
              <GenericToolRun
                key={event.call_id ?? `generic-${i}`}
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
  turnId?: string;
  isStreaming?: boolean;
}

export function AgentActivityCluster({
  messages,
  turnId,
  isStreaming,
}: AgentActivityClusterProps) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!isStreaming) return;
    setNow(Date.now());
    const interval = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(interval);
  }, [isStreaming]);

  const groups = useMemo(() => {
    let traceMessages = messages.filter((m) => m.kind === "trace");
    if (turnId) {
      traceMessages = traceMessages.filter((m) => m.turnId === turnId);
    }

    const reasoningMessages = messages.filter(
      (m) => isReasoningOnlyAssistant(m) && (!turnId || m.turnId === turnId),
    );

    const allActivityMessages = [...traceMessages, ...reasoningMessages].sort(
      (a, b) => a.createdAt - b.createdAt,
    );

    if (allActivityMessages.length === 0) return [];

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