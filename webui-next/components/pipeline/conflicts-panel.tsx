"use client";

import { useState, useEffect, useCallback, useMemo } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { apiClient } from "@/lib/api-client";
import { wsClient } from "@/lib/ws-client";
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
} from "lucide-react";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Which source provided the value for a given field. */
export type ConflictSource = "llm" | "api" | "asr";

/** Severity of a conflict. */
export type ConflictSeverity = "high" | "low";

/** A single field-level conflict between multiple data sources. */
export interface SourceConflict {
  /** Unique conflict ID. */
  id: string;
  /** The entity that this conflict belongs to (e.g. book title, chapter name). */
  entityName: string;
  /** The field name that is in conflict. */
  fieldName: string;
  /** The value from the LLM source. */
  llmValue: string | null;
  /** The value from the API source. */
  apiValue: string | null;
  /** The value from the ASR (speech recognition) source. */
  asrValue: string | null;
  /** How severe is this conflict. */
  severity: ConflictSeverity;
  /** The session / job ID this conflict belongs to. */
  sessionId?: string;
  /** When the conflict was created. */
  createdAt?: string;
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

/** GraphQL response shape for source_conflicts query. */
interface SourceConflictsResponse {
  source_conflicts: SourceConflict[];
}

/** GraphQL response shape for conflict resolution mutation. */
interface ResolveConflictResponse {
  resolve_source_conflict: {
    id: string;
    success: boolean;
  };
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

const SEVERITY_COLORS: Record<ConflictSeverity, { variant: "destructive" | "secondary"; label: string }> = {
  high: { variant: "destructive", label: "High" },
  low: { variant: "secondary", label: "Low" },
};

// ---------------------------------------------------------------------------
// GraphQL helpers
// ---------------------------------------------------------------------------

const SOURCE_CONFLICTS_QUERY = `
  query SourceConflicts($status: String) {
    source_conflicts(status: $status) {
      id
      entityName
      fieldName
      llmValue
      apiValue
      asrValue
      severity
      sessionId
      createdAt
    }
  }
`;

const RESOLVE_CONFLICT_MUTATION = `
  mutation ResolveSourceConflict($conflictId: ID!, $selectedSource: String!) {
    resolve_source_conflict(conflictId: $conflictId, selectedSource: $selectedSource) {
      id
      success
    }
  }
`;

async function fetchSourceConflicts(): Promise<SourceConflict[]> {
  const data = await apiClient.graphqlRequest<SourceConflictsResponse>(
    SOURCE_CONFLICTS_QUERY,
    { status: "pending" }
  );
  return data.source_conflicts;
}

async function postResolveConflict(
  conflictId: string,
  selectedSource: ConflictSource
): Promise<ResolveConflictResponse["resolve_source_conflict"]> {
  const data = await apiClient.graphqlRequest<ResolveConflictResponse>(
    RESOLVE_CONFLICT_MUTATION,
    { conflictId, selectedSource }
  );
  return data.resolve_source_conflict;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

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

function valueOrEmpty(value: string | null): string {
  return value ?? "(empty)";
}

function hasConflict(conflict: SourceConflict): boolean {
  const values = [conflict.llmValue, conflict.apiValue, conflict.asrValue].filter(
    (v) => v !== null
  );
  return new Set(values).size > 1;
}

// ---------------------------------------------------------------------------
// Sub-component: Value cell for side-by-side comparison
// ---------------------------------------------------------------------------

interface ValueCellProps {
  conflict: SourceConflict;
  source: ConflictSource;
  isSelected: boolean;
  onSelect: () => void;
}

function ValueCell({ conflict, source, isSelected, onSelect }: ValueCellProps) {
  const value = conflict[`${source}Value` as keyof Pick<SourceConflict, "llmValue" | "apiValue" | "asrValue">] as string | null;
  const Icon = SOURCE_ICONS[source];
  const isNull = value === null;

  return (
    <label
      className={cn(
        "flex flex-col rounded-md border-2 p-3 cursor-pointer transition-all hover:border-primary/50",
        isSelected
          ? "border-primary bg-primary/5 dark:bg-primary/10"
          : "border-border bg-card hover:bg-muted/50"
      )}
    >
      <div className="flex items-center gap-2 mb-2">
        <input
          type="radio"
          name={`conflict-${conflict.id}`}
          value={source}
          checked={isSelected}
          onChange={onSelect}
          className="sr-only"
        />
        <div
          className={cn(
            "flex h-4 w-4 shrink-0 items-center justify-center rounded-full border-2",
            isSelected
              ? "border-primary"
              : "border-muted-foreground/30"
          )}
        >
          {isSelected && (
            <div className="h-2 w-2 rounded-full bg-primary" />
          )}
        </div>
        <Icon className="h-4 w-4 text-muted-foreground" />
        <span className="text-xs font-medium text-muted-foreground">
          {SOURCE_LABELS[source]}
        </span>
        {isSelected && (
          <Badge variant="default" className="ml-auto text-[10px] px-1.5 py-0 h-4">
            Selected
          </Badge>
        )}
      </div>
      <div
        className={cn(
          "text-sm break-all rounded bg-muted/50 px-2 py-1.5 font-mono min-h-[2rem]",
          isNull && "text-muted-foreground italic"
        )}
      >
        {isNull ? "(no value)" : value}
      </div>
    </label>
  );
}

// ---------------------------------------------------------------------------
// Sub-component: Single conflict card
// ---------------------------------------------------------------------------

interface ConflictCardProps {
  conflict: SourceConflict;
  selectedSource: ConflictSource;
  onSelectSource: (source: ConflictSource) => void;
  onResolve: (conflictId: string, source: ConflictSource) => void;
  isResolving: boolean;
}

function ConflictCard({
  conflict,
  selectedSource,
  onSelectSource,
  onResolve,
  isResolving,
}: ConflictCardProps) {
  const severity = SEVERITY_COLORS[conflict.severity];

  return (
    <Card
      className={cn(
        "border-l-4",
        conflict.severity === "high"
          ? "border-l-destructive"
          : "border-l-muted-foreground/30"
      )}
    >
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 min-w-0">
            <GitCompare className="h-4 w-4 shrink-0 text-muted-foreground" />
            <CardTitle className="text-sm font-medium truncate">
              {conflict.entityName}
            </CardTitle>
          </div>
          <Badge variant={severity.variant} className="shrink-0 ml-2 text-xs">
            {severity.label}
          </Badge>
        </div>
        <CardDescription className="flex items-center gap-2">
          <span className="font-mono text-xs bg-muted px-1.5 py-0.5 rounded">
            {conflict.fieldName}
          </span>
          {conflict.sessionId && (
            <span className="text-xs text-muted-foreground truncate">
              Session: {conflict.sessionId.slice(0, 8)}...
            </span>
          )}
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-3">
        {/* Side-by-side value comparison */}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
          <ValueCell
            conflict={conflict}
            source="llm"
            isSelected={selectedSource === "llm"}
            onSelect={() => onSelectSource("llm")}
          />
          <ValueCell
            conflict={conflict}
            source="api"
            isSelected={selectedSource === "api"}
            onSelect={() => onSelectSource("api")}
          />
          <ValueCell
            conflict={conflict}
            source="asr"
            isSelected={selectedSource === "asr"}
            onSelect={() => onSelectSource("asr")}
          />
        </div>

        {/* Difference indicator */}
        {!hasConflict(conflict) && (
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <CheckCircle className="h-3 w-3 text-green-500" />
            All sources agree &mdash; no actual conflict detected
          </div>
        )}

        {/* Resolve button */}
        <Button
          variant="default"
          size="sm"
          className="w-full"
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
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export interface ConflictsPanelProps {
  /** Optional className for the outer container. */
  className?: string;
  /** Maximum number of history entries to display. */
  maxHistoryEntries?: number;
  /** Polling interval in ms for auto-refresh. Set to 0 to disable. */
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

  // -- Fetch conflicts -----------------------------------------------------
  const loadConflicts = useCallback(async () => {
    try {
      setError(null);
      const data = await fetchSourceConflicts();
      setConflicts(data);

      // Initialize default source selections (prefer LLM by default)
      setSelectedSources((prev) => {
        const next = { ...prev };
        for (const c of data) {
          if (!(c.id in next)) {
            next[c.id] = "llm";
          }
        }
        return next;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load conflicts");
    } finally {
      setLoading(false);
    }
  }, []);

  // Initial load
  useEffect(() => {
    loadConflicts();
  }, [loadConflicts]);

  // Polling
  useEffect(() => {
    if (pollIntervalMs <= 0) return;
    const interval = setInterval(loadConflicts, pollIntervalMs);
    return () => clearInterval(interval);
  }, [loadConflicts, pollIntervalMs]);

  // WebSocket: listen for new conflicts
  useEffect(() => {
    const unsub = wsClient.on("pipeline.conflict_detected", (raw: unknown) => {
      const payload = raw as SourceConflict;
      if (!payload?.id) return;

      setConflicts((prev) => {
        const exists = prev.some((c) => c.id === payload.id);
        if (exists) {
          return prev.map((c) => (c.id === payload.id ? payload : c));
        }
        return [...prev, payload];
      });

      setSelectedSources((prev) => {
        if (!(payload.id in prev)) {
          return { ...prev, [payload.id]: "llm" };
        }
        return prev;
      });
    });

    const unsubResolved = wsClient.on("pipeline.conflict_resolved", (raw: unknown) => {
      const payload = raw as { conflictId: string };
      if (!payload?.conflictId) return;
      setConflicts((prev) => prev.filter((c) => c.id !== payload.conflictId));
    });

    return () => {
      unsub();
      unsubResolved();
    };
  }, []);

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
        await postResolveConflict(conflictId, selectedSource);

        // Add to history
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

  // -- Derived state -------------------------------------------------------

  const pendingCount = conflicts.length;
  const highCount = useMemo(
    () => conflicts.filter((c) => c.severity === "high").length,
    [conflicts]
  );

  // -- Render --------------------------------------------------------------

  return (
    <div className={cn("space-y-4", className)}>
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <h2 className="text-lg font-semibold">Conflicts Resolution</h2>
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
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={loadConflicts}
          disabled={loading}
        >
          <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
        </Button>
      </div>

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
            Dismiss
          </Button>
        </div>
      )}

      {/* Loading State */}
      {loading && conflicts.length === 0 && (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center justify-center py-12 text-center">
            <Loader2 className="h-8 w-8 text-muted-foreground animate-spin mb-3" />
            <p className="text-sm text-muted-foreground">Loading conflicts...</p>
          </CardContent>
        </Card>
      )}

      {/* Empty State */}
      {!loading && conflicts.length === 0 && history.length === 0 && (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center justify-center py-12 text-center">
            <CheckCircle className="h-8 w-8 text-muted-foreground mb-3" />
            <p className="text-sm text-muted-foreground font-medium">
              No pending conflicts
            </p>
            <p className="text-xs text-muted-foreground mt-1 max-w-md">
              When the pipeline detects conflicting values between LLM, API, and ASR
              sources, they will appear here for resolution.
            </p>
          </CardContent>
        </Card>
      )}

      {/* Conflict List */}
      {conflicts.length > 0 && (
        <ScrollArea className="h-[600px] pr-1">
          <div className="space-y-3">
            {conflicts.map((conflict) => (
              <ConflictCard
                key={conflict.id}
                conflict={conflict}
                selectedSource={selectedSources[conflict.id] ?? "llm"}
                onSelectSource={(source) => handleSelectSource(conflict.id, source)}
                onResolve={handleResolve}
                isResolving={resolving.has(conflict.id)}
              />
            ))}
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
                <CardTitle className="text-sm font-medium">
                  Resolution History
                </CardTitle>
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
          </CardHeader>

          {historyExpanded && (
            <CardContent className="pt-0">
              <ScrollArea className="h-[280px]">
                <div className="space-y-2">
                  {history.map((entry) => {
                    const Icon = SOURCE_ICONS[entry.selectedSource];
                    return (
                      <div
                        key={entry.id}
                        className="flex items-center justify-between rounded-md border p-3 text-sm"
                      >
                        <div className="flex items-center gap-2 min-w-0">
                          <CheckCircle className="h-4 w-4 shrink-0 text-green-600" />
                          <div className="min-w-0">
                            <div className="font-medium truncate">
                              {entry.entityName}
                            </div>
                            <div className="text-xs text-muted-foreground">
                              <span className="font-mono">{entry.fieldName}</span>
                              {" "}&middot;{" "}
                              {formatDate(entry.resolvedAt)}
                            </div>
                          </div>
                        </div>
                        <div className="flex items-center gap-2 shrink-0 ml-2">
                          <Icon className="h-3.5 w-3.5 text-muted-foreground" />
                          <Badge variant="outline" className="text-xs">
                            {SOURCE_LABELS[entry.selectedSource]}
                          </Badge>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </ScrollArea>
            </CardContent>
          )}
        </Card>
      )}
    </div>
  );
}