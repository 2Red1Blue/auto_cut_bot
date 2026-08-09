"use client";

import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState, type ReactNode } from "react";
import { ArrowDown } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { UIMessage } from "@/lib/types";

export interface ThreadViewportHandle {
  jumpToUserPrompt: (promptId: string) => void;
  cancelAutoScroll: () => void;
}

interface ThreadViewportProps {
  messages: UIMessage[];
  isStreaming: boolean;
  composer: ReactNode;
  emptyState?: ReactNode;
  onOpenFilePreview?: (path: string) => void;
}

export const ThreadViewport = forwardRef<ThreadViewportHandle, ThreadViewportProps>(
  function ThreadViewport({ messages, isStreaming, composer, emptyState, onOpenFilePreview }, ref) {
    const { t } = useTranslation();
    const scrollRef = useRef<HTMLDivElement>(null);
    const [showScrollButton, setShowScrollButton] = useState(false);
    const autoScrollRef = useRef(true);

    useImperativeHandle(ref, () => ({
      jumpToUserPrompt: (promptId: string) => {
        const el = document.getElementById(`prompt-${promptId}`);
        el?.scrollIntoView({ behavior: "smooth", block: "start" });
      },
      cancelAutoScroll: () => {
        autoScrollRef.current = false;
      },
    }));

    const scrollToBottom = useCallback(() => {
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
      autoScrollRef.current = true;
      setShowScrollButton(false);
    }, []);

    const handleScroll = useCallback(() => {
      if (!scrollRef.current) return;
      const { scrollTop, scrollHeight, clientHeight } = scrollRef.current;
      const nearBottom = scrollHeight - scrollTop - clientHeight < 100;
      setShowScrollButton(!nearBottom);
      if (nearBottom && isStreaming) {
        autoScrollRef.current = true;
      }
    }, [isStreaming]);

    useEffect(() => {
      if (autoScrollRef.current) {
        scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "instant" });
      }
    }, [messages]);

    return (
      <div className="relative flex-1 min-h-0">
        <div
          ref={scrollRef}
          onScroll={handleScroll}
          className="h-full overflow-y-auto"
        >
          {messages.length === 0 && emptyState ? (
            emptyState
          ) : (
            <div className="max-w-3xl mx-auto py-4 space-y-4">
              {messages.map((msg) => (
                <div
                  key={msg.id}
                  id={msg.turnId ? `prompt-${msg.turnId}` : undefined}
                  className={cn(
                    "px-4",
                    msg.role === "user" ? "flex justify-end" : "flex justify-start"
                  )}
                >
                  <div
                    className={cn(
                      "max-w-[80%] rounded-lg px-4 py-2 text-sm",
                      msg.role === "user"
                        ? "bg-primary text-primary-foreground"
                        : msg.kind === "trace"
                        ? "bg-muted/50 text-muted-foreground text-xs"
                        : "bg-muted text-foreground"
                    )}
                  >
                    <div className="whitespace-pre-wrap break-words">{msg.content}</div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {showScrollButton && (
          <Button
            variant="secondary"
            size="icon"
            className="absolute bottom-20 right-4 rounded-full shadow-lg"
            onClick={scrollToBottom}
          >
            <ArrowDown className="h-4 w-4" />
          </Button>
        )}

        {composer}
      </div>
    );
  }
);