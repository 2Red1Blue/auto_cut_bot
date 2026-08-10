"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const MAX_IMAGES = 10;
const MAX_DIMENSION = 1920;

export interface AttachedImage {
  id: string;
  file: File;
  previewUrl: string;
  width: number;
  height: number;
}

export interface UseAttachedImagesReturn {
  images: AttachedImage[];
  addImages: (files: FileList | File[]) => void;
  removeImage: (id: string) => void;
  reorderImages: (fromIndex: number, toIndex: number) => void;
  clearAll: () => void;
  isFull: boolean;
}

function genId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * Resize an image file to fit within MAX_DIMENSION while preserving aspect ratio.
 * Returns a new File with the resized image data.
 */
function resizeImage(file: File, maxDim: number): Promise<File> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    const url = URL.createObjectURL(file);

    img.onload = () => {
      URL.revokeObjectURL(url);

      const { naturalWidth, naturalHeight } = img;
      if (naturalWidth <= maxDim && naturalHeight <= maxDim) {
        resolve(file);
        return;
      }

      const scale = maxDim / Math.max(naturalWidth, naturalHeight);
      const width = Math.round(naturalWidth * scale);
      const height = Math.round(naturalHeight * scale);

      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      const ctx = canvas.getContext("2d");
      if (!ctx) {
        reject(new Error("Failed to get canvas context"));
        return;
      }
      ctx.drawImage(img, 0, 0, width, height);

      canvas.toBlob((blob) => {
        if (!blob) {
          reject(new Error("Failed to create blob from canvas"));
          return;
        }
        const resized = new File([blob], file.name, {
          type: file.type || "image/png",
          lastModified: file.lastModified,
        });
        resolve(resized);
      }, file.type || "image/png");
    };

    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("Failed to load image"));
    };

    img.src = url;
  });
}

/**
 * Check if a file is an image based on its MIME type.
 */
function isImageFile(file: File): boolean {
  return file.type.startsWith("image/");
}

export function useAttachedImages(): UseAttachedImagesReturn {
  const [images, setImages] = useState<AttachedImage[]>([]);
  const imagesRef = useRef<AttachedImage[]>([]);
  imagesRef.current = images;

  const addImages = useCallback(async (files: FileList | File[]) => {
    const imageFiles = Array.from(files).filter(isImageFile);
    if (imageFiles.length === 0) return;

    const available = MAX_IMAGES - imagesRef.current.length;
    if (available <= 0) return;

    const toAdd = imageFiles.slice(0, available);

    try {
      const resized = await Promise.all(
        toAdd.map((file) => resizeImage(file, MAX_DIMENSION))
      );

      const newImages: AttachedImage[] = resized.map((file) => {
        const url = URL.createObjectURL(file);
        // Get dimensions from the resized file name or from the original
        // We'll use a temporary image to get dimensions
        const dimensions = { width: 0, height: 0 };
        const tempImg = new Image();
        tempImg.src = url;

        return {
          id: genId(),
          file,
          previewUrl: url,
          width: 0,
          height: 0,
        };
      });

      // Load dimensions for all images
      await Promise.all(
        newImages.map(
          (img) =>
            new Promise<void>((resolve) => {
              const tempImg = new Image();
              tempImg.onload = () => {
                img.width = tempImg.naturalWidth;
                img.height = tempImg.naturalHeight;
                resolve();
              };
              tempImg.onerror = () => resolve();
              tempImg.src = img.previewUrl;
            })
        )
      );

      setImages((prev) => {
        const merged = [...prev, ...newImages].slice(0, MAX_IMAGES);
        imagesRef.current = merged;
        return merged;
      });
    } catch {
      // Silently ignore resize failures; the image is simply not added
    }
  }, []);

  const removeImage = useCallback((id: string) => {
    setImages((prev) => {
      const target = prev.find((img) => img.id === id);
      if (target) {
        URL.revokeObjectURL(target.previewUrl);
      }
      const next = prev.filter((img) => img.id !== id);
      imagesRef.current = next;
      return next;
    });
  }, []);

  const reorderImages = useCallback((fromIndex: number, toIndex: number) => {
    setImages((prev) => {
      if (
        fromIndex < 0 ||
        fromIndex >= prev.length ||
        toIndex < 0 ||
        toIndex >= prev.length
      ) {
        return prev;
      }
      const next = [...prev];
      const [moved] = next.splice(fromIndex, 1);
      next.splice(toIndex, 0, moved);
      imagesRef.current = next;
      return next;
    });
  }, []);

  const clearAll = useCallback(() => {
    setImages((prev) => {
      for (const img of prev) {
        URL.revokeObjectURL(img.previewUrl);
      }
      imagesRef.current = [];
      return [];
    });
  }, []);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      for (const img of imagesRef.current) {
        URL.revokeObjectURL(img.previewUrl);
      }
    };
  }, []);

  // Keyboard paste handler
  useEffect(() => {
    const handlePaste = (e: ClipboardEvent) => {
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
        addImages(files);
      }
    };

    document.addEventListener("paste", handlePaste);
    return () => document.removeEventListener("paste", handlePaste);
  }, [addImages]);

  const isFull = images.length >= MAX_IMAGES;

  return {
    images,
    addImages,
    removeImage,
    reorderImages,
    clearAll,
    isFull,
  };
}