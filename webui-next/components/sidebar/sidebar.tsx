"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useSessions } from "@/hooks/use-sessions";
import { Button } from "@/components/ui/button";
import { Plus, Search, MessageSquare, Film, Workflow } from "lucide-react";
import { ChatItem } from "./chat-item";
import { useState } from "react";
import { cn } from "@/lib/utils";

export function Sidebar() {
  const {
    sessions,
    currentSessionId,
    setCurrentSession,
    createSession,
    deleteSession,
  } = useSessions();
  const [search, setSearch] = useState("");
  const pathname = usePathname();

  const filtered = sessions.filter((s) =>
    (s.title || "Untitled").toLowerCase().includes(search.toLowerCase())
  );

  const handleCreate = async () => {
    await createSession();
  };

  const navItems = [
    { href: "/", label: "聊天", icon: MessageSquare },
    { href: "/media", label: "素材库", icon: Film },
    { href: "/pipeline", label: "Pipeline", icon: Workflow },
  ];

  return (
    <aside className="w-64 border-r flex flex-col bg-sidebar text-sidebar-foreground h-screen shrink-0">
      {/* Navigation */}
      <nav className="p-3 border-b border-sidebar-border space-y-1">
        {navItems.map((item) => {
          const Icon = item.icon;
          const isActive = pathname === item.href;
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "flex items-center gap-2 px-3 py-2 rounded-md text-sm font-medium transition-colors",
                isActive
                  ? "bg-sidebar-accent text-sidebar-accent-foreground"
                  : "text-sidebar-foreground/70 hover:bg-sidebar-accent/50 hover:text-sidebar-foreground"
              )}
            >
              <Icon className="h-4 w-4" />
              {item.label}
            </Link>
          );
        })}
      </nav>

      {/* Chat Sessions */}
      <div className="p-3 border-b border-sidebar-border flex items-center gap-2">
        <Button
          variant="outline"
          size="icon"
          onClick={handleCreate}
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
            placeholder="Search..."
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
            onDelete={() => deleteSession(session.id)}
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