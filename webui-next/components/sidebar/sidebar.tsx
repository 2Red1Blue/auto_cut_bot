"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState, useCallback } from "react";
import { useSessions } from "@/hooks/use-sessions";
import { useSessionStore } from "@/lib/stores/session-store";
import { Button } from "@/components/ui/button";
import { Plus, Search, MessageSquare, Film, Workflow } from "lucide-react";
import { ChatItem } from "./chat-item";
import { DeleteConfirm } from "@/components/common/delete-confirm";
import { SessionSearchDialog } from "./session-search-dialog";
import { cn } from "@/lib/utils";

export function Sidebar() {
  const {
    sessions,
    currentSessionId,
    setCurrentSession,
    createSession,
    deleteSession,
  } = useSessions();
  const renameSession = useSessionStore((s) => s.renameSession);
  const pathname = usePathname();
  const router = useRouter();

  // Search dialog state
  const [searchOpen, setSearchOpen] = useState(false);

  // Delete confirmation state
  const [deleteTarget, setDeleteTarget] = useState<{
    id: string;
    title: string;
  } | null>(null);

  const handleCreate = useCallback(async () => {
    const session = await createSession();
    if (session?.id) {
      router.push(`/${session.id}`);
    }
  }, [createSession, router]);

  const handleDeleteRequest = useCallback((id: string, title: string) => {
    setDeleteTarget({ id, title });
  }, []);

  const handleDeleteConfirm = useCallback(() => {
    if (deleteTarget) {
      deleteSession(deleteTarget.id);
      setDeleteTarget(null);
    }
  }, [deleteTarget, deleteSession]);

  const handleRename = useCallback(
    (id: string, title: string) => {
      renameSession(id, title);
    },
    [renameSession],
  );

  const navItems = [
    { href: "/", label: "Chat", icon: MessageSquare },
    { href: "/media", label: "Media", icon: Film },
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

      {/* Chat Sessions Header */}
      <div className="p-3 border-b border-sidebar-border flex items-center gap-2">
        <Button
          variant="outline"
          size="icon"
          onClick={handleCreate}
          title="New Session"
        >
          <Plus className="h-4 w-4" />
        </Button>
        <Button
          variant="outline"
          size="default"
          onClick={() => setSearchOpen(true)}
          className="flex-1 justify-start gap-2 h-9 px-3 text-sm text-muted-foreground font-normal"
        >
          <Search className="h-4 w-4 shrink-0" />
          <span className="truncate">Search sessions...</span>
        </Button>
      </div>

      {/* Session List */}
      <div className="flex-1 overflow-y-auto p-2 space-y-1">
        {sessions.map((session) => (
          <ChatItem
            key={session.id}
            session={session}
            isActive={session.id === currentSessionId}
            onClick={() => {
              setCurrentSession(session.id);
              router.push(`/${session.id}`);
            }}
            onDelete={() =>
              handleDeleteRequest(session.id, session.title || "Untitled")
            }
            onRename={handleRename}
          />
        ))}
        {sessions.length === 0 && (
          <p className="text-sm text-muted-foreground text-center py-4">
            No sessions found.
          </p>
        )}
      </div>

      {/* Session Search Dialog */}
      <SessionSearchDialog
        open={searchOpen}
        onOpenChange={setSearchOpen}
      />

      {/* Delete Confirmation Dialog */}
      <DeleteConfirm
        open={deleteTarget !== null}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        onConfirm={handleDeleteConfirm}
        title="Delete session?"
        description="This action cannot be undone."
        itemName={deleteTarget?.title}
      />
    </aside>
  );
}