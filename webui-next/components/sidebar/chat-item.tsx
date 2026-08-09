"use client";

import { cn } from "@/lib/utils";
import { MessageSquare } from "lucide-react";

interface Session {
  id: string;
  title?: string;
  updatedAt?: string;
}

interface ChatItemProps {
  session: Session;
  isActive: boolean;
  onClick: () => void;
}

export function ChatItem({ session, isActive, onClick }: ChatItemProps) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "w-full flex items-center gap-2 px-3 py-2 rounded-md text-sm text-left transition-colors",
        isActive
          ? "bg-sidebar-accent text-sidebar-accent-foreground"
          : "hover:bg-sidebar-accent/50 text-sidebar-foreground"
      )}
    >
      <MessageSquare className="h-4 w-4 shrink-0" />
      <span className="truncate">{session.title || "Untitled Session"}</span>
    </button>
  );
}