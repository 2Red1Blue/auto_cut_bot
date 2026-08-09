"use client";

import { useState } from "react";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { X, ChevronLeft, ChevronRight, Download } from "lucide-react";
import { cn } from "@/lib/utils";

interface ImageLightboxProps {
  images: { url: string; name?: string }[];
  initialIndex?: number;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function ImageLightbox({
  images,
  initialIndex = 0,
  open,
  onOpenChange,
}: ImageLightboxProps) {
  const [index, setIndex] = useState(initialIndex);

  const current = images[index];
  if (!current) return null;

  const goNext = () => setIndex((i) => (i + 1) % images.length);
  const goPrev = () => setIndex((i) => (i - 1 + images.length) % images.length);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[90vw] max-h-[90vh] p-0 bg-black/95 border-none">
        <div className="relative flex items-center justify-center h-full">
          {/* Close button */}
          <button
            onClick={() => onOpenChange(false)}
            className="absolute top-4 right-4 z-10 rounded-full bg-black/50 p-2 text-white hover:bg-black/70"
          >
            <X className="h-5 w-5" />
          </button>

          {/* Navigation */}
          {images.length > 1 && (
            <>
              <button
                onClick={goPrev}
                className="absolute left-4 z-10 rounded-full bg-black/50 p-2 text-white hover:bg-black/70"
              >
                <ChevronLeft className="h-5 w-5" />
              </button>
              <button
                onClick={goNext}
                className="absolute right-4 z-10 rounded-full bg-black/50 p-2 text-white hover:bg-black/70"
              >
                <ChevronRight className="h-5 w-5" />
              </button>
            </>
          )}

          {/* Image */}
          <img
            src={current.url}
            alt={current.name || "Image"}
            className="max-w-full max-h-[85vh] object-contain"
          />

          {/* Info bar */}
          <div className="absolute bottom-4 left-1/2 -translate-x-1/2 flex items-center gap-4 rounded-full bg-black/50 px-4 py-2 text-white text-sm">
            <span>
              {index + 1} / {images.length}
            </span>
            {current.name && <span className="text-white/70">{current.name}</span>}
            <a
              href={current.url}
              download={current.name}
              target="_blank"
              rel="noopener noreferrer"
              className="hover:text-white/80"
            >
              <Download className="h-4 w-4" />
            </a>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}