"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Play, Loader2 } from "lucide-react";
import { apiClient } from "@/lib/api-client";
import type { PipelineTriggerRequest } from "@/lib/types/pipeline";

export function PipelineTrigger() {
  const [bookId, setBookId] = useState("");
  const [mode, setMode] = useState<"auto" | "manual">("auto");
  const [sourcePath, setSourcePath] = useState("");
  const [stageFrom, setStageFrom] = useState<string>("");
  const [stageTo, setStageTo] = useState<string>("");
  const [triggering, setTriggering] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const handleTrigger = async () => {
    if (!bookId.trim()) {
      setError("请输入书籍 ID");
      return;
    }

    setTriggering(true);
    setError(null);
    setSuccess(null);

    try {
      const request: PipelineTriggerRequest = {
        bookId: bookId.trim(),
        mode,
        sourcePath: sourcePath.trim() || undefined,
        stageFrom: stageFrom || null,
        stageTo: stageTo || null,
      };

      const response = await apiClient.triggerPipeline(request);
      setSuccess(`Pipeline 已启动！会话 ID: ${response.sessionId}`);
      
      // Reset form
      setBookId("");
      setSourcePath("");
      setStageFrom("");
      setStageTo("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "触发 Pipeline 失败");
    } finally {
      setTriggering(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>触发 Pipeline</CardTitle>
        <CardDescription>
          启动视频处理流水线，自动完成从素材准备到最终渲染的全流程
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          <Label htmlFor="bookId">书籍 ID *</Label>
          <Input
            id="bookId"
            placeholder="例如: book-001"
            value={bookId}
            onChange={(e) => setBookId(e.target.value)}
          />
        </div>

        <div className="space-y-2">
          <Label htmlFor="mode">执行模式</Label>
          <Select value={mode} onValueChange={(value: "auto" | "manual") => setMode(value)}>
            <SelectTrigger id="mode">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="auto">自动模式（全自动执行）</SelectItem>
              <SelectItem value="manual">手动模式（每阶段确认）</SelectItem>
            </SelectContent>
          </Select>
        </div>

        <div className="space-y-2">
          <Label htmlFor="sourcePath">源视频路径（可选）</Label>
          <Input
            id="sourcePath"
            placeholder="/path/to/video.mp4"
            value={sourcePath}
            onChange={(e) => setSourcePath(e.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            如果不指定，将从媒体库中自动选择
          </p>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div className="space-y-2">
            <Label htmlFor="stageFrom">从阶段开始（可选）</Label>
            <Input
              id="stageFrom"
              placeholder="例如: source_prep"
              value={stageFrom}
              onChange={(e) => setStageFrom(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="stageTo">到阶段结束（可选）</Label>
            <Input
              id="stageTo"
              placeholder="例如: render"
              value={stageTo}
              onChange={(e) => setStageTo(e.target.value)}
            />
          </div>
        </div>

        {error && (
          <div className="p-3 bg-red-50 border border-red-200 rounded-md text-sm text-red-800">
            {error}
          </div>
        )}

        {success && (
          <div className="p-3 bg-green-50 border border-green-200 rounded-md text-sm text-green-800">
            {success}
          </div>
        )}

        <Button
          onClick={handleTrigger}
          disabled={triggering}
          className="w-full"
          size="lg"
        >
          {triggering ? (
            <>
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              正在启动...
            </>
          ) : (
            <>
              <Play className="mr-2 h-4 w-4" />
              启动 Pipeline
            </>
          )}
        </Button>
      </CardContent>
    </Card>
  );
}
