"use client";

import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { useRouter } from "next/navigation";
import { Command } from "cmdk";
import { Search, MessageSquare, Calendar } from "lucide-react";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { useSessions } from "@/hooks/use-sessions";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface SessionSearchDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatDate(iso?: string): string {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    const now = new Date();
    const diffMs = now.getTime() - d.getTime();
    const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

    if (diffDays === 0) {
      return d.toLocaleTimeString(undefined, {
        hour: "2-digit",
        minute: "2-digit",
      });
    }
    if (diffDays === 1) return "Yesterday";
    if (diffDays < 7) return `${diffDays}d ago`;
    return d.toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
    });
  } catch {
    return "";
  }
}

function deriveTitle(
  session: { title?: string; preview?: string },
  fallback: string,
): string {
  return session.title?.trim() || session.preview?.trim().slice(0, 80) || fallback;
}

function getPreviewText(session: { preview?: string; title?: string }): string {
  const raw = session.preview?.trim() ?? "";
  const title = session.title?.trim() ?? "";
  if (!raw) return "";
  if (raw.toLowerCase() === title.toLowerCase()) return "";
  return raw.length > 120 ? raw.slice(0, 120) + "..." : raw;
}

const SKELETON_COUNT = 5;

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function SessionSearchDialog({
  open,
  onOpenChange,
}: SessionSearchDialogProps) {
  const router = useRouter();
  const { sessions, isLoading } = useSessions();

  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Debounce input (300ms)
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      setDebouncedQuery(query);
    }, 300);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [query]);

  // Reset state when dialog opens
  useEffect(() => {
    if (open) {
      setQuery("");
      setDebouncedQuery("");
    }
  }, [open]);

  // Filter sessions using debounced query
  const results = useMemo(() => {
    const normalized = debouncedQuery.trim().toLowerCase();
    if (!normalized) return sessions;
    const terms = normalized.split(/\s+/).filter(Boolean);
    return sessions.filter((s) => {
      const haystack = [s.title, s.preview].filter(Boolean).join(" ").toLowerCase();
      return terms.every((t) => haystack.includes(t));
    });
  }, [sessions, debouncedQuery]);

  const handleSelect = useCallback(
    (id: string) => {
      onOpenChange(false);
      router.push(`/?session=${encodeURIComponent(id)}`);
    },
    [onOpenChange, router],
  );

  const showLoading = isLoading && sessions.length === 0;
  const showEmpty = !showLoading && debouncedQuery && results.length === 0;
  const showResults = !showLoading && results.length > 0;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        showCloseButton={false}
        className="max-h-[min(32rem,calc(100vh-4rem))] w-[calc(100vw-2rem)] max-w-[40rem] gap-0 overflow-hidden p-0"
      >
        <Command
          label="Search sessions"
          shouldFilter={false}
          onKeyDown={(e: React.KeyboardEvent) => {
            if (e.key === "Escape") {
              onOpenChange(false);
            }
          }}
        >
          {/* ---- Search input ---- */}
          <div className="flex items-center gap-3 border-b px-4 h-14 shrink-0">
            <Search className="h-4 w-4 shrink-0 text-muted-foreground" />
            <Command.Input
              value={query}
              onValueChange={setQuery}
              placeholder="Search sessions..."
              className="flex-1 bg-transparent text-base font-normal text-foreground outline-none placeholder:text-muted-foreground"
              autoFocus
            />
          </div>

          {/* ---- Results ---- */}
          <Command.List className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-2">
            {/* Loading skeleton */}
            {showLoading && (
              <div className="space-y-1 px-2 py-1">
                {Array.from({ length: SKELETON_COUNT }).map((_, i) => (
                  <div
                    key={i}
                    className="flex items-center gap-3 rounded-lg px-3 py-2.5"
                  >
                    <div className="h-4 w-4 shrink-0 animate-pulse rounded bg-muted" />
                    <div className="flex-1 space-y-1.5">
                      <div className="h-3.5 w-2/3 animate-pulse rounded bg-muted" />
                      <div className="h-3 w-1/2 animate-pulse rounded bg-muted" />
                    </div>
                  </div>
                ))}
              </div>
            )}

            {/* Empty state: no results for query */}
            {showEmpty && (
              <div className="py-12 text-center text-sm text-muted-foreground">
                <Search className="mx-auto mb-3 h-8 w-8 opacity-40" />
                <p>
                  No results found for &ldquo;{debouncedQuery}&rdquo;
                </p>
              </div>
            )}

            {/* Empty state: no sessions at all */}
            {!showLoading && !debouncedQuery && sessions.length === 0 && (
              <div className="py-12 text-center text-sm text-muted-foreground">
                <MessageSquare className="mx-auto mb-3 h-8 w-8 opacity-40" />
                <p>No sessions yet</p>
              </div>
            )}

            {/* Result list */}
            {showResults && (
              <Command.Group
                heading={debouncedQuery ? "Search results" : "Recent"}
                className="[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:pb-1.5 [&_[cmdk-group-heading]]:pt-1 [&_[cmdk-group-heading]]:text-xs [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:text-muted-foreground"
              >
                {results.map((session) => {
                  const title = deriveTitle(session, "Untitled");
                  const preview = getPreviewText(session);
                  const date = formatDate(
                    session.updatedAt || session.createdAt,
                  );

                  return (
                    <Command.Item
                      key={session.id}
                      value={session.id}
                      onSelect={() => handleSelect(session.id)}
                      className={cn(
                        "flex cursor-pointer items-center gap-3 rounded-lg px-3 py-2.5",
                        "aria-selected:bg-accent aria-selected:text-accent-foreground",
                        "transition-colors",
                      )}
                    >
                      <MessageSquare className="h-4 w-4 shrink-0 text-muted-foreground" />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="truncate text-sm font-medium">
                            {title}
                          </span>
                          {date && (
                            <span className="flex shrink-0 items-center gap-1 text-xs text-muted-foreground">
                              <Calendar className="h-3 w-3" />
                              {date}
                            </span>
                          )}
                        </div>
                        {preview && (
                          <p className="truncate text-xs text-muted-foreground mt-0.5">
                            {preview}
                          </p>
                        )}
                      </div>
                    </Command.Item>
                  );
                })}
              </Command.Group>
            )}
          </Command.List>

          {/* ---- Footer keyboard hints ---- */}
          <div className="flex items-center gap-4 border-t px-4 h-10 shrink-0 text-xs text-muted-foreground">
            <span className="flex items-center gap-1">
              <kbd className="inline-flex items-center rounded border px-1.5 py-0.5 font-mono text-[10px]">
                &uarr;&darr;
              </kbd>
              <span>Navigate</span>
            </span>
            <span className="flex items-center gap-1">
              <kbd className="inline-flex items-center rounded border px-1.5 py-0.5 font-mono text-[10px]">
                Enter
              </kbd>
              <span>Select</span>
            </span>
            <span className="flex items-center gap-1">
              <kbd className="inline-flex items-center rounded border px-1.5 py-0.5 font-mono text-[10px]">
                Esc
              </kbd>
              <span>Close</span>
            </span>
          </div>
        </Command>
      </DialogContent>
    </Dialog>
  );
}