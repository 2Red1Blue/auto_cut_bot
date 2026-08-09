"use client";

import { useState, useCallback } from "react";
import { ChevronDown, ChevronRight, Wrench, CheckCircle2, XCircle, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { useTranslation } from "react-i18next";
import type { UIMessage, ToolProgressEvent } from "@/lib/types";
interface AgentActivityClusterProps {
  messages: UIMessage[];
  turnId?: string;
  isStreaming?: boolean;
}

function getToolStatus(events: ToolProgressEvent[]): "running" | "done" | "error" | "idle" {
  if (events.length === 0) return "idle";
  const phases = events.map((e) => e.phase);
  if (phases.some((p) => p === "error")) return "error";
  if (phases.some((p) => p === "start") && !phases.some((p) => p === "end")) return "running";
  if (phases.some((p) => p === "end")) return "done";
  return "idle";
}
function StatusIcon({ status }: { status: "running" | "done" | "error" | "idle" }) {
  if (status === "running") return <Loader2 className="h-4 w-4 shrink-0 animate-spin text-blue-500" />;
  if (status === "done") return <CheckCircle2 className="h-4 w-4 shrink-0 text-green-500" />;
  return status === "error" ? <XCircle className="h-4 w-4 shrink-0 text-red-500" /> : null;
}

export function AgentActivityCluster({ messages, turnId, isStreaming }: AgentActivityClusterProps) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const toggle = useCallback((key: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
  }, []);

  let traceMessages = messages.filter((m) => m.kind === "trace");
  if (turnId) traceMessages = traceMessages.filter((m) => m.turnId === turnId);

  const groups = new Map<string, UIMessage[]>();
  for (const msg of traceMessages) {
    const key = msg.turnId || msg.id;
    const list = groups.get(key) || [];
    list.push(msg);
    groups.set(key, list);
  }

  if (groups.size === 0) return null;

  return (
    <div className="space-y-2">
      {Array.from(groups.entries()).map(([key, msgs]) => {
        const isOpen = expanded.has(key);
        const allEvents = msgs.flatMap((m) => m.toolEvents || []);
        const toolNames = [...new Set(allEvents.map((e) => e.name).filter(Boolean) as string[])];
        const status = allEvents.length > 0 ? getToolStatus(allEvents) : isStreaming ? "running" : "idle";
        const label = toolNames.length > 0 ? toolNames.join(", ") : t("activity.agent_label", "Activity");

        return (
          <div key={key} className="rounded-lg border border-border bg-card">
            <button
              type="button"
              onClick={() => toggle(key)}
              className={cn(
                "flex w-full items-center gap-2 px-3 py-2 text-sm text-muted-foreground",
                "hover:bg-muted/50 transition-colors rounded-lg",
              )}
            >
              {isOpen ? <ChevronDown className="h-4 w-4 shrink-0" /> : <ChevronRight className="h-4 w-4 shrink-0" />}
              <Wrench className="h-4 w-4 shrink-0" />
              <span className="flex-1 truncate text-left">{label}</span>
              <StatusIcon status={status} />
            </button>
            {isOpen && (
              <div className="border-t border-border px-3 py-2 space-y-1">
                {allEvents.map((event, i) => (
                  <div key={i} className="flex items-center gap-2 text-xs text-muted-foreground">
                    <StatusIcon status={event.phase === "error" ? "error" : event.phase === "end" ? "done" : "running"} />
                    <span className="font-mono">{event.name}</span>
                    <span className="text-muted-foreground/60">{event.phase}</span>
                  </div>
                ))}
                {msgs.flatMap((m) => m.traces || []).map((trace, i) => (
                  <div key={`trace-${i}`} className="text-xs text-muted-foreground font-mono whitespace-pre-wrap">
                    {trace}
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}