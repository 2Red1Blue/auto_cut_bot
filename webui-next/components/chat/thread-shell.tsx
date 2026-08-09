"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useChatStream } from "@/hooks/use-chat-stream";
import { useClient } from "@/providers/client-provider";
import { useSessionStore } from "@/lib/stores/session-store";
import { useUIStore } from "@/lib/stores/ui-store";
import { MessageBubble } from "./message-bubble";
import { MessageInput } from "./message-input";
import { MarkdownText } from "./markdown-text";
import { CodeBlock } from "./code-block";
import { AttachmentTileList, type Attachment } from "./attachment-tile";
import { VoiceRecorder } from "./voice-recorder";
import { Button } from "@/components/ui/button";
import { ConnectionBadge } from "@/components/common/connection-badge";
import { ThemeToggle } from "@/components/common/theme-toggle";
import { LanguageSwitcher } from "@/components/common/language-switcher";
import { DeleteConfirm } from "@/components/common/delete-confirm";
import { RenameChatDialog } from "@/components/common/rename-chat-dialog";
import { SessionSearchDialog } from "@/components/sidebar/session-search-dialog";
import { SettingsDialog } from "@/components/settings/settings-dialog";
import {
  PanelLeftClose,
  PanelLeft,
  LogOut,
  Search,
  Settings,
  Trash2,
  Pencil,
  Paperclip,
} from "lucide-react";
import { cn } from "@/lib/utils";

interface ThreadShellProps {
  sessionId: string;
}

export function ThreadShell({ sessionId }: ThreadShellProps) {
  const { t } = useTranslation();
  const { connectionStatus, logout } = useClient();
  const { messages, sendMessage, isStreaming, isReady } = useChatStream(sessionId);
  const sessions = useSessionStore((s) => s.sessions);
  const currentSession = sessions.find((s) => s.id === sessionId);
  const sidebarOpen = useUIStore((s) => s.sidebarOpen);
  const toggleSidebar = useUIStore((s) => s.toggleSidebar);
  const setSettingsOpen = useUIStore((s) => s.setSettingsOpen);

  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [showRenameDialog, setShowRenameDialog] = useState(false);
  const [showSearchDialog, setShowSearchDialog] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  const title = currentSession?.title || t("chat.untitled", "Untitled Session");

  // Auto-scroll to bottom
  useMemo(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const handleSend = useCallback(
    async (content: string) => {
      // Attach media if any
      const fullContent = attachments.length > 0
        ? content + "\n\n" + attachments.map((a) => `[${a.name}](${a.url || ""})`).join("\n")
        : content;
      await sendMessage(fullContent);
      setAttachments([]);
    },
    [sendMessage, attachments]
  );

  const handleFileAdd = useCallback((file: File) => {
    const isImage = file.type.startsWith("image/");
    const url = URL.createObjectURL(file);
    setAttachments((prev) => [
      ...prev,
      {
        id: `att-${Date.now()}`,
        name: file.name,
        url: isImage ? url : undefined,
        type: isImage ? "image" : "file",
        file,
      },
    ]);
  }, []);

  const handleAttachmentRemove = useCallback((id: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  }, []);

  const handleVoiceRecording = useCallback((blob: Blob) => {
    const url = URL.createObjectURL(blob);
    setAttachments((prev) => [
      ...prev,
      {
        id: `voice-${Date.now()}`,
        name: `recording-${Date.now()}.webm`,
        url,
        type: "file",
      },
    ]);
  }, []);

  return (
    <div className="flex h-full flex-col">
      {/* Thread Header */}
      <header className="flex items-center gap-2 px-4 py-2 border-b bg-background shrink-0">
        <Button
          variant="ghost"
          size="icon"
          onClick={toggleSidebar}
          title={sidebarOpen ? t("chat.closeSidebar", "Close sidebar") : t("chat.openSidebar", "Open sidebar")}
        >
          {sidebarOpen ? <PanelLeftClose className="h-4 w-4" /> : <PanelLeft className="h-4 w-4" />}
        </Button>

        <h1 className="flex-1 text-sm font-medium truncate min-w-0">{title}</h1>

        <Button variant="ghost" size="icon" onClick={() => setShowRenameDialog(true)} title={t("chat.rename", "Rename")}>
          <Pencil className="h-4 w-4" />
        </Button>

        <Button variant="ghost" size="icon" onClick={() => setShowSearchDialog(true)} title={t("chat.search", "Search")}>
          <Search className="h-4 w-4" />
        </Button>

        <Button variant="ghost" size="icon" onClick={() => setShowDeleteConfirm(true)} title={t("chat.delete", "Delete")}>
          <Trash2 className="h-4 w-4" />
        </Button>

        <Button variant="ghost" size="icon" onClick={() => setSettingsOpen(true)} title={t("settings.title", "Settings")}>
          <Settings className="h-4 w-4" />
        </Button>

        <ConnectionBadge connected={connectionStatus === "open"} />
        <LanguageSwitcher />
        <ThemeToggle />
        <Button variant="ghost" size="icon" onClick={logout} title={t("chat.logout", "Disconnect")}>
          <LogOut className="h-4 w-4" />
        </Button>
      </header>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto">
        {messages.length === 0 ? (
          <div className="flex h-full items-center justify-center">
            <div className="text-center space-y-3 max-w-md px-8">
              <h2 className="text-xl font-semibold">
                {t("chat.welcome", "Start a Conversation")}
              </h2>
              <p className="text-sm text-muted-foreground">
                {isReady
                  ? t("chat.welcomeReady", "Send a message to start chatting with the AI agent.")
                  : t("chat.welcomeConnecting", "Connecting to the agent...")}
              </p>
            </div>
          </div>
        ) : (
          <div className="max-w-3xl mx-auto p-4 space-y-4">
            {messages.map((msg) => (
              <MessageBubble key={msg.id} message={msg} />
            ))}
            <div ref={bottomRef} />
          </div>
        )}
      </div>

      {/* Attachments */}
      <AttachmentTileList
        attachments={attachments}
        onRemove={handleAttachmentRemove}
        className="px-4"
      />

      {/* Composer */}
      <div className="border-t bg-background shrink-0">
        <div className="max-w-3xl mx-auto p-4">
          <div className="flex items-end gap-2">
            <label className="cursor-pointer p-2 hover:bg-muted rounded-lg transition-colors" title={t("chat.attach", "Attach file")}>
              <Paperclip className="h-4 w-4 text-muted-foreground" />
              <input
                type="file"
                className="hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) handleFileAdd(file);
                  e.target.value = "";
                }}
                accept="image/*,.pdf,.doc,.docx,.txt,.json,.csv,.md"
              />
            </label>
            <VoiceRecorder onRecordingComplete={handleVoiceRecording} />
            <MessageInput
              sessionId={sessionId}
              onSend={handleSend}
              disabled={!isReady || isStreaming}
            />
          </div>
        </div>
      </div>

      {/* Dialogs */}
      <DeleteConfirm
        open={showDeleteConfirm}
        onOpenChange={setShowDeleteConfirm}
        onConfirm={() => {
          useSessionStore.getState().deleteSession(sessionId);
        }}
        itemName={title}
      />

      <RenameChatDialog
        open={showRenameDialog}
        onOpenChange={setShowRenameDialog}
        currentTitle={title}
        onRename={(newTitle) => {
          // TODO: call API to rename session
          console.log("Rename to:", newTitle);
        }}
      />

      <SessionSearchDialog
        open={showSearchDialog}
        onOpenChange={setShowSearchDialog}
        onSelect={(id) => {
          useSessionStore.getState().setCurrentSession(id);
        }}
      />

      <SettingsDialog />
    </div>
  );
}