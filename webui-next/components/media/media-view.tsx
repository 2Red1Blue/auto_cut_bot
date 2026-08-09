"use client";

import { MediaLibraryPanel } from "@/components/media/media-library-panel";

export function MediaView() {
  return (
    <div className="container mx-auto p-6 space-y-6">
      <div>
        <h1 className="text-3xl font-bold">素材库</h1>
        <p className="text-muted-foreground mt-2">
          管理视频、音频和图片素材
        </p>
      </div>

      <MediaLibraryPanel />
    </div>
  );
}
