"use client";

import { useState, useMemo } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Search } from "lucide-react";
import { useSessionStore } from "@/lib/stores/session-store";

interface SessionSearchDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSelect: (sessionId: string) => void;
}

export function SessionSearchDialog({
  open,
  onOpenChange,
  onSelect,
}: SessionSearchDialogProps) {
  const sessions = useSessionStore((s) => s.sessions);
  const [query, setQuery] = useState("");

  const filtered = useMemo(() => {
    if (!query.trim()) return sessions.slice(0, 20);
    const q = query.toLowerCase();
    return sessions
      .filter((s) => (s.title || "").toLowerCase().includes(q))
      .slice(0, 20);
  }, [sessions, query]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Search Sessions</DialogTitle>
        </DialogHeader>
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Type to search..."
            className="pl-10"
            autoFocus
          />
        </div>
        <div className="max-h-60 overflow-y-auto space-y-1">
          {filtered.map((session) => (
            <button
              key={session.id}
              onClick={() => {
                onSelect(session.id);
                onOpenChange(false);
              }}
              className="w-full text-left px-3 py-2 rounded-md text-sm hover:bg-muted transition-colors"
            >
              <div className="font-medium truncate">
                {session.title || "Untitled Session"}
              </div>
              {session.updatedAt && (
                <div className="text-xs text-muted-foreground">
                  {new Date(session.updatedAt).toLocaleDateString()}
                </div>
              )}
            </button>
          ))}
          {filtered.length === 0 && (
            <p className="text-sm text-muted-foreground text-center py-4">
              No sessions found.
            </p>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}