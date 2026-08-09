"use client";

import { useRef, useEffect, useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { useChatStream } from "@/hooks/use-chat-stream";
import { useClient } from "@/providers/client-provider";
import { useUIStore } from "@/lib/stores/ui-store";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { ThemeToggle } from "@/components/common/theme-toggle";
import {
  PanelLeftClose,
  PanelLeft,
  AlertCircle,
  RefreshCw,
} from "lucide-react";
import { MessageBubble } from "./message-bubble";
import { MessageInput } from "./message-input";
import { AttachmentTileList, type Attachment } from "./attachment-tile";
import { MarkdownText } from "./markdown-text";
import { CodeBlock } from "./code-block";

interface ThreadShellProps {
  sessionId: string;
}

export function ThreadShell({ sessionId }: ThreadShellProps) {
  const { t } = useTranslation();
  const { connectionStatus } = useClient();
  const sidebarOpen = useUIStore((s) => s.sidebarOpen);
  const toggleSidebar = useUIStore((s) => s.toggleSidebar);
  const { messages, sendMessage, isStreaming, isReady } =
    useChatStream(sessionId);
  const bottomRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [attachments, setAttachments] = useState<Attachment[]>([]);

  // Auto-scroll to bottom when messages change
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const handleSend = useCallback(
    async (content: string) => {
      setError(null);
      try {
        await sendMessage(content);
        setAttachments([]);
      } catch (err) {
        setError(
          err instanceof Error
            ? err.message
            : t("error.sendFailed", "Failed to send message"),
        );
      }
    },
    [sendMessage, t],
  );

  const handleRetry = useCallback(() => {
    setError(null);
  }, []);

  const handleRemoveAttachment = useCallback((id: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  }, []);

  const isConnected = connectionStatus === "open";

  return (
    <div className="flex h-full flex-col">
      {/* Thread Header */}
      <header className="flex items-center gap-2 border-b px-4 py-2 bg-background shrink-0">
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

        <h1 className="flex-1 text-sm font-medium truncate">
          {t("thread.title", "Thread")}
        </h1>

        <span
          className={cn(
            "h-2 w-2 rounded-full",
            isConnected ? "bg-green-500" : "bg-yellow-500",
          )}
          title={
            isConnected
              ? t("connection.connected", "Connected")
              : t("connection.disconnected", "Disconnected")
          }
        />

        <ThemeToggle />
      </header>

      {/* Messages Area */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* Empty State */}
        {messages.length === 0 && !error && (
          <div className="flex items-center justify-center h-full">
            <div className="text-center space-y-3 max-w-md">
              <h2 className="text-xl font-semibold text-foreground">
                {t("thread.empty.title", "Welcome")}
              </h2>
              <MarkdownText
                className="text-sm"
                streaming={false}
              >
                {t(
                  "thread.empty.description",
                  "Start a conversation by sending a message below. The AI agent will respond to your queries.",
                )}
              </MarkdownText>
              {!isReady && (
                <div className="flex items-center justify-center gap-2 text-muted-foreground text-sm">
                  <div className="animate-spin h-4 w-4 border-2 border-primary border-t-transparent rounded-full" />
                  {t("thread.empty.connecting", "Connecting to agent...")}
                </div>
              )}
            </div>
          </div>
        )}

        {/* Error State */}
        {error && (
          <div className="flex items-start gap-3 rounded-lg border border-destructive/50 bg-destructive/10 p-4">
            <AlertCircle className="h-5 w-5 text-destructive shrink-0 mt-0.5" />
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-destructive-foreground">
                {t("error.title", "Something went wrong")}
              </p>
              <p className="text-sm text-muted-foreground mt-1">{error}</p>
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={handleRetry}
              className="shrink-0"
            >
              <RefreshCw className="h-3 w-3 mr-1" />
              {t("common.retry", "Retry")}
            </Button>
          </div>
        )}

        {/* Message Bubbles */}
        {messages.map((msg) => (
          <MessageBubble key={msg.id} message={msg} />
        ))}

        {/* Streaming Indicator */}
        {isStreaming && (
          <div className="flex items-center gap-2 text-muted-foreground text-sm pl-2">
            <div className="flex gap-1">
              <span className="animate-bounce h-1.5 w-1.5 rounded-full bg-primary [animation-delay:0ms]" />
              <span className="animate-bounce h-1.5 w-1.5 rounded-full bg-primary [animation-delay:150ms]" />
              <span className="animate-bounce h-1.5 w-1.5 rounded-full bg-primary [animation-delay:300ms]" />
            </div>
            <span>{t("thread.streaming", "Generating response...")}</span>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* Attachments Preview */}
      {attachments.length > 0 && (
        <div className="border-t px-4 pt-2">
          <AttachmentTileList
            attachments={attachments}
            onRemove={handleRemoveAttachment}
          />
        </div>
      )}

      {/* Message Input */}
      <MessageInput
        sessionId={sessionId}
        onSend={handleSend}
        disabled={!isReady || isStreaming}
      />
    </div>
  );
}
