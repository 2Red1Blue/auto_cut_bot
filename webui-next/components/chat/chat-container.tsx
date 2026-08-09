"use client";

import { useClient } from "@/providers/client-provider";
import { AuthScreen } from "@/components/common/auth-screen";
import { ConnectionBadge } from "@/components/common/connection-badge";
import { ThemeToggle } from "@/components/common/theme-toggle";
import { LanguageSwitcher } from "@/components/common/language-switcher";
import { Button } from "@/components/ui/button";
import { PanelLeftClose, PanelLeft, LogOut } from "lucide-react";
import { useUIStore } from "@/lib/stores/ui-store";
import { Sidebar } from "@/components/sidebar/sidebar";
import { ChatView } from "@/components/chat/chat-view";
import { useSessionStore } from "@/lib/stores/session-store";

export function ChatContainer() {
  const { status, connectionStatus, logout } = useClient();
  const currentSessionId = useSessionStore((s) => s.currentSessionId);
  const sidebarOpen = useUIStore((s) => s.sidebarOpen);
  const toggleSidebar = useUIStore((s) => s.toggleSidebar);

  // Show auth screen when not connected
  if (status === "auth" || status === "loading") {
    return <AuthScreen />;
  }

  // Show error state
  if (status === "error") {
    return <AuthScreen />;
  }

  // Show connecting state
  if (status === "connecting") {
    return (
      <div className="flex h-screen items-center justify-center">
        <div className="text-center space-y-4">
          <div className="animate-spin h-8 w-8 border-2 border-primary border-t-transparent rounded-full mx-auto" />
          <p className="text-muted-foreground">Connecting to agent...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-screen overflow-hidden">
      {sidebarOpen && <Sidebar />}
      <main className="flex-1 flex flex-col min-w-0">
        {/* Header */}
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
          <ConnectionBadge connected={connectionStatus === "open"} />
          <LanguageSwitcher />
          <ThemeToggle />
          <Button variant="ghost" size="icon" onClick={logout} title="Disconnect">
            <LogOut className="h-4 w-4" />
          </Button>
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