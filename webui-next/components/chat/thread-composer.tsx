"use client";

import { useState, useRef, useCallback, useEffect, type KeyboardEvent } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { ArrowUp, Square, Paperclip, Mic, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { AttachmentTileList, type Attachment } from "./attachment-tile";
import type { CliAppInfo, McpPresetInfo } from "@/lib/types";

interface ThreadComposerProps {
  sessionId: string;
  onSend: (content: string, attachments?: Attachment[]) => void;
  onStop?: () => void;
  disabled?: boolean;
  isStreaming?: boolean;
  cliApps?: CliAppInfo[];
  mcpPresets?: McpPresetInfo[];
}

let nextId = 0;
function uid(prefix: string) {
  return `${prefix}-${Date.now()}-${++nextId}`;
}

export function ThreadComposer({
  onSend,
  onStop,
  disabled = false,
  isStreaming = false,
}: ThreadComposerProps) {
  const { t } = useTranslation();
  const [content, setContent] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [recording, setRecording] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);

  // Auto-resize textarea to fit content, capped at 200px
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [content]);

  const handleSend = useCallback(() => {
    const trimmed = content.trim();
    if (!trimmed && attachments.length === 0) return;
    if (disabled || isStreaming) return;
    onSend(trimmed, attachments.length > 0 ? attachments : undefined);
    setContent("");
    setAttachments([]);
  }, [content, attachments, disabled, isStreaming, onSend]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend],
  );

  const handleFileChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;
    const newAttachments: Attachment[] = Array.from(files).map((file) => ({
      id: uid("att"),
      name: file.name,
      type: (file.type.startsWith("image/") ? "image" : "file") as Attachment["type"],
      url: file.type.startsWith("image/") ? URL.createObjectURL(file) : undefined,
      file,
    }));
    setAttachments((prev) => [...prev, ...newAttachments]);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }, []);

  const removeAttachment = useCallback((id: string) => {
    setAttachments((prev) => {
      const att = prev.find((a) => a.id === id);
      if (att?.url) URL.revokeObjectURL(att.url);
      return prev.filter((a) => a.id !== id);
    });
  }, []);

  // Voice recording
  const startRecording = useCallback(async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mimeType = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "audio/mp4";
      const recorder = new MediaRecorder(stream, { mimeType });
      chunksRef.current = [];
      recorder.ondataavailable = (ev) => {
        if (ev.data.size > 0) chunksRef.current.push(ev.data);
      };
      recorder.onstop = () => {
        const blob = new Blob(chunksRef.current, { type: mimeType });
        stream.getTracks().forEach((tr) => tr.stop());
        const ext = mimeType.includes("webm") ? "webm" : "mp4";
        setAttachments((prev) => [
          ...prev,
          {
            id: uid("voice"),
            name: `recording.${ext}`,
            type: "file",
            file: new File([blob], `recording.${ext}`, { type: mimeType }),
          },
        ]);
      };
      mediaRecorderRef.current = recorder;
      recorder.start();
      setRecording(true);
    } catch {
      // Mic permission denied — silently ignore
    }
  }, []);

  const stopRecording = useCallback(() => {
    mediaRecorderRef.current?.stop();
    setRecording(false);
  }, []);

  const canSend = (content.trim() || attachments.length > 0) && !disabled && !isStreaming;

  return (
    <div className="border-t bg-background p-4">
      {attachments.length > 0 && (
        <div className="mb-2">
          <AttachmentTileList attachments={attachments} onRemove={removeAttachment} />
        </div>
      )}
      <div className="flex items-end gap-2 max-w-3xl mx-auto">
        <textarea
          ref={textareaRef}
          value={content}
          onChange={(e) => setContent(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={t("chat.input.placeholder", "Type a message...")}
          rows={1}
          disabled={disabled}
          className="flex-1 min-h-[44px] max-h-[200px] rounded-lg border border-input bg-background px-3 py-2 text-sm resize-none focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-50"
        />
        <div className="flex items-center gap-1 shrink-0">
          <Button
            variant="ghost"
            size="icon"
            disabled={disabled}
            onClick={() => fileInputRef.current?.click()}
            title={t("chat.attach", "Attach file")}
          >
            <Paperclip className="h-4 w-4" />
          </Button>
          <input ref={fileInputRef} type="file" multiple hidden onChange={handleFileChange} />

          <Button
            variant="ghost"
            size="icon"
            disabled={disabled}
            onClick={recording ? stopRecording : startRecording}
            className={cn(recording && "text-red-500 animate-pulse")}
            title={recording ? t("chat.stopRecording", "Stop recording") : t("chat.startRecording", "Start recording")}
          >
            {recording ? <Loader2 className="h-4 w-4 animate-spin" /> : <Mic className="h-4 w-4" />}
          </Button>

          {isStreaming ? (
            <Button size="icon" onClick={onStop} variant="destructive" title={t("chat.stop", "Stop")}>
              <Square className="h-4 w-4" />
            </Button>
          ) : (
            <Button size="icon" onClick={handleSend} disabled={!canSend} title={t("chat.send", "Send")}>
              <ArrowUp className="h-4 w-4" />
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}