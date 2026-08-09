"use client";

import { useState, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { MessageCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import type { UIMessage } from "@/lib/types";

interface PromptNavigatorProps {
  messages: UIMessage[];
  onJumpToPrompt: (turnId: string) => void;
}

export function PromptNavigator({ messages, onJumpToPrompt }: PromptNavigatorProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);

  const userPrompts = useMemo(
    () =>
      messages
        .filter((m) => m.role === "user" && m.turnId)
        .map((m) => ({
          turnId: m.turnId!,
          preview: m.content.slice(0, 80) + (m.content.length > 80 ? "..." : ""),
        })),
    [messages]
  );

  if (userPrompts.length === 0) return null;

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger render={<Button variant="ghost" size="sm" className="gap-1 text-xs" title={t("prompts.navigate", "Navigate prompts")} />}>
          <MessageCircle className="h-3 w-3" />
          {userPrompts.length}
      </PopoverTrigger>
      <PopoverContent className="w-64 p-1" align="start">
        <div className="max-h-60 overflow-y-auto space-y-0.5">
          {userPrompts.map((p, i) => (
            <button
              key={p.turnId}
              onClick={() => {
                onJumpToPrompt(p.turnId);
                setOpen(false);
              }}
              className={cn(
                "w-full text-left px-3 py-2 text-xs rounded-md hover:bg-muted transition-colors",
                "truncate"
              )}
            >
              <span className="text-muted-foreground mr-1">#{i + 1}</span>
              {p.preview}
            </button>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
}