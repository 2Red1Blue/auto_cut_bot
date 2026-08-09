"use client";

import { useSessionStore } from "@/lib/stores/session-store";
import { Sidebar } from "@/components/sidebar/sidebar";
import { ChatView } from "@/components/chat/chat-view";
import { useWebSocket } from "@/hooks/use-websocket";
import { ConnectionBadge } from "@/components/common/connection-badge";
import { ThemeToggle } from "@/components/common/theme-toggle";
import { LanguageSwitcher } from "@/components/common/language-switcher";
import { Button } from "@/components/ui/button";
import { PanelLeftClose, PanelLeft } from "lucide-react";
import { useUIStore } from "@/lib/stores/ui-store";

export function ChatContainer() {
  const currentSessionId = useSessionStore((s) => s.currentSessionId);
  const sidebarOpen = useUIStore((s) => s.sidebarOpen);
  const toggleSidebar = useUIStore((s) => s.toggleSidebar);
  const { connected } = useWebSocket(currentSessionId);

  return (
    <div className="flex h-screen overflow-hidden">
      {sidebarOpen && <Sidebar />}
      <main className="flex-1 flex flex-col min-w-0">
        {/* Header bar */}
        <header className="flex items-center gap-2 px-4 py-2 border-b bg-background">
          <Button
            variant="ghost"
            size="icon"
            onClick={toggleSidebar}
            title={sidebarOpen ? "Close sidebar" : "Open sidebar"}
          >
            {sidebarOpen ? (
              <PanelLeftClose className="h-4 w-4" />
            ) : (
              <PanelLeft className="h-4 w-4" />
            )}
          </Button>
          <div className="flex-1" />
          <ConnectionBadge connected={connected} />
          <LanguageSwitcher />
          <ThemeToggle />
        </header>

        {/* Chat area */}
        {currentSessionId ? (
          <ChatView sessionId={currentSessionId} />
        ) : (
          <div className="flex-1 flex items-center justify-center text-muted-foreground">
            <div className="text-center space-y-3">
              <h2 className="text-2xl font-semibold">Auto Cut Bot</h2>
              <p>Select a session or create a new one to start chatting.</p>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}