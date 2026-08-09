"use client";

import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Trash2, MoreVertical } from "lucide-react";
import type { MediaAsset } from "@/lib/types/pipeline";

interface MediaGridProps {
  assets: MediaAsset[];
  viewMode: "grid" | "list";
  onDelete: (assetId: string) => void;
  getIcon: (type: string) => React.ReactNode;
  formatDuration: (seconds?: number) => string;
  formatSize: (bytes: number) => string;
}

export function MediaGrid({
  assets,
  viewMode,
  onDelete,
  getIcon,
  formatDuration,
  formatSize,
}: MediaGridProps) {
  if (viewMode === "grid") {
    return (
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-4">
        {assets.map((asset) => (
          <Card key={asset.id} className="group relative overflow-hidden">
            <div className="aspect-video bg-muted relative">
              {asset.thumbnailUrl ? (
                <img
                  src={asset.thumbnailUrl}
                  alt={asset.name}
                  className="w-full h-full object-cover"
                />
              ) : (
                <div className="w-full h-full flex items-center justify-center">
                  {getIcon(asset.type)}
                </div>
              )}
              {asset.duration && (
                <div className="absolute bottom-2 right-2 bg-black/70 text-white text-xs px-2 py-1 rounded">
                  {formatDuration(asset.duration)}
                </div>
              )}
            </div>
            <CardContent className="p-3">
              <div className="flex items-start justify-between gap-2">
                <div className="flex-1 min-w-0">
                  <div className="font-medium text-sm truncate">{asset.name}</div>
                  <div className="text-xs text-muted-foreground mt-1">
                    {formatSize(asset.size)}
                  </div>
                </div>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 opacity-0 group-hover:opacity-100 transition-opacity"
                  onClick={(e) => {
                    e.stopPropagation();
                    onDelete(asset.id);
                  }}
                >
                  <Trash2 className="h-4 w-4" />
                </Button>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    );
  }

  return (
    <Card>
      <CardContent className="p-0">
        <div className="divide-y">
          {assets.map((asset) => (
            <div
              key={asset.id}
              className="flex items-center gap-4 p-4 hover:bg-accent transition-colors"
            >
              <div className="w-16 h-16 bg-muted rounded flex items-center justify-center flex-shrink-0">
                {asset.thumbnailUrl ? (
                  <img
                    src={asset.thumbnailUrl}
                    alt={asset.name}
                    className="w-full h-full object-cover rounded"
                  />
                ) : (
                  getIcon(asset.type)
                )}
              </div>
              <div className="flex-1 min-w-0">
                <div className="font-medium truncate">{asset.name}</div>
                <div className="flex items-center gap-4 text-sm text-muted-foreground mt-1">
                  <span>{formatSize(asset.size)}</span>
                  {asset.duration && <span>{formatDuration(asset.duration)}</span>}
                  {asset.width && asset.height && (
                    <span>{asset.width}×{asset.height}</span>
                  )}
                </div>
              </div>
              <Button
                variant="ghost"
                size="icon"
                onClick={() => onDelete(asset.id)}
              >
                <Trash2 className="h-4 w-4" />
              </Button>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
