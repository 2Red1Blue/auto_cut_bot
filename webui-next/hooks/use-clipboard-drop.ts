"use client";

import { useState, useCallback, useEffect } from "react";

interface UseClipboardDropOptions {
  onFiles?: (files: File[]) => void;
  onText?: (text: string) => void;
}

export function useClipboardAndDrop(options: UseClipboardDropOptions = {}) {
  const [isDragOver, setIsDragOver] = useState(false);

  const handlePaste = useCallback(
    (e: ClipboardEvent) => {
      const items = e.clipboardData?.items;
      if (!items) return;

      const files: File[] = [];
      for (let i = 0; i < items.length; i++) {
        const item = items[i];
        if (item.kind === "file") {
          const file = item.getAsFile();
          if (file) files.push(file);
        }
      }

      if (files.length > 0) {
        e.preventDefault();
        options.onFiles?.(files);
      }
    },
    [options]
  );

  const handleDragOver = useCallback((e: DragEvent) => {
    e.preventDefault();
    setIsDragOver(true);
  }, []);

  const handleDragLeave = useCallback((e: DragEvent) => {
    e.preventDefault();
    setIsDragOver(false);
  }, []);

  const handleDrop = useCallback(
    (e: DragEvent) => {
      e.preventDefault();
      setIsDragOver(false);

      const files = Array.from(e.dataTransfer?.files || []);
      if (files.length > 0) {
        options.onFiles?.(files);
      }

      const text = e.dataTransfer?.getData("text/plain");
      if (text) {
        options.onText?.(text);
      }
    },
    [options]
  );

  useEffect(() => {
    document.addEventListener("paste", handlePaste);
    return () => document.removeEventListener("paste", handlePaste);
  }, [handlePaste]);

  return {
    isDragOver,
    dragHandlers: {
      onDragOver: handleDragOver,
      onDragLeave: handleDragLeave,
      onDrop: handleDrop,
    },
  };
}