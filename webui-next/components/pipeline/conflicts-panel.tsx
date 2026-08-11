"use client";

import { useState, useEffect, useCallback, useMemo } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";
import {
  AlertTriangle,
  CheckCircle,
  ChevronDown,
  ChevronUp,
  Loader2,
  RefreshCw,
  GitCompare,
  Bot,
  Database,
  Mic,
  XCircle,
  Filter,
} from "lucide-react";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Which source provided the value for a given field. */
export type ConflictSource = "llm" | "api" | "asr";

/** Severity of a conflict. */
export type ConflictSeverity = "high" | "medium" | "low";

/** A single field-level conflict between multiple data sources. */
export interface SourceConflict {
  id: string;
  entityName: string;
  fieldName: string;
  llmValue: string | null;
  apiValue: string | null;
  asrValue: string | null;
  severity: ConflictSeverity;
  sessionId?: string;
  jobId?: string;
  createdAt?: string;
  /** Backend-suggested winning source, if any. */
  suggestedSource?: ConflictSource;
}

/** A resolution decision for a single conflict. */
export interface ConflictResolution {
  id: string;
  conflictId: string;
  entityName: string;
  fieldName: string;
  selectedSource: ConflictSource;
  resolvedAt: number;
  resolvedBy?: string;
}

/** Payload sent to POST /api/conflicts/resolve */
interface ResolvePayload {
  conflictId: string;
  selectedSource: ConflictSource;
}

/** Response from POST /api/conflicts/resolve */
interface ResolveResponse {
  success: boolean;
  id: string;
  message?: string;
}

/** Response from GET /api/conflicts */
interface ConflictsListResponse {
  conflicts: SourceConflict[];
  total: number;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const SOURCE_LABELS: Record<ConflictSource, string> = {
  llm: "LLM",
  api: "API",
  asr: "ASR",
};

const SOURCE_ICONS: Record<ConflictSource, typeof Bot> = {
  llm: Bot,
  api: Database,
  asr: Mic,
};

const SOURCE_DESCRIPTIONS: Record<ConflictSource, string> = {
  llm: "Large Language Model output",
  api: "External API / structured data",
  asr: "Automatic Speech Recognition transcript",
};

const SEVERITY_CONFIG: Record<
  ConflictSeverity,
  { variant: "destructive" | "default" | "secondary"; label: string }
> = {
  high: { variant: "destructive", label: "High" },
  medium: { variant: "default", label: "Medium" },
  low: { variant: "secondary", label: "Low" },
};

type SortField = "entityName" | "fieldName" | "severity" | "createdAt";
type SortDir = "asc" | "desc";

// ---------------------------------------------------------------------------
// API helpers — REST endpoints (not GraphQL)
// ---------------------------------------------------------------------------

async function fetchConflicts(
  status?: string,
  signal?: AbortSignal
): Promise<ConflictsListResponse> {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  const query = params.toString();
  const url = `/api/conflicts${query ? `?${query}` : ""}`;

  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    signal,
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "Unknown error");
    throw new Error(`Failed to fetch conflicts: ${res.status} ${text}`);
  }

  return res.json();
}

async function postResolveConflict(payload: ResolvePayload): Promise<ResolveResponse> {
  const res = await fetch("/api/conflicts/resolve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "Unknown error");
    throw new Error(`Failed to resolve conflict: ${res.status} ${text}`);
  }

  return res.json();
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString("zh-CN", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatTime(ts: number): string {
  return new Date(ts).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function hasActualConflict(conflict: SourceConflict): boolean {
  const values = [conflict.llmValue, conflict.apiValue, conflict.asrValue].filter(
    (v) => v !== null
  );
  return new Set(values).size > 1;
}

// ---------------------------------------------------------------------------
// Sub-component: Source value cell for side-by-side comparison
// ---------------------------------------------------------------------------

interface SourceValueCellProps {
  conflict: SourceConflict;
  source: ConflictSource;
  isSelected: boolean;
  isSuggested: boolean;
  onSelect: () => void;
}

function SourceValueCell({
  conflict,
  source,
  isSelected,
  isSuggested,
  onSelect,
}: SourceValueCellProps) {
  const rawValue =
    conflict[`${source}Value` as keyof Pick<SourceConflict, "llmValue" | "apiValue" | "asrValue">];
  const value = rawValue as string | null;
  const Icon = SOURCE_ICONS[source];
  const isNull = value === null;

  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "group relative flex flex-col items-start rounded-lg border-2 p-3 text-left transition-all w-full",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
        isSelected
          ? "border-primary bg-primary/5 shadow-sm"
          : "border-border bg-card hover:border-muted-foreground/30 hover:bg-muted/30"
      )}
    >
      {/* Header: radio indicator + icon + label + badge */}
      <div className="flex items-center gap-2 w-full mb-2">
        <span
          className={cn(
            "flex h-4 w-4 shrink-0 items-center justify-center rounded-full border-2 transition-colors",
            isSelected
              ? "border-primary bg-primary"
              : "border-muted-foreground/30 group-hover:border-muted-foreground/50"
          )}
          aria-hidden="true"
        >
          {isSelected && <span className="h-1.5 w-1.5 rounded-full bg-primary-foreground" />}
        </span>
        <Icon className="h-4 w-4 text-muted-foreground shrink-0" />
        <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">
          {SOURCE_LABELS[source]}
        </span>
        {isSuggested && (
          <Badge variant="outline" className="ml-auto text-[10px] px-1.5 py-0 h-4 border-primary/40 text-primary">
            Suggested
          </Badge>
        )}
        {isSelected && !isSuggested && (
          <Badge variant="default" className="ml-auto text-[10px] px-1.5 py-0 h-4">
            Selected
          </Badge>
        )}
      </div>

      {/* Value display */}
      <div
        className={cn(
          "w-full rounded bg-muted/60 px-2.5 py-2 text-sm font-mono break-all whitespace-pre-wrap min-h-[2.5rem] border",
          isNull ? "text-muted-foreground italic border-dashed" : "border-transparent",
          isSelected && "bg-primary/10"
        )}
      >
        {isNull ? "(no value)" : value}
      </div>
    </button>
  );
}

// ---------------------------------------------------------------------------
// Sub-component: Single conflict card (expandable)
// ---------------------------------------------------------------------------

interface ConflictCardProps {
  conflict: SourceConflict;
  selectedSource: ConflictSource;
  onSelectSource: (source: ConflictSource) => void;
  onResolve: (conflictId: string, source: ConflictSource) => void;
  isResolving: boolean;
  expanded: boolean;
  onToggleExpand: () => void;
}

function ConflictCard({
  conflict,
  selectedSource,
  onSelectSource,
  onResolve,
  isResolving,
  expanded,
  onToggleExpand,
}: ConflictCardProps) {
  const severity = SEVERITY_CONFIG[conflict.severity];

  return (
    <Card
      className={cn(
        "transition-all",
        conflict.severity === "high" && "border-l-4 border-l-destructive",
        conflict.severity === "medium" && "border-l-4 border-l-amber-500",
        conflict.severity === "low" && "border-l-4 border-l-muted-foreground/20"
      )}
    >
      {/* Summary row (always visible) */}
      <div
        className="flex items-center gap-3 px-4 py-3 cursor-pointer select-none"
        onClick={onToggleExpand}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onToggleExpand();
          }
        }}
      >
        {/* Expand toggle */}
        <span className="shrink-0">
          {expanded ? (
            <ChevronUp className="h-4 w-4 text-muted-foreground" />
          ) : (
            <ChevronDown className="h-4 w-4 text-muted-foreground" />
          )}
        </span>

        {/* Severity indicator */}
        <AlertTriangle
          className={cn(
            "h-4 w-4 shrink-0",
            conflict.severity === "high" && "text-destructive",
            conflict.severity === "medium" && "text-amber-500",
            conflict.severity === "low" && "text-muted-foreground"
          )}
        />

        {/* Entity name + field name */}
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium truncate">{conflict.entityName}</span>
            <span className="text-xs font-mono text-muted-foreground bg-muted px-1.5 py-0.5 rounded shrink-0">
              {conflict.fieldName}
            </span>
          </div>
          {conflict.sessionId && (
            <div className="text-xs text-muted-foreground truncate mt-0.5">
              Session: {conflict.sessionId.slice(0, 8)}...
            </div>
          )}
        </div>

        {/* Severity badge */}
        <Badge variant={severity.variant} className="shrink-0 text-xs">
          {severity.label}
        </Badge>

        {/* Quick resolve button (visible when collapsed) */}
        {!expanded && (
          <Button
            variant="outline"
            size="sm"
            className="shrink-0 ml-1"
            onClick={(e) => {
              e.stopPropagation();
              onResolve(conflict.id, selectedSource);
            }}
            disabled={isResolving}
          >
            {isResolving ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <>
                <CheckCircle className="h-3.5 w-3.5 mr-1" />
                Resolve
              </>
            )}
          </Button>
        )}
      </div>

      {/* Expanded detail view */}
      {expanded && (
        <div className="px-4 pb-4 border-t">
          {/* Side-by-side value comparison */}
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mt-4">
            {(Object.keys(SOURCE_LABELS) as ConflictSource[]).map((source) => (
              <SourceValueCell
                key={source}
                conflict={conflict}
                source={source}
                isSelected={selectedSource === source}
                isSuggested={conflict.suggestedSource === source}
                onSelect={() => onSelectSource(source)}
              />
            ))}
          </div>

          {/* All-sources-agree notice */}
          {!hasActualConflict(conflict) && (
            <div className="flex items-center gap-2 mt-3 p-2 rounded bg-green-50 dark:bg-green-950/20 border border-green-200 dark:border-green-800 text-sm text-green-700 dark:text-green-400">
              <CheckCircle className="h-4 w-4 shrink-0" />
              All sources agree &mdash; no actual conflict detected
            </div>
          )}

          {/* Conflict summary */}
          {hasActualConflict(conflict) && (
            <div className="flex items-center gap-2 mt-3 p-2 rounded bg-amber-50 dark:bg-amber-950/20 border border-amber-200 dark:border-amber-800 text-sm text-amber-700 dark:text-amber-400">
              <AlertTriangle className="h-4 w-4 shrink-0" />
              {(() => {
                const sources = (Object.keys(SOURCE_LABELS) as ConflictSource[]).filter(
                  (s) =>
                    conflict[`${s}Value` as keyof Pick<SourceConflict, "llmValue" | "apiValue" | "asrValue">] !== null
                );
                return `${sources.length} sources disagree on "${conflict.fieldName}"`;
              })()}
            </div>
          )}

          {/* Metadata */}
          {conflict.createdAt && (
            <div className="text-xs text-muted-foreground mt-3">
              Detected: {formatDateTime(conflict.createdAt)}
            </div>
          )}

          {/* Resolve button */}
          <div className="flex gap-2 mt-4">
            <Button
              variant="default"
              size="sm"
              className="flex-1"
              onClick={() => onResolve(conflict.id, selectedSource)}
              disabled={isResolving}
            >
              {isResolving ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Resolving...
                </>
              ) : (
                <>
                  <CheckCircle className="mr-2 h-4 w-4" />
                  Resolve with {SOURCE_LABELS[selectedSource]}
                </>
              )}
            </Button>
          </div>
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Sub-component: Resolution history entry
// ---------------------------------------------------------------------------

interface HistoryEntryProps {
  entry: ConflictResolution;
}

function HistoryEntry({ entry }: HistoryEntryProps) {
  const Icon = SOURCE_ICONS[entry.selectedSource];

  return (
    <div className="flex items-center justify-between rounded-md border p-3 text-sm">
      <div className="flex items-center gap-2.5 min-w-0">
        <CheckCircle className="h-4 w-4 shrink-0 text-green-600" />
        <div className="min-w-0">
          <div className="font-medium truncate">{entry.entityName}</div>
          <div className="text-xs text-muted-foreground flex items-center gap-1.5 flex-wrap">
            <span className="font-mono bg-muted px-1 py-0.5 rounded text-[11px]">
              {entry.fieldName}
            </span>
            <span className="text-muted-foreground/50">&middot;</span>
            <span>{formatTime(entry.resolvedAt)}</span>
            {entry.resolvedBy && (
              <>
                <span className="text-muted-foreground/50">&middot;</span>
                <span>{entry.resolvedBy}</span>
              </>
            )}
          </div>
        </div>
      </div>
      <div className="flex items-center gap-2 shrink-0 ml-2">
        <Icon className="h-3.5 w-3.5 text-muted-foreground" />
        <Badge variant="outline" className="text-xs font-medium">
          {SOURCE_LABELS[entry.selectedSource]}
        </Badge>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export interface ConflictsPanelProps {
  className?: string;
  maxHistoryEntries?: number;
  pollIntervalMs?: number;
}

export function ConflictsPanel({
  className,
  maxHistoryEntries = 50,
  pollIntervalMs = 10000,
}: ConflictsPanelProps) {
  // -- State ---------------------------------------------------------------
  const [conflicts, setConflicts] = useState<SourceConflict[]>([]);
  const [selectedSources, setSelectedSources] = useState<Record<string, ConflictSource>>({});
  const [resolving, setResolving] = useState<Set<string>>(new Set());
  const [history, setHistory] = useState<ConflictResolution[]>([]);
  const [historyExpanded, setHistoryExpanded] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [filterSeverity, setFilterSeverity] = useState<ConflictSeverity | "all">("all");

  // -- Fetch conflicts -----------------------------------------------------
  const loadConflicts = useCallback(async () => {
    try {
      setError(null);
      const data = await fetchConflicts("pending");
      setConflicts(data.conflicts);

      // Initialize default selections (prefer suggested source, then LLM)
      setSelectedSources((prev) => {
        const next = { ...prev };
        for (const c of data.conflicts) {
          if (!(c.id in next)) {
            next[c.id] = c.suggestedSource ?? "llm";
          }
        }
        return next;
      });
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      setError(err instanceof Error ? err.message : "Failed to load conflicts");
    } finally {
      setLoading(false);
    }
  }, []);

  // Initial load
  useEffect(() => {
    const ac = new AbortController();
    const load = async () => {
      try {
        setError(null);
        const data = await fetchConflicts("pending", ac.signal);
        setConflicts(data.conflicts);
        setSelectedSources((prev) => {
          const next = { ...prev };
          for (const c of data.conflicts) {
            if (!(c.id in next)) {
              next[c.id] = c.suggestedSource ?? "llm";
            }
          }
          return next;
        });
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setError(err instanceof Error ? err.message : "Failed to load conflicts");
      } finally {
        setLoading(false);
      }
    };
    load();
    return () => ac.abort();
  }, []);

  // Polling
  useEffect(() => {
    if (pollIntervalMs <= 0) return;
    const interval = setInterval(loadConflicts, pollIntervalMs);
    return () => clearInterval(interval);
  }, [loadConflicts, pollIntervalMs]);

  // -- Actions -------------------------------------------------------------

  const handleSelectSource = useCallback(
    (conflictId: string, source: ConflictSource) => {
      setSelectedSources((prev) => ({ ...prev, [conflictId]: source }));
    },
    []
  );

  const handleResolve = useCallback(
    async (conflictId: string, selectedSource: ConflictSource) => {
      setResolving((prev) => new Set(prev).add(conflictId));
      setError(null);

      try {
        const conflict = conflicts.find((c) => c.id === conflictId);
        await postResolveConflict({ conflictId, selectedSource });

        // Add to local history
        const entry: ConflictResolution = {
          id: `${conflictId}-${Date.now()}`,
          conflictId,
          entityName: conflict?.entityName ?? "Unknown",
          fieldName: conflict?.fieldName ?? "Unknown",
          selectedSource,
          resolvedAt: Date.now(),
        };

        setHistory((prev) => [entry, ...prev].slice(0, maxHistoryEntries));

        // Remove from pending
        setConflicts((prev) => prev.filter((c) => c.id !== conflictId));
        setExpandedIds((prev) => {
          const next = new Set(prev);
          next.delete(conflictId);
          return next;
        });
        setSelectedSources((prev) => {
          const next = { ...prev };
          delete next[conflictId];
          return next;
        });
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to resolve conflict");
      } finally {
        setResolving((prev) => {
          const next = new Set(prev);
          next.delete(conflictId);
          return next;
        });
      }
    },
    [conflicts, maxHistoryEntries]
  );

  const handleToggleExpand = useCallback((conflictId: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(conflictId)) {
        next.delete(conflictId);
      } else {
        next.add(conflictId);
      }
      return next;
    });
  }, []);

  const handleExpandAll = useCallback(() => {
    if (expandedIds.size === conflicts.length) {
      setExpandedIds(new Set());
    } else {
      setExpandedIds(new Set(conflicts.map((c) => c.id)));
    }
  }, [expandedIds.size, conflicts]);

  const handleResolveAll = useCallback(async () => {
    for (const conflict of conflicts) {
      const source = selectedSources[conflict.id] ?? "llm";
      await handleResolve(conflict.id, source);
    }
  }, [conflicts, selectedSources, handleResolve]);

  // -- Derived state -------------------------------------------------------

  const severityOrder: Record<ConflictSeverity, number> = { high: 0, medium: 1, low: 2 };

  const filteredConflicts = useMemo(() => {
    let result = [...conflicts];
    if (filterSeverity !== "all") {
      result = result.filter((c) => c.severity === filterSeverity);
    }
    result.sort((a, b) => severityOrder[a.severity] - severityOrder[b.severity]);
    return result;
  }, [conflicts, filterSeverity]);

  const pendingCount = conflicts.length;
  const highCount = useMemo(
    () => conflicts.filter((c) => c.severity === "high").length,
    [conflicts]
  );
  const mediumCount = useMemo(
    () => conflicts.filter((c) => c.severity === "medium").length,
    [conflicts]
  );
  const allResolving = useMemo(
    () =>
      filteredConflicts.length > 0 &&
      filteredConflicts.every((c) => resolving.has(c.id)),
    [filteredConflicts, resolving]
  );

  // -- Render --------------------------------------------------------------

  return (
    <div className={cn("space-y-4", className)}>
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 flex-wrap">
          <h2 className="text-lg font-semibold flex items-center gap-2">
            <GitCompare className="h-5 w-5 text-muted-foreground" />
            Conflicts Resolution
          </h2>
          {pendingCount > 0 && (
            <Badge variant="destructive" className="animate-pulse">
              <AlertTriangle className="mr-1 h-3 w-3" />
              {pendingCount} pending
            </Badge>
          )}
          {highCount > 0 && (
            <Badge variant="destructive" className="text-xs">
              {highCount} high
            </Badge>
          )}
          {mediumCount > 0 && (
            <Badge variant="default" className="text-xs">
              {mediumCount} medium
            </Badge>
          )}
        </div>
        <Button variant="outline" size="sm" onClick={loadConflicts} disabled={loading}>
          <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
        </Button>
      </div>

      {/* Error Banner */}
      {error && (
        <div className="flex items-center justify-between rounded-md bg-destructive/10 border border-destructive/30 p-3 text-sm text-destructive">
          <span className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 shrink-0" />
            {error}
          </span>
          <Button
            variant="ghost"
            size="sm"
            className="h-auto p-0 text-destructive underline hover:bg-transparent"
            onClick={() => setError(null)}
          >
            Dismiss
          </Button>
        </div>
      )}

      {/* Loading State */}
      {loading && conflicts.length === 0 && (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center justify-center py-16 text-center">
            <Loader2 className="h-8 w-8 text-muted-foreground animate-spin mb-3" />
            <p className="text-sm text-muted-foreground">Loading conflicts...</p>
          </CardContent>
        </Card>
      )}

      {/* Empty State */}
      {!loading && conflicts.length === 0 && history.length === 0 && (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center justify-center py-16 text-center">
            <CheckCircle className="h-10 w-10 text-muted-foreground/50 mb-3" />
            <p className="text-sm text-muted-foreground font-medium">No pending conflicts</p>
            <p className="text-xs text-muted-foreground mt-1 max-w-md">
              When the pipeline detects conflicting values between LLM, API, and ASR
              sources, they will appear here for resolution. You can compare values
              side by side and choose which source to trust.
            </p>
          </CardContent>
        </Card>
      )}

      {/* Toolbar: filters + batch actions */}
      {conflicts.length > 0 && (
        <div className="flex items-center gap-2 flex-wrap">
          {/* Severity filter */}
          <div className="flex items-center gap-1.5">
            <Filter className="h-3.5 w-3.5 text-muted-foreground" />
            <select
              className="h-8 rounded-md border border-input bg-background px-2 text-xs focus:outline-none focus:ring-2 focus:ring-ring"
              value={filterSeverity}
              onChange={(e) =>
                setFilterSeverity(e.target.value as ConflictSeverity | "all")
              }
            >
              <option value="all">All severities</option>
              <option value="high">High</option>
              <option value="medium">Medium</option>
              <option value="low">Low</option>
            </select>
          </div>

          {/* Expand/collapse all */}
          <Button
            variant="ghost"
            size="sm"
            className="text-xs h-8"
            onClick={handleExpandAll}
          >
            {expandedIds.size === conflicts.length ? "Collapse all" : "Expand all"}
          </Button>

          {/* Batch resolve */}
          {filteredConflicts.length > 0 && (
            <Button
              variant="outline"
              size="sm"
              className="text-xs h-8 ml-auto"
              onClick={handleResolveAll}
              disabled={allResolving}
            >
              {allResolving ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin mr-1" />
              ) : (
                <CheckCircle className="h-3.5 w-3.5 mr-1" />
              )}
              Resolve all ({filteredConflicts.length})
            </Button>
          )}
        </div>
      )}

      {/* Conflict List */}
      {conflicts.length > 0 && (
        <ScrollArea className="h-[600px] pr-1">
          <div className="space-y-3">
            {filteredConflicts.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-12 text-center">
                <XCircle className="h-6 w-6 text-muted-foreground/50 mb-2" />
                <p className="text-sm text-muted-foreground">
                  No conflicts match the current filters
                </p>
              </div>
            ) : (
              filteredConflicts.map((conflict) => (
                <ConflictCard
                  key={conflict.id}
                  conflict={conflict}
                  selectedSource={selectedSources[conflict.id] ?? "llm"}
                  onSelectSource={(source) => handleSelectSource(conflict.id, source)}
                  onResolve={handleResolve}
                  isResolving={resolving.has(conflict.id)}
                  expanded={expandedIds.has(conflict.id)}
                  onToggleExpand={() => handleToggleExpand(conflict.id)}
                />
              ))
            )}
          </div>
        </ScrollArea>
      )}

      {/* Resolution History */}
      {history.length > 0 && (
        <Card>
          <CardHeader
            className="cursor-pointer select-none pb-3"
            onClick={() => setHistoryExpanded((v) => !v)}
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <CardTitle className="text-sm font-medium">Resolution History</CardTitle>
                <Badge variant="secondary" className="text-xs">
                  {history.length}
                </Badge>
              </div>
              {historyExpanded ? (
                <ChevronUp className="h-4 w-4 text-muted-foreground" />
              ) : (
                <ChevronDown className="h-4 w-4 text-muted-foreground" />
              )}
            </div>
            <CardDescription>Recently resolved conflicts from this session</CardDescription>
          </CardHeader>

          {historyExpanded && (
            <CardContent className="pt-0">
              <ScrollArea className="h-[300px]">
                <div className="space-y-2">
                  {history.map((entry) => (
                    <HistoryEntry key={entry.id} entry={entry} />
                  ))}
                </div>
              </ScrollArea>
            </CardContent>
          )}
        </Card>
      )}
    </div>
  );
}