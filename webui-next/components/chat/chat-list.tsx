"use client";

import { useMessageStore } from "@/lib/stores/message-store";
import { MessageBubble } from "./message-bubble";
import { useEffect, useRef } from "react";

interface ChatListProps {
  sessionId: string;
}

export function ChatList({ sessionId }: ChatListProps) {
  const messages = useMessageStore((s) => s.messagesBySession[sessionId] ?? []);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  if (messages.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center text-muted-foreground">
        <p>Send a message to start the conversation.</p>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto p-4 space-y-4">
      {messages.map((msg) => (
        <MessageBubble key={msg.id} message={msg} />
      ))}
      <div ref={bottomRef} />
    </div>
  );
}