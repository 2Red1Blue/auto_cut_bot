"use client";

import { cn } from "@/lib/utils";
import { MessageSquare, Trash2 } from "lucide-react";

interface Session {
  id: string;
  title?: string;
  updatedAt?: string;
}

interface ChatItemProps {
  session: Session;
  isActive: boolean;
  onClick: () => void;
  onDelete: () => void;
}

export function ChatItem({ session, isActive, onClick, onDelete }: ChatItemProps) {
  return (
    <div
      className={cn(
        "group w-full flex items-center gap-2 px-3 py-2 rounded-md text-sm text-left transition-colors cursor-pointer",
        isActive
          ? "bg-sidebar-accent text-sidebar-accent-foreground"
          : "hover:bg-sidebar-accent/50 text-sidebar-foreground"
      )}
    >
      <button onClick={onClick} className="flex items-center gap-2 flex-1 min-w-0">
        <MessageSquare className="h-4 w-4 shrink-0" />
        <span className="truncate">{session.title || "Untitled Session"}</span>
      </button>
      <button
        onClick={(e) => {
          e.stopPropagation();
          onDelete();
        }}
        className="opacity-0 group-hover:opacity-100 transition-opacity shrink-0 p-1 hover:bg-destructive/10 rounded"
        title="Delete session"
      >
        <Trash2 className="h-3 w-3 text-muted-foreground hover:text-destructive" />
      </button>
    </div>
  );
}