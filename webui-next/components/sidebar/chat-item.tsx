"use client";

import { useState, useRef, useEffect } from "react";
import { cn } from "@/lib/utils";
import { MessageSquare, Trash2 } from "lucide-react";

interface Session {
  id: string;
  title?: string;
  createdAt?: string;
  updatedAt?: string;
}

interface ChatItemProps {
  session: Session;
  isActive: boolean;
  onClick: () => void;
  onDelete: () => void;
  onRename?: (id: string, title: string) => void;
}

function formatTime(iso?: string): string {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    const now = new Date();
    const isToday = d.toDateString() === now.toDateString();
    if (isToday) {
      return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  } catch {
    return "";
  }
}

export function ChatItem({ session, isActive, onClick, onDelete, onRename }: ChatItemProps) {
  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState(session.title || "");
  const inputRef = useRef<HTMLInputElement>(null);

  // Sync editValue when session.title changes externally
  useEffect(() => {
    if (!editing) {
      setEditValue(session.title || "");
    }
  }, [session.title, editing]);

  // Focus and select input when entering edit mode
  useEffect(() => {
    if (editing) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [editing]);

  const displayTitle = session.title || "Untitled Session";
  const timeLabel = formatTime(session.updatedAt || session.createdAt);

  const handleDoubleClick = (e: React.MouseEvent) => {
    if (!onRename) return;
    e.preventDefault();
    setEditValue(session.title || "");
    setEditing(true);
  };

  const commitRename = () => {
    setEditing(false);
    const trimmed = editValue.trim();
    if (trimmed && trimmed !== (session.title || "") && onRename) {
      onRename(session.id, trimmed);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      commitRename();
    } else if (e.key === "Escape") {
      setEditing(false);
      setEditValue(session.title || "");
    }
  };

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
        {editing ? (
          <input
            ref={inputRef}
            value={editValue}
            onChange={(e) => setEditValue(e.target.value)}
            onBlur={commitRename}
            onKeyDown={handleKeyDown}
            onClick={(e) => e.stopPropagation()}
            className="flex-1 min-w-0 bg-transparent border-b border-sidebar-accent-foreground/50 outline-none text-sm"
          />
        ) : (
          <span className="truncate" onDoubleClick={handleDoubleClick}>
            {displayTitle}
          </span>
        )}
        {timeLabel && !editing && (
          <span className="text-xs text-muted-foreground shrink-0 ml-auto">
            {timeLabel}
          </span>
        )}
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