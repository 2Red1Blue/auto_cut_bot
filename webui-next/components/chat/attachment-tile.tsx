"use client";

import { useState, useCallback } from "react";
import { X, Upload, Image as ImageIcon, File } from "lucide-react";
import { cn } from "@/lib/utils";

export interface Attachment {
  id: string;
  name: string;
  url?: string;
  type: "image" | "file";
  file?: File;
}

interface AttachmentTileProps {
  attachment: Attachment;
  onRemove?: (id: string) => void;
  onPreview?: (attachment: Attachment) => void;
  className?: string;
}

export function AttachmentTile({
  attachment,
  onRemove,
  onPreview,
  className,
}: AttachmentTileProps) {
  const isImage = attachment.type === "image";

  return (
    <div
      className={cn(
        "relative group inline-flex items-center gap-2 rounded-lg border bg-muted/50 px-3 py-2 text-sm",
        onPreview && "cursor-pointer hover:bg-muted",
        className
      )}
      onClick={() => onPreview?.(attachment)}
    >
      {isImage ? (
        <ImageIcon className="h-4 w-4 text-muted-foreground" />
      ) : (
        <File className="h-4 w-4 text-muted-foreground" />
      )}
      <span className="max-w-[120px] truncate text-xs">{attachment.name}</span>
      {onRemove && (
        <button
          onClick={(e) => {
            e.stopPropagation();
            onRemove(attachment.id);
          }}
          className="shrink-0 rounded-full p-0.5 hover:bg-destructive/10 hover:text-destructive transition-colors"
          title="Remove attachment"
        >
          <X className="h-3 w-3" />
        </button>
      )}
    </div>
  );
}

interface AttachmentTileListProps {
  attachments: Attachment[];
  onRemove?: (id: string) => void;
  onPreview?: (attachment: Attachment) => void;
  className?: string;
}

export function AttachmentTileList({
  attachments,
  onRemove,
  onPreview,
  className,
}: AttachmentTileListProps) {
  if (attachments.length === 0) return null;

  return (
    <div className={cn("flex flex-wrap gap-2", className)}>
      {attachments.map((att) => (
        <AttachmentTile
          key={att.id}
          attachment={att}
          onRemove={onRemove}
          onPreview={onPreview}
        />
      ))}
    </div>
  );
}