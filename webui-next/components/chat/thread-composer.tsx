"use client";

import {
  useState,
  useRef,
  useCallback,
  useEffect,
  useMemo,
  type KeyboardEvent,
  type ChangeEvent,
  type FormEvent,
} from "react";
import { useTranslation } from "react-i18next";
import {
  ArrowUp,
  Square,
  Mic,
  Loader2,
  Plus,
  X,
  Quote,
  SquarePen,
  CircleHelp,
  Activity,
  BookOpen,
  Brain,
  History,
  RotateCw,
  Shield,
  Sparkles,
  Undo2,
  type LucideIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { AttachmentTileList, type Attachment } from "./attachment-tile";
import type { SlashCommand } from "@/lib/types";
import { slashCommandLifecycle } from "@/lib/slash-command";

// ── Types ───────────────────────────────────────────────────────────────────

interface ThreadComposerProps {
  sessionId: string;
  onSend: (content: string, attachments?: Attachment[]) => void;
  onStop?: () => void;
  disabled?: boolean;
  isStreaming?: boolean;
  placeholder?: string;
  slashCommands?: SlashCommand[];
  quotedContext?: string | null;
  onQuotedContextChange?: (text: string | null) => void;
}

interface SlashPaletteEntry {
  command: string;
  title: string;
  description: string;
  icon: string;
  argHint?: string;
  detail: string;
  badge?: string;
  recent: boolean;
}

// ── Constants ───────────────────────────────────────────────────────────────

const COMMAND_ICONS: Record<string, LucideIcon> = {
  activity: Activity,
  "book-open": BookOpen,
  brain: Brain,
  "circle-help": CircleHelp,
  history: History,
  "rotate-cw": RotateCw,
  shield: Shield,
  sparkles: Sparkles,
  square: Square,
  "square-pen": SquarePen,
  "undo-2": Undo2,
};

const SLASH_RECENTS_STORAGE_KEY = "auto_cut_bot.webui.slashCommandRecents";
const SLASH_RECENTS_LIMIT = 5;
const MAX_TEXTAREA_HEIGHT = 260;

// ── Helpers ─────────────────────────────────────────────────────────────────

let nextId = 0;
function uid(prefix: string): string {
  return `${prefix}-${Date.now()}-${++nextId}`;
}

function readSlashRecents(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(SLASH_RECENTS_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed)
      ? parsed.filter((item): item is string => typeof item === "string").slice(0, SLASH_RECENTS_LIMIT)
      : [];
  } catch {
    return [];
  }
}

function storeSlashRecents(commands: string[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      SLASH_RECENTS_STORAGE_KEY,
      JSON.stringify(commands.slice(0, SLASH_RECENTS_LIMIT)),
    );
  } catch {
    // localStorage unavailable — no-op
  }
}

function slashCommandI18nKey(command: string): string {
  return command.replace(/^\//, "").replace(/-/g, "_");
}

// ── Component ───────────────────────────────────────────────────────────────

export function ThreadComposer({
  onSend,
  onStop,
  disabled = false,
  isStreaming = false,
  placeholder,
  slashCommands = [],
  quotedContext = null,
  onQuotedContextChange,
}: ThreadComposerProps) {
  const { t } = useTranslation();
  const [value, setValue] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [recording, setRecording] = useState(false);
  const [inlineError, setInlineError] = useState<string | null>(null);
  const [slashMenuDismissed, setSlashMenuDismissed] = useState(false);
  const [selectedCommandIndex, setSelectedCommandIndex] = useState(0);
  const [recentSlashCommands, setRecentSlashCommands] = useState<string[]>(() => readSlashRecents());
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);

  // ── Derived state ─────────────────────────────────────────────────────────

  const resolvedPlaceholder = isStreaming
    ? t("thread.composer.placeholderStreaming", "Streaming in progress...")
    : placeholder ?? t("thread.composer.placeholderThread", "Type a message...");

  const hasContent = value.trim().length > 0 || attachments.length > 0;
  const canSend = hasContent && !disabled && !isStreaming;

  // ── Slash command query ───────────────────────────────────────────────────

  const slashQuery = useMemo(() => {
    if (disabled || slashMenuDismissed || !value.startsWith("/")) return null;
    const commandToken = value.slice(1);
    if (/\s/.test(commandToken)) return null;
    return commandToken.toLowerCase();
  }, [disabled, slashMenuDismissed, value]);

  const filteredSlashCommands = useMemo<SlashPaletteEntry[]>(() => {
    if (slashQuery === null) return [];
    const activeCommands = isStreaming && onStop
      ? slashCommands.filter((c) => c.command === "/stop")
      : slashCommands;

    const results = activeCommands
      .filter((command) => {
        if (slashQuery === "") {
          // Hide /restart and /stop (when not streaming) when query is empty
          if (command.command === "/restart") return false;
          if (command.command === "/stop" && !(isStreaming && onStop)) return false;
          return true;
        }
        const commandKey = slashCommandI18nKey(command.command);
        const title = t(`thread.composer.slash.commands.${commandKey}.title`, {
          defaultValue: command.title,
        });
        const description = t(`thread.composer.slash.commands.${commandKey}.description`, {
          defaultValue: command.description,
        });
        const haystack = [command.command, title, description, command.argHint ?? ""]
          .join(" ")
          .toLowerCase();
        return haystack.includes(slashQuery);
      })
      .map((command) => {
        const commandKey = slashCommandI18nKey(command.command);
        const description = t(`thread.composer.slash.commands.${commandKey}.description`, {
          defaultValue: command.description,
        });
        return {
          command: command.command,
          title: command.title,
          description: command.description,
          icon: command.icon,
          argHint: command.argHint,
          detail: description,
          recent: recentSlashCommands.includes(command.command),
        };
      })
      .sort((a, b) => {
        if (isStreaming) {
          if (a.command === "/stop") return -1;
          if (b.command === "/stop") return 1;
        }
        if (slashQuery !== "") return 0;
        const aIdx = recentSlashCommands.indexOf(a.command);
        const bIdx = recentSlashCommands.indexOf(b.command);
        if (aIdx === -1 && bIdx === -1) return 0;
        if (aIdx === -1) return 1;
        if (bIdx === -1) return -1;
        return aIdx - bIdx;
      });

    return results.slice(0, 8);
  }, [recentSlashCommands, isStreaming, onStop, slashCommands, slashQuery, t]);

  const showSlashMenu = filteredSlashCommands.length > 0;

  // ── Reset selection index when query changes, clamp when length changes ──

  const prevSlashQueryRef = useRef(slashQuery);
  if (prevSlashQueryRef.current !== slashQuery) {
    prevSlashQueryRef.current = slashQuery;
    // React allows render-phase setState for resetting state when props change
    setSelectedCommandIndex(0);
  }
  const safeSelectedIndex = Math.min(
    selectedCommandIndex,
    Math.max(0, filteredSlashCommands.length - 1),
  );

  // ── Dismiss slash menu on outside click ───────────────────────────────────

  useEffect(() => {
    if (!showSlashMenu) return;
    const dismiss = (event: PointerEvent) => {
      const target = event.target as Node | null;
      if (target && textareaRef.current?.closest("form")?.contains(target)) return;
      setSlashMenuDismissed(true);
    };
    document.addEventListener("pointerdown", dismiss, true);
    return () => document.removeEventListener("pointerdown", dismiss, true);
  }, [showSlashMenu]);

  // ── Auto-resize textarea ──────────────────────────────────────────────────

  const resizeTextarea = useCallback(() => {
    requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (!el) return;
      el.style.height = "auto";
      el.style.height = `${Math.min(el.scrollHeight, MAX_TEXTAREA_HEIGHT)}px`;
    });
  }, []);

  useEffect(() => {
    resizeTextarea();
  }, [value, resizeTextarea]);

  // ── Auto-focus ────────────────────────────────────────────────────────────

  useEffect(() => {
    if (disabled) return;
    const id = requestAnimationFrame(() => textareaRef.current?.focus());
    return () => cancelAnimationFrame(id);
  }, [disabled]);

  // ── Choose slash command ──────────────────────────────────────────────────

  const chooseSlashCommand = useCallback(
    (command: SlashPaletteEntry) => {
      if (command.command === "/stop" && isStreaming && onStop) {
        onStop();
        setValue("");
        setSlashMenuDismissed(true);
        setInlineError(null);
        resizeTextarea();
        return;
      }

      const nextRecents = [
        command.command,
        ...recentSlashCommands.filter((item) => item !== command.command),
      ].slice(0, SLASH_RECENTS_LIMIT);
      setRecentSlashCommands(nextRecents);
      storeSlashRecents(nextRecents);

      setValue(command.argHint ? `${command.command} ` : command.command);
      setSlashMenuDismissed(true);
      setInlineError(null);
      resizeTextarea();
    },
    [isStreaming, onStop, recentSlashCommands, resizeTextarea],
  );

  // ── Send ──────────────────────────────────────────────────────────────────

  const submit = useCallback(() => {
    if (!canSend) return;
    const trimmed = value.trim();

    const lifecycle = slashCommandLifecycle(trimmed, slashCommands);
    if (lifecycle === "stop_active_turn" && isStreaming && onStop) {
      onStop();
      setValue("");
      setAttachments([]);
      setInlineError(null);
      resizeTextarea();
      return;
    }

    onSend(
      trimmed,
      attachments.length > 0 ? attachments : undefined,
    );
    setValue("");
    setAttachments([]);
    setInlineError(null);
    setSlashMenuDismissed(false);
    resizeTextarea();
  }, [
    canSend,
    value,
    slashCommands,
    isStreaming,
    onStop,
    onSend,
    attachments,
    resizeTextarea,
  ]);

  // ── Keyboard handler ──────────────────────────────────────────────────────

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      // Slash menu navigation
      if (showSlashMenu) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          setSelectedCommandIndex((idx) => (idx + 1) % filteredSlashCommands.length);
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          setSelectedCommandIndex(
            (idx) => (idx - 1 + filteredSlashCommands.length) % filteredSlashCommands.length,
          );
          return;
        }
        if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
          e.preventDefault();
          chooseSlashCommand(filteredSlashCommands[safeSelectedIndex]);
          return;
        }
        if (e.key === "Escape") {
          e.preventDefault();
          setSlashMenuDismissed(true);
          return;
        }
      }

      // Enter to send, Shift+Enter for newline
      if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
        e.preventDefault();
        submit();
      }
    },
    [showSlashMenu, filteredSlashCommands, safeSelectedIndex, chooseSlashCommand, submit],
  );

  // ── Input handler ─────────────────────────────────────────────────────────

  const onInput = useCallback((e: React.FormEvent<HTMLTextAreaElement>) => {
    if ((e.nativeEvent as InputEvent).isComposing) return;
    const el = e.currentTarget;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_TEXTAREA_HEIGHT)}px`;
  }, []);

  // ── File attachments ──────────────────────────────────────────────────────

  const handleFileChange = useCallback((e: ChangeEvent<HTMLInputElement>) => {
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
    setInlineError(null);
  }, []);

  const removeAttachment = useCallback((id: string) => {
    setAttachments((prev) => {
      const att = prev.find((a) => a.id === id);
      if (att?.url) URL.revokeObjectURL(att.url);
      return prev.filter((a) => a.id !== id);
    });
  }, []);

  // ── Voice recording ───────────────────────────────────────────────────────

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

  // ── Cleanup ───────────────────────────────────────────────────────────────

  useEffect(() => {
    return () => {
      // Revoke any remaining blob URLs
      attachments.forEach((a) => {
        if (a.url) URL.revokeObjectURL(a.url);
      });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Render ────────────────────────────────────────────────────────────────

  const normalizedQuotedContext = quotedContext?.trim() || null;

  return (
    <form
      onSubmit={(e: FormEvent) => {
        e.preventDefault();
        submit();
      }}
      className="relative w-full"
    >
      {/* Slash command palette */}
      {showSlashMenu ? (
        <div
          role="listbox"
          aria-label={t("thread.composer.slash.ariaLabel", "Slash commands")}
          className={cn(
            "absolute bottom-full left-1/2 z-30 mb-2 w-[calc(100%-0.5rem)] -translate-x-1/2 overflow-hidden",
            "rounded-xl border border-border bg-popover shadow-lg",
            "max-w-[58rem]",
          )}
          style={{ maxHeight: 288 }}
        >
          <div className="overflow-y-auto p-1" style={{ maxHeight: 276 }}>
            {filteredSlashCommands.map((command, index) => {
              const Icon = COMMAND_ICONS[command.icon] ?? CircleHelp;
              const selected = index === safeSelectedIndex;
              const commandKey = slashCommandI18nKey(command.command);
              const title = t(`thread.composer.slash.commands.${commandKey}.title`, {
                defaultValue: command.title,
              });
              const description = t(`thread.composer.slash.commands.${commandKey}.description`, {
                defaultValue: command.description,
              });
              return (
                <button
                  key={command.command}
                  type="button"
                  role="option"
                  aria-selected={selected}
                  onMouseEnter={() => setSelectedCommandIndex(index)}
                  onMouseDown={(e) => {
                    e.preventDefault();
                    chooseSlashCommand(command);
                  }}
                  className={cn(
                    "flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left transition-colors",
                    selected
                      ? "bg-foreground/[0.065] text-foreground"
                      : "text-foreground/86 hover:bg-foreground/[0.045]",
                  )}
                >
                  <span
                    className={cn(
                      "flex h-7 w-7 shrink-0 items-center justify-center text-muted-foreground transition-colors",
                      selected && "text-foreground",
                    )}
                  >
                    <Icon className="h-4 w-4" />
                  </span>
                  <span className="flex min-w-0 flex-1 flex-col sm:flex-row sm:items-baseline sm:gap-2">
                    <span className="text-[13.5px] font-semibold text-foreground min-w-0 truncate">
                      {title}
                    </span>
                    <span className="min-w-0 truncate text-[13px] text-muted-foreground">
                      {command.detail || description}
                    </span>
                  </span>
                  <span className="ml-2 flex shrink-0 items-center gap-1.5">
                    {command.recent ? (
                      <span className="hidden rounded-full bg-foreground/[0.055] px-2 py-1 text-[11px] font-medium text-muted-foreground sm:inline-flex">
                        {t("thread.composer.slash.badges.recent", "Recent")}
                      </span>
                    ) : null}
                    <span className="font-mono text-[12px] text-muted-foreground/60">
                      {command.argHint ? `${command.command} ${command.argHint}` : command.command}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      ) : null}

      {/* Composer surface */}
      <div
        className={cn(
          "relative mx-auto flex w-full flex-col overflow-visible transition-all duration-200",
          "max-w-[58rem] rounded-[28px] bg-muted/30 focus-within:bg-muted/50 dark:bg-card dark:focus-within:bg-white/[0.06]",
          disabled && "opacity-60",
        )}
      >
        {/* Attachment previews */}
        {attachments.length > 0 ? (
          <div className="flex flex-wrap gap-2 px-3 pt-3" aria-label={t("thread.composer.attachImage", "Attachments")}>
            <AttachmentTileList attachments={attachments} onRemove={removeAttachment} />
          </div>
        ) : null}

        {/* Quoted context banner */}
        {normalizedQuotedContext ? (
          <div
            className="mx-3 mt-3 flex min-w-0 items-start gap-2 border-l-2 border-muted-foreground/25 pl-3 pr-1 text-muted-foreground"
            aria-label={t("thread.composer.quotedContext", "Quoted context")}
          >
            <Quote className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
            <p className="line-clamp-2 min-w-0 flex-1 text-[13px]/[1.45]">
              {normalizedQuotedContext}
            </p>
            <button
              type="button"
              className="-mr-1 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-full transition-colors hover:bg-muted/70 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              aria-label={t("thread.composer.removeQuotedContext", "Remove quoted context")}
              onClick={() => {
                onQuotedContextChange?.(null);
                requestAnimationFrame(() => textareaRef.current?.focus());
              }}
            >
              <X className="h-3.5 w-3.5" aria-hidden />
            </button>
          </div>
        ) : null}

        {/* Textarea */}
        <div className="relative">
          <textarea
            ref={textareaRef}
            value={value}
            onChange={(e) => {
              setValue(e.target.value);
              setSlashMenuDismissed(false);
            }}
            onInput={onInput}
            onKeyDown={handleKeyDown}
            rows={1}
            placeholder={resolvedPlaceholder}
            disabled={disabled}
            aria-label={t("thread.composer.inputAria", "Message input")}
            className={cn(
              "w-full resize-none bg-transparent",
              "min-h-[50px] px-3.5 pb-1.5 pt-3 text-[16px] leading-5 sm:px-4",
              "caret-foreground placeholder:text-muted-foreground/70",
              "focus:outline-none focus-visible:outline-none",
              "disabled:cursor-not-allowed",
            )}
          />
        </div>

        {/* Inline error */}
        {inlineError ? (
          <div
            role="alert"
            className="mx-3 mb-1 rounded-md border border-destructive/40 bg-destructive/8 px-2.5 py-1 text-[11.5px] font-medium text-destructive"
          >
            {inlineError}
          </div>
        ) : null}

        {/* Footer toolbar */}
        <div className="flex flex-nowrap items-center gap-x-2 px-2.5 pb-2 sm:px-3">
          {/* Left side: attach button */}
          <div className="flex min-w-0 flex-1 basis-0 items-center gap-2">
            <input
              ref={fileInputRef}
              type="file"
              accept="image/*,.pdf,.txt,.csv,.json,.md,.py,.js,.ts,.tsx,.jsx,.html,.css,.yaml,.yml,.toml,.xml"
              multiple
              hidden
              onChange={handleFileChange}
            />
            <Button
              type="button"
              size="icon"
              variant="ghost"
              disabled={disabled}
              aria-label={t("thread.composer.attachImage", "Attach file")}
              onClick={() => fileInputRef.current?.click()}
              className={cn(
                "touch-target rounded-full text-muted-foreground hover:text-foreground",
                "h-9 w-9 border border-border/55 bg-card shadow-[0_2px_8px_rgba(15,23,42,0.05)] hover:bg-card",
              )}
            >
              <Plus className="h-4 w-4" />
            </Button>
          </div>

          {/* Right side: voice + send/stop */}
          <div className="ml-auto flex min-w-0 items-center justify-end gap-2">
            {/* Voice recording button */}
            <Button
              type="button"
              size="icon"
              variant="ghost"
              disabled={disabled}
              aria-label={recording ? t("thread.composer.voice.stop", "Stop recording") : t("thread.composer.tools.voice", "Voice input")}
              title={recording ? t("thread.composer.voice.stop", "Stop recording") : t("thread.composer.voice.hint", "Record voice message")}
              onClick={recording ? stopRecording : startRecording}
              className={cn(
                "touch-target rounded-full text-muted-foreground hover:text-foreground",
                "h-9 w-9 border border-transparent hover:bg-muted/65",
                recording &&
                  "bg-red-500 text-white shadow-[0_8px_20px_rgba(239,68,68,0.22)] hover:bg-red-500 hover:text-white",
              )}
            >
              {recording ? (
                <Square className="h-3.5 w-3.5" fill="currentColor" />
              ) : (
                <Mic className="h-4 w-4" />
              )}
            </Button>

            {/* Send / Stop button */}
            {isStreaming ? (
              <Button
                type="button"
                size="icon"
                disabled={disabled}
                aria-label={t("thread.composer.stop", "Stop")}
                onClick={onStop}
                className={cn(
                  "touch-target rounded-full transition-transform",
                  "border border-border/70 bg-card text-foreground/85 shadow-[0_3px_10px_rgba(15,23,42,0.08)]",
                  "hover:bg-muted/65 hover:text-foreground disabled:text-muted-foreground/50",
                  "h-9 w-9 hover:scale-[1.03] active:scale-95",
                )}
              >
                <Square className="h-3.5 w-3.5" fill="currentColor" stroke="currentColor" />
              </Button>
            ) : (
              <Button
                type="submit"
                size="icon"
                disabled={!canSend}
                aria-label={t("thread.composer.send", "Send")}
                className={cn(
                  "touch-target rounded-full transition-transform",
                  "border border-foreground bg-foreground text-background shadow-[0_3px_10px_rgba(15,23,42,0.18)]",
                  "hover:bg-foreground/90 disabled:border-foreground disabled:bg-foreground disabled:text-background",
                  "h-9 w-9",
                  canSend && "hover:scale-[1.03] active:scale-95",
                )}
              >
                {isStreaming ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <ArrowUp className="h-4 w-4" />
                )}
              </Button>
            )}
          </div>
        </div>
      </div>
    </form>
  );
}