"use client";

import { useTranslation } from "react-i18next";
import { AlertTriangle, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { StreamError } from "@/lib/auto_cut_bot-client";

interface StreamErrorNoticeProps {
  error: StreamError;
  onDismiss: () => void;
}

export function StreamErrorNotice({ error, onDismiss }: StreamErrorNoticeProps) {
  const { t } = useTranslation();

  return (
    <div
      role="alert"
      aria-live="assertive"
      className={cn(
        "mb-2 flex items-start gap-2 rounded-lg border border-destructive/30",
        "bg-destructive/10 px-3 py-2 text-[12px] leading-5 text-destructive"
      )}
    >
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
      <div className="flex-1 min-w-0">
        <p className="font-medium">{error.kind || t("error.streamError", "Stream Error")}</p>
        {"detail" in error && (
          <p className="mt-0.5 text-destructive/80">{(error as any).detail || (error as any).reason}</p>
        )}
      </div>
      <Button variant="ghost" size="icon" className="h-5 w-5 shrink-0" onClick={onDismiss}>
        <X className="h-3 w-3" />
      </Button>
    </div>
  );
}