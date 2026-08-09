"use client";

import { Sidebar } from "@/components/sidebar/sidebar";
import { ChatView } from "@/components/chat/chat-view";
import { useSessionStore } from "@/lib/stores/session-store";

export function ChatContainer() {
  const currentSessionId = useSessionStore((s) => s.currentSessionId);

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <main className="flex-1 flex flex-col min-w-0">
        {currentSessionId ? (
          <ChatView sessionId={currentSessionId} />
        ) : (
          <div className="flex-1 flex items-center justify-center text-muted-foreground">
            <div className="text-center">
              <h2 className="text-2xl font-semibold mb-2">Auto Cut Bot</h2>
              <p>Select a session or create a new one to start chatting.</p>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}