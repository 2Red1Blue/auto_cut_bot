"use client";

import { useState, useRef, useCallback, type KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";
import { ArrowUp, Square, Paperclip, Mic, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { AttachmentTileList, type Attachment } from "./attachment-tile";
import type { CliAppInfo, McpPresetInfo } from "@/lib/types";

interface ThreadComposerProps {
  sessionId: string;
  onSend: (content: string, attachments?: Attachment[]) => Promise<void>;
  onStop?: () => void;
  disabled?: boolean;
  isStreaming?: boolean;
  cliApps?: CliAppInfo[];
  mcpPresets?: McpPresetInfo[];
}

export function ThreadComposer({
  sessionId,
  onSend,
  onStop,
  disabled,
  isStreaming,
  cliApps,
  mcpPresets,
}: ThreadComposerProps) {
  const { t } = useTranslation();
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [recording, setRecording] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleSend = useCallback(async () => {
    const content = input.trim();
    if (!content || sending || disabled) return;
    setInput("");
    setSending(true);
    try {
      await onSend(content, attachments.length > 0 ? attachments : undefined);
      setAttachments([]);
    } finally {
      setSending(false);
      textareaRef.current?.focus();
    }
  }, [input, sending, disabled, attachments, onSend]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend]
  );

  const handleFileChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const isImage = file.type.startsWith("image/");
    const url = isImage ? URL.createObjectURL(file) : undefined;
    setAttachments((prev) => [
      ...prev,
      { id: `att-${Date.now()}`, name: file.name, url, type: isImage ? "image" : "file", file },
    ]);
    e.target.value = "";
  }, []);

  const handleVoiceToggle = useCallback(async () => {
    if (recording) {
      setRecording(false);
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const recorder = new MediaRecorder(stream);
      const chunks: Blob[] = [];
      recorder.ondataavailable = (e) => chunks.push(e.data);
      recorder.onstop = () => {
        const blob = new Blob(chunks, { type: recorder.mimeType });
        stream.getTracks().forEach((t) => t.stop());
        setAttachments((prev) => [
          ...prev,
          {
            id: `voice-${Date.now()}`,
            name: `recording-${Date.now()}.webm`,
            url: URL.createObjectURL(blob),
            type: "file",
          },
        ]);
      };
      recorder.start();
      setRecording(true);
      setTimeout(() => recorder.stop(), 10000); // 10s max
    } catch {
      // Permission denied
    }
  }, [recording]);

  const canSend = input.trim().length > 0 && !disabled && !sending;

  return (
    <div className="border-t bg-background shrink-0">
      <AttachmentTileList
        attachments={attachments}
        onRemove={(id) => setAttachments((prev) => prev.filter((a) => a.id !== id))}
        className="px-4 pt-2"
      />

      <div className="max-w-3xl mx-auto p-4">
        <div className="flex items-end gap-2">
          <input
            ref={fileInputRef}
            type="file"
            className="hidden"
            onChange={handleFileChange}
            accept="image/*,.pdf,.doc,.docx,.txt,.json,.csv,.md"
          />

          <Button
            variant="ghost"
            size="icon"
            onClick={() => fileInputRef.current?.click()}
            disabled={disabled}
            title={t("composer.attach", "Attach file")}
          >
            <Paperclip className="h-4 w-4" />
          </Button>

          <Button
            variant="ghost"
            size="icon"
            onClick={handleVoiceToggle}
            disabled={disabled}
            className={cn(recording && "text-red-500")}
            title={recording ? t("composer.stopRecording", "Stop recording") : t("composer.record", "Record audio")}
          >
            <Mic className="h-4 w-4" />
          </Button>

          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={t("composer.placeholder", "Type a message... (Enter to send, Shift+Enter for new line)")}
            className="flex-1 min-h-[44px] max-h-[200px] rounded-lg border border-input bg-background px-3 py-2 text-sm resize-none focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-50"
            rows={1}
            disabled={disabled || sending}
            autoFocus
          />

          {isStreaming ? (
            <Button variant="destructive" size="icon" onClick={onStop} title={t("composer.stop", "Stop")}>
              <Square className="h-4 w-4" />
            </Button>
          ) : (
            <Button onClick={handleSend} size="icon" disabled={!canSend}>
              {sending ? <Loader2 className="h-4 w-4 animate-spin" /> : <ArrowUp className="h-4 w-4" />}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}