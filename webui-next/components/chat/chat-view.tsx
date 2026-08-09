"use client";

import { useChatStream } from "@/hooks/use-chat-stream";
import { MessageBubble } from "./message-bubble";
import { MessageInput } from "./message-input";
import { useEffect, useRef } from "react";

interface ChatViewProps {
  sessionId: string;
}

export function ChatView({ sessionId }: ChatViewProps) {
  const { messages, sendMessage, isStreaming, isReady } = useChatStream(sessionId);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {messages.length === 0 && (
          <div className="flex-1 flex items-center justify-center h-full text-muted-foreground">
            <div className="text-center space-y-2">
              <h2 className="text-lg font-semibold">Start a Conversation</h2>
              <p className="text-sm">
                {isReady
                  ? "Send a message to start chatting with the AI agent."
                  : "Connecting to agent..."}
              </p>
            </div>
          </div>
        )}
        {messages.map((msg) => (
          <MessageBubble key={msg.id} message={msg} />
        ))}
        <div ref={bottomRef} />
      </div>
      <MessageInput
        sessionId={sessionId}
        onSend={sendMessage}
        disabled={!isReady || isStreaming}
      />
    </div>
  );
}