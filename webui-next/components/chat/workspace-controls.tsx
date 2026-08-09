"use client";

import type { WorkspaceScopePayload } from "@/lib/types";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Folder } from "lucide-react";
import { cn } from "@/lib/utils";

interface WorkspaceControlsProps {
  workspaceScope?: WorkspaceScopePayload;
  onScopeChange?: (scope: WorkspaceScopePayload) => void;
}
export function WorkspaceControls({
  workspaceScope,
  onScopeChange,
}: WorkspaceControlsProps) {
  const { t } = useTranslation();

  const label = workspaceScope?.project_name
    || workspaceScope?.project_path
    || t("workspace.none", "No workspace");

  return (
    <div
      className={cn(
        "flex items-center gap-2 rounded-md px-2 py-1 text-sm",
        workspaceScope ? "text-muted-foreground" : "text-muted-foreground/50 italic",
      )}
    >
      <Folder className="h-4 w-4 shrink-0" />
      <span className="truncate max-w-[160px]" title={label}>
        {label}
      </span>
      {onScopeChange && workspaceScope && (
        <Button
          variant="ghost"
          size="sm"
          className="h-6 px-2 text-xs"
          onClick={() => onScopeChange(workspaceScope)}
        >
          {t("workspace.change", "Change")}
        </Button>
      )}
      {onScopeChange && !workspaceScope && (
        <Button
          variant="ghost"
          size="sm"
          className="h-6 px-2 text-xs"
          onClick={() => {
            onScopeChange({
              project_path: "",
              access_mode: "full",
            });
          }}
        >
          {t("workspace.select", "Select")}
        </Button>
      )}
    </div>
  );
}