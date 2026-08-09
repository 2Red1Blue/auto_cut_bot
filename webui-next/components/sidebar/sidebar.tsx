"use client";

import { useSessionStore } from "@/lib/stores/session-store";
import { Button } from "@/components/ui/button";
import { Plus, Search } from "lucide-react";
import { ChatItem } from "./chat-item";
import { useState } from "react";

export function Sidebar() {
  const sessions = useSessionStore((s) => s.sessions);
  const currentSessionId = useSessionStore((s) => s.currentSessionId);
  const createSession = useSessionStore((s) => s.createSession);
  const setCurrentSession = useSessionStore((s) => s.setCurrentSession);
  const [search, setSearch] = useState("");

  const filtered = sessions.filter((s) =>
    s.title?.toLowerCase().includes(search.toLowerCase())
  );

  return (
    <aside className="w-64 border-r flex flex-col bg-sidebar text-sidebar-foreground h-screen">
      <div className="p-3 border-b border-sidebar-border flex items-center gap-2">
        <Button
          variant="outline"
          size="icon"
          onClick={createSession}
          title="New Session"
        >
          <Plus className="h-4 w-4" />
        </Button>
        <div className="relative flex-1">
          <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search sessions..."
            className="w-full pl-8 pr-2 py-1.5 text-sm rounded-md border border-sidebar-border bg-sidebar-accent focus:outline-none focus:ring-1 focus:ring-ring"
          />
        </div>
      </div>
      <div className="flex-1 overflow-y-auto p-2 space-y-1">
        {filtered.map((session) => (
          <ChatItem
            key={session.id}
            session={session}
            isActive={session.id === currentSessionId}
            onClick={() => setCurrentSession(session.id)}
          />
        ))}
        {filtered.length === 0 && (
          <p className="text-sm text-muted-foreground text-center py-4">
            No sessions found.
          </p>
        )}
      </div>
    </aside>
  );
}