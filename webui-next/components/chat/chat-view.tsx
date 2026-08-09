"use client";

import { ChatList } from "./chat-list";
import { MessageInput } from "./message-input";

interface ChatViewProps {
  sessionId: string;
}

export function ChatView({ sessionId }: ChatViewProps) {
  return (
    <div className="flex-1 flex flex-col min-h-0">
      <ChatList sessionId={sessionId} />
      <MessageInput sessionId={sessionId} />
    </div>
  );
}