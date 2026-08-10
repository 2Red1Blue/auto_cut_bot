"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { FolderPlus, Upload, RefreshCw, Grid3x3, List } from "lucide-react";
import { apiClient } from "@/lib/api-client";
import type { MediaAsset, MediaFolder, MediaLibrary } from "@/lib/types/pipeline";
import useSWR from "swr";
import { MediaGrid } from "./media-grid";

export function MediaLibraryPanel() {
  const [currentFolderId, setCurrentFolderId] = useState<string | undefined>();
  const [viewMode, setViewMode] = useState<"grid" | "list">("grid");
  const [showNewFolder, setShowNewFolder] = useState(false);
  const [newFolderName, setNewFolderName] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);

  const { data: library, error, mutate } = useSWR(
    `media-library-${currentFolderId}`,
    () => apiClient.getMediaLibrary(currentFolderId),
    { refreshInterval: 10000 }
  );

  const handleCreateFolder = async () => {
    if (!newFolderName.trim()) return;
    
    try {
      await apiClient.createMediaFolder(newFolderName.trim(), currentFolderId);
      setNewFolderName("");
      setShowNewFolder(false);
      await mutate();
    } catch (err) {
      console.error("Failed to create folder:", err);
    }
  };

  const handleUpload = async (files: FileList | null) => {
    if (!files || files.length === 0) return;

    setUploading(true);
    setUploadProgress(0);

    try {
      for (let i = 0; i < files.length; i++) {
        await apiClient.uploadMediaAsset(files[i], currentFolderId, (progress) => {
          setUploadProgress((i / files.length + progress / files.length) * 100);
        });
      }
      await mutate();
    } catch (err) {
      console.error("Upload failed:", err);
    } finally {
      setUploading(false);
      setUploadProgress(0);
    }
  };

  const handleDeleteAsset = async (assetId: string) => {
    if (!confirm("确定要删除这个素材吗？")) return;

    try {
      await apiClient.deleteMediaAsset(assetId);
      await mutate();
    } catch (err) {
      console.error("Failed to delete asset:", err);
    }
  };

  const getAssetIcon = (type: string) => {
    switch (type) {
      case "video":
        return "🎬";
      case "image":
        return "🖼️";
      case "audio":
        return "🎵";
      default:
        return "📄";
    }
  };

  const formatDuration = (seconds?: number) => {
    if (!seconds) return "";
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, "0")}`;
  };

  const formatSize = (bytes: number) => {
    const mb = bytes / (1024 * 1024);
    if (mb < 1) return `${Math.round(bytes / 1024)} KB`;
    return `${mb.toFixed(1)} MB`;
  };

  return (
    <div className="space-y-4">
      {/* Toolbar */}
      <Card>
        <CardContent className="pt-6">
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setShowNewFolder(!showNewFolder)}
            >
              <FolderPlus className="h-4 w-4 mr-2" />
              新建文件夹
            </Button>

            <div className="relative">
              <input
                type="file"
                multiple
                accept="video/*,image/*,audio/*"
                onChange={(e) => handleUpload(e.target.files)}
                className="absolute inset-0 w-full h-full opacity-0 cursor-pointer"
                disabled={uploading}
              />
              <Button
                variant="outline"
                size="sm"
                disabled={uploading}
              >
                <Upload className="h-4 w-4 mr-2" />
                {uploading ? `上传中 ${Math.round(uploadProgress)}%` : "上传"}
              </Button>
            </div>

            <div className="flex-1" />

            <Button
              variant={viewMode === "grid" ? "default" : "outline"}
              size="sm"
              onClick={() => setViewMode("grid")}
            >
              <Grid3x3 className="h-4 w-4" />
            </Button>
            <Button
              variant={viewMode === "list" ? "default" : "outline"}
              size="sm"
              onClick={() => setViewMode("list")}
            >
              <List className="h-4 w-4" />
            </Button>

            <Button variant="outline" size="sm" onClick={() => mutate()}>
              <RefreshCw className="h-4 w-4" />
            </Button>
          </div>

          {showNewFolder && (
            <div className="mt-4 flex gap-2">
              <Input
                placeholder="文件夹名称"
                value={newFolderName}
                onChange={(e) => setNewFolderName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleCreateFolder()}
              />
              <Button onClick={handleCreateFolder}>创建</Button>
              <Button variant="outline" onClick={() => setShowNewFolder(false)}>
                取消
              </Button>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Breadcrumb */}
      {currentFolderId && (
        <Card>
          <CardContent className="pt-6">
            <div className="flex items-center gap-2 text-sm">
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setCurrentFolderId(undefined)}
              >
                根目录
              </Button>
              <span>/</span>
              <span className="text-muted-foreground">当前文件夹</span>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Error State */}
      {error && (
        <Card>
          <CardContent className="pt-6">
            <div className="space-y-2">
              <div className="text-sm text-red-500 font-medium">加载素材库失败</div>
              <div className="text-xs text-muted-foreground">
                {error instanceof Error ? error.message : String(error)}
              </div>
              <Button variant="outline" size="sm" onClick={() => mutate()}>
                <RefreshCw className="h-3 w-3 mr-1" />
                重试
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Folders */}
      {library?.folders && library.folders.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">文件夹</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
              {library.folders.map((folder) => (
                <Card
                  key={folder.id}
                  className="cursor-pointer hover:bg-accent transition-colors"
                  onClick={() => setCurrentFolderId(folder.id)}
                >
                  <CardContent className="pt-6">
                    <div className="flex items-center gap-3">
                      <div className="text-4xl">📁</div>
                      <div className="flex-1 min-w-0">
                        <div className="font-medium truncate">{folder.name}</div>
                        <div className="text-xs text-muted-foreground">
                          {folder.assetCount} 个素材
                        </div>
                      </div>
                    </div>
                  </CardContent>
                </Card>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Assets */}
      {library?.assets && library.assets.length > 0 ? (
        <MediaGrid
          assets={library.assets}
          viewMode={viewMode}
          onDelete={handleDeleteAsset}
          getIcon={getAssetIcon}
          formatDuration={formatDuration}
          formatSize={formatSize}
        />
      ) : (
        <Card>
          <CardContent className="pt-6">
            <div className="text-center py-12 text-muted-foreground">
              <Upload className="h-12 w-12 mx-auto mb-4 opacity-50" />
              <p>暂无素材</p>
              <p className="text-sm mt-2">上传文件开始使用</p>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
