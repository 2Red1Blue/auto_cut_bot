"use client";

import { useRef, useEffect, useState, useCallback, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { useClient } from "@/providers/client-provider";
import { useChatStream } from "@/hooks/use-chat-stream";
import { useUIStore } from "@/lib/stores/ui-store";
import { useSessionStore } from "@/lib/stores/session-store";
import { AuthScreen } from "@/components/common/auth-screen";
import { ConnectionBadge } from "@/components/common/connection-badge";
import { ThemeToggle } from "@/components/common/theme-toggle";
import { LanguageSwitcher } from "@/components/common/language-switcher";
import { Sidebar } from "@/components/sidebar/sidebar";
import { MessageBubble } from "./message-bubble";
import { MessageInput } from "./message-input";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  PanelLeftClose,
  PanelLeft,
  LogOut,
  AlertCircle,
  RefreshCw,
  WifiOff,
  Cpu,
  FolderOpen,
  MessageSquare,
  ArrowDown,
  Loader2,
} from "lucide-react";

// ── Helpers ──

function toModelBadgeLabel(modelName: string | null): string | null {
  if (!modelName) return null;
  const trimmed = modelName.trim();
  if (!trimmed) return null;
  const leaf = trimmed.split("/").pop() ?? trimmed;
  return leaf || trimmed;
}

// ── Loading Skeleton ──

function LoadingSkeleton() {
  return (
    <div className="flex-1 p-4 space-y-4 overflow-hidden">
      {Array.from({ length: 4 }).map((_, i) => (
        <div
          key={i}
          className={cn("flex gap-3", i % 2 === 0 ? "justify-end" : "justify-start")}
        >
          <div
            className={cn(
              "animate-pulse rounded-lg bg-muted",
              i % 2 === 0 ? "w-2/3 h-12" : "w-3/4 h-20",
            )}
          />
        </div>
      ))}
    </div>
  );
}

// ── Empty State ──

function EmptyState({ isReady }: { isReady: boolean }) {
  const { t } = useTranslation();
  return (
    <div className="flex-1 flex items-center justify-center p-8">
      <div className="text-center space-y-4 max-w-md">
        <div className="mx-auto h-12 w-12 rounded-full bg-muted flex items-center justify-center">
          <MessageSquare className="h-6 w-6 text-muted-foreground" />
        </div>
        <h2 className="text-xl font-semibold text-foreground">
          {t("thread.empty.title", "Welcome")}
        </h2>
        <p className="text-sm text-muted-foreground">
          {t(
            "thread.empty.description",
            "Start a conversation by sending a message below. The AI agent will respond to your queries.",
          )}
        </p>
        {!isReady && (
          <div className="flex items-center justify-center gap-2 text-muted-foreground text-sm">
            <Loader2 className="h-4 w-4 animate-spin" />
            {t("thread.empty.connecting", "Connecting to agent...")}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Error Banner ──

function ErrorBanner({
  error,
  onRetry,
  onDismiss,
}: {
  error: string;
  onRetry: () => void;
  onDismiss: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="mx-4 mt-4 flex items-start gap-3 rounded-lg border border-destructive/50 bg-destructive/10 p-4 animate-in fade-in slide-in-from-top-2">
      <AlertCircle className="h-5 w-5 text-destructive shrink-0 mt-0.5" />
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-destructive-foreground">
          {t("error.title", "Something went wrong")}
        </p>
        <p className="text-sm text-muted-foreground mt-1">{error}</p>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <Button variant="outline" size="sm" onClick={onRetry}>
          <RefreshCw className="h-3 w-3 mr-1" />
          {t("common.retry", "Retry")}
        </Button>
        <Button variant="ghost" size="sm" onClick={onDismiss}>
          {t("common.dismiss", "Dismiss")}
        </Button>
      </div>
    </div>
  );
}

// ── Connection Banner ──

function ConnectionBanner() {
  const { t } = useTranslation();
  return (
    <div className="flex items-center justify-center gap-2 bg-yellow-500/10 border-b border-yellow-500/30 px-4 py-2 text-sm text-yellow-700 dark:text-yellow-400 shrink-0">
      <WifiOff className="h-4 w-4" />
      <span>{t("connection.disconnected", "Connection lost. Reconnecting...")}</span>
    </div>
  );
}

// ── Streaming Indicator ──

function StreamingIndicator() {
  const { t } = useTranslation();
  return (
    <div className="flex items-center gap-2 text-muted-foreground text-sm pl-2 py-1">
      <div className="flex gap-1">
        <span className="animate-bounce h-1.5 w-1.5 rounded-full bg-primary [animation-delay:0ms]" />
        <span className="animate-bounce h-1.5 w-1.5 rounded-full bg-primary [animation-delay:150ms]" />
        <span className="animate-bounce h-1.5 w-1.5 rounded-full bg-primary [animation-delay:300ms]" />
      </div>
      <span>{t("thread.streaming", "Generating response...")}</span>
    </div>
  );
}

// ── Scroll-to-Bottom Button ──

function ScrollToBottomButton({ onClick }: { onClick: () => void }) {
  const { t } = useTranslation();
  return (
    <Button
      variant="secondary"
      size="icon"
      className="absolute bottom-32 right-6 rounded-full shadow-lg z-10 animate-in fade-in zoom-in"
      onClick={onClick}
      title={t("thread.scrollToBottom", "Scroll to bottom")}
    >
      <ArrowDown className="h-4 w-4" />
    </Button>
  );
}

// ── No Session Selected ──

function NoSessionSelected() {
  const { t } = useTranslation();
  return (
    <div className="flex-1 flex items-center justify-center text-muted-foreground">
      <div className="text-center space-y-4">
        <div className="mx-auto h-16 w-16 rounded-full bg-muted flex items-center justify-center">
          <MessageSquare className="h-8 w-8 text-muted-foreground" />
        </div>
        <h2 className="text-2xl font-semibold text-foreground">
          {t("app.name", "Auto Cut Bot")}
        </h2>
        <p className="max-w-xs text-sm">
          {t(
            "home.selectSession",
            "Select a session or create a new one to start chatting.",
          )}
        </p>
      </div>
    </div>
  );
}

// ── Main Component ──

export function ChatContainer() {
  const { t } = useTranslation();
  const {
    status,
    connectionStatus,
    error: clientError,
    modelName,
    bootstrap,
    logout,
  } = useClient();
  const currentSessionId = useSessionStore((s) => s.currentSessionId);
  const sessions = useSessionStore((s) => s.sessions);
  const sidebarOpen = useUIStore((s) => s.sidebarOpen);
  const toggleSidebar = useUIStore((s) => s.toggleSidebar);

  // Chat stream for the current session
  const { messages, sendMessage, isStreaming, isReady } = useChatStream(currentSessionId);

  const bottomRef = useRef<HTMLDivElement>(null);
  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const [sendError, setSendError] = useState<string | null>(null);
  const [userScrolledUp, setUserScrolledUp] = useState(false);
  const prevMessageCountRef = useRef(messages.length);

  // Current session info
  const currentSession = useMemo(
    () => sessions.find((s) => s.id === currentSessionId) ?? null,
    [sessions, currentSessionId],
  );

  // Connection status
  const isConnected = connectionStatus === "open";

  // Model badge label
  const modelLabel = useMemo(
    () => toModelBadgeLabel(modelName) ?? bootstrap?.model_name ?? null,
    [modelName, bootstrap],
  );

  // Workspace indicator
  const workspaceLabel = useMemo(() => {
    const scope = (bootstrap as Record<string, unknown> | null)?.workspace_scope as
      | { project_path?: string }
      | undefined;
    if (!scope?.project_path) return null;
    const parts = scope.project_path.split("/");
    return parts[parts.length - 1] || null;
  }, [bootstrap]);

  // Auto-scroll to bottom when messages change
  useEffect(() => {
    const prevCount = prevMessageCountRef.current;
    prevMessageCountRef.current = messages.length;

    // Auto-scroll if new messages were added and user hasn't scrolled up
    if (messages.length > prevCount && !userScrolledUp) {
      bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, userScrolledUp]);

  // Track user scroll position
  useEffect(() => {
    const container = messagesContainerRef.current;
    if (!container) return;

    const handleScroll = () => {
      const { scrollTop, scrollHeight, clientHeight } = container;
      // Consider "at bottom" if within 120px of the bottom
      const isAtBottom = scrollHeight - scrollTop - clientHeight < 120;
      setUserScrolledUp(!isAtBottom);
    };

    container.addEventListener("scroll", handleScroll, { passive: true });
    return () => container.removeEventListener("scroll", handleScroll);
  }, []);

  // Scroll to bottom manually
  const scrollToBottom = useCallback(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    setUserScrolledUp(false);
  }, []);

  // Handle send message
  const handleSend = useCallback(
    async (content: string) => {
      setSendError(null);
      setUserScrolledUp(false);
      try {
        await sendMessage(content);
      } catch (err) {
        setSendError(
          err instanceof Error
            ? err.message
            : t("error.sendFailed", "Failed to send message"),
        );
      }
    },
    [sendMessage, t],
  );

  // ── Render States ──

  // Auth / loading screen
  if (status === "auth" || status === "loading") {
    return <AuthScreen />;
  }

  // Error screen
  if (status === "error") {
    return <AuthScreen />;
  }

  // Connecting screen
  if (status === "connecting") {
    return (
      <div className="flex h-screen items-center justify-center bg-background">
        <div className="text-center space-y-4">
          <Loader2 className="h-8 w-8 animate-spin text-primary mx-auto" />
          <p className="text-muted-foreground">
            {t("connection.connecting", "Connecting to agent...")}
          </p>
          {clientError && (
            <p className="text-sm text-destructive max-w-xs">{clientError}</p>
          )}
        </div>
      </div>
    );
  }

  // ── Main Layout ──

  return (
    <div className="flex h-screen overflow-hidden">
      {/* Sidebar */}
      {sidebarOpen && <Sidebar />}

      {/* Main Chat Area */}
      <main className="flex-1 flex flex-col min-w-0 bg-background">
        {/* ── Top Bar ── */}
        <header className="flex items-center gap-2 px-3 sm:px-4 py-2 border-b bg-background shrink-0">
          {/* Sidebar toggle */}
          <Button
            variant="ghost"
            size="icon"
            onClick={toggleSidebar}
            title={sidebarOpen ? t("sidebar.close") : t("sidebar.open")}
          >
            {sidebarOpen ? (
              <PanelLeftClose className="h-4 w-4" />
            ) : (
              <PanelLeft className="h-4 w-4" />
            )}
          </Button>

          {/* Session title */}
          <h1 className="flex-1 text-sm font-medium truncate min-w-0">
            {currentSession?.title ?? t("thread.title", "New Chat")}
          </h1>

          {/* Workspace indicator */}
          {workspaceLabel && (
            <div className="hidden sm:flex items-center gap-1.5 text-xs text-muted-foreground bg-muted px-2 py-1 rounded-md shrink-0">
              <FolderOpen className="h-3 w-3" />
              <span className="truncate max-w-[120px]">{workspaceLabel}</span>
            </div>
          )}

          {/* Model badge */}
          {modelLabel && (
            <div className="hidden sm:flex items-center gap-1.5 text-xs text-muted-foreground bg-muted px-2 py-1 rounded-md shrink-0">
              <Cpu className="h-3 w-3" />
              <span className="truncate max-w-[140px]">{modelLabel}</span>
            </div>
          )}

          {/* Connection status */}
          <ConnectionBadge connected={isConnected} />

          {/* Language switcher */}
          <LanguageSwitcher />

          {/* Theme toggle */}
          <ThemeToggle />

          {/* Logout */}
          <Button
            variant="ghost"
            size="icon"
            onClick={logout}
            title={t("common.logout", "Disconnect")}
          >
            <LogOut className="h-4 w-4" />
          </Button>
        </header>

        {/* Connection lost banner */}
        {!isConnected && <ConnectionBanner />}

        {/* ── Chat Content ── */}
        {currentSessionId ? (
          <div className="flex-1 flex flex-col min-h-0 relative">
            {/* Messages area */}
            <div
              ref={messagesContainerRef}
              className="flex-1 overflow-y-auto overscroll-contain"
            >
              {/* Loading skeleton — initial load */}
              {!isReady && messages.length === 0 && <LoadingSkeleton />}

              {/* Empty state — ready but no messages yet */}
              {isReady && messages.length === 0 && !sendError && (
                <EmptyState isReady={isReady} />
              )}

              {/* Error banner */}
              {sendError && (
                <ErrorBanner
                  error={sendError}
                  onRetry={() => setSendError(null)}
                  onDismiss={() => setSendError(null)}
                />
              )}

              {/* Message list */}
              {messages.length > 0 && (
                <div className="p-4 space-y-4">
                  {messages.map((msg) => (
                    <MessageBubble key={msg.id} message={msg} />
                  ))}

                  {/* Streaming indicator */}
                  {isStreaming && <StreamingIndicator />}

                  <div ref={bottomRef} />
                </div>
              )}
            </div>

            {/* Scroll to bottom button */}
            {userScrolledUp && messages.length > 0 && (
              <ScrollToBottomButton onClick={scrollToBottom} />
            )}

            {/* Message input */}
            <MessageInput
              sessionId={currentSessionId}
              onSend={handleSend}
              disabled={!isReady || isStreaming}
            />
          </div>
        ) : (
          <NoSessionSelected />
        )}
      </main>
    </div>
  );
}