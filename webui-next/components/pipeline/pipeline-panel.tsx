"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Loader2, Play, Square, RefreshCw } from "lucide-react";
import { apiClient } from "@/lib/api-client";
import type { PipelineJob, PipelineTriggerRequest } from "@/lib/types/pipeline";
import useSWR from "swr";

export function PipelinePanel() {
  const [bookId, setBookId] = useState("");
  const [mode, setMode] = useState<"auto" | "manual">("auto");
  const [sourcePath, setSourcePath] = useState("");
  const [triggering, setTriggering] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: jobs, error: fetchError, mutate } = useSWR("pipeline-jobs", () => apiClient.getPipelineJobs(), {
    refreshInterval: 5000,
  });

  const handleTrigger = async () => {
    if (!bookId.trim()) {
      setError("Book ID is required");
      return;
    }

    setTriggering(true);
    setError(null);

    try {
      const request: PipelineTriggerRequest = {
        bookId: bookId.trim(),
        mode,
        sourcePath: sourcePath.trim() || undefined,
      };

      await apiClient.triggerPipeline(request);
      await mutate();
      setBookId("");
      setSourcePath("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to trigger pipeline");
    } finally {
      setTriggering(false);
    }
  };

  const handleCancel = async (jobId: string) => {
    try {
      await apiClient.cancelPipelineJob(jobId);
      await mutate();
    } catch (err) {
      console.error("Failed to cancel job:", err);
    }
  };

  const getStatusColor = (status: string) => {
    switch (status) {
      case "completed":
        return "bg-green-500";
      case "running":
        return "bg-blue-500";
      case "failed":
        return "bg-red-500";
      case "paused":
        return "bg-yellow-500";
      default:
        return "bg-gray-500";
    }
  };

  return (
    <div className="space-y-6">
      {/* Trigger Form */}
      <Card>
        <CardHeader>
          <CardTitle>Trigger Pipeline</CardTitle>
          <CardDescription>Start a new video processing pipeline</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <label className="text-sm font-medium">Book ID</label>
            <Input
              placeholder="e.g., test-001"
              value={bookId}
              onChange={(e) => setBookId(e.target.value)}
            />
          </div>

          <div className="space-y-2">
            <label className="text-sm font-medium">Mode</label>
            <div className="flex gap-2">
              <Button
                variant={mode === "auto" ? "default" : "outline"}
                onClick={() => setMode("auto")}
                size="sm"
              >
                Auto
              </Button>
              <Button
                variant={mode === "manual" ? "default" : "outline"}
                onClick={() => setMode("manual")}
                size="sm"
              >
                Manual
              </Button>
            </div>
          </div>

          <div className="space-y-2">
            <label className="text-sm font-medium">Source Path (Optional)</label>
            <Input
              placeholder="/path/to/video.mp4"
              value={sourcePath}
              onChange={(e) => setSourcePath(e.target.value)}
            />
          </div>

          {error && (
            <div className="text-sm text-red-500">{error}</div>
          )}

          <Button onClick={handleTrigger} disabled={triggering} className="w-full">
            {triggering ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Triggering...
              </>
            ) : (
              <>
                <Play className="mr-2 h-4 w-4" />
                Trigger Pipeline
              </>
            )}
          </Button>
        </CardContent>
      </Card>

      {/* Jobs List */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>Pipeline Jobs</CardTitle>
            <CardDescription>Recent pipeline execution history</CardDescription>
          </div>
          <Button variant="outline" size="sm" onClick={() => mutate()}>
            <RefreshCw className="h-4 w-4" />
          </Button>
        </CardHeader>
        <CardContent>
          {fetchError ? (
            <div className="text-sm text-red-500">Failed to load jobs</div>
          ) : !jobs || jobs.length === 0 ? (
            <div className="text-sm text-muted-foreground text-center py-8">
              No pipeline jobs yet
            </div>
          ) : (
            <ScrollArea className="h-[400px]">
              <div className="space-y-3">
                {jobs.map((job) => (
                  <Card key={job.id}>
                    <CardContent className="pt-4">
                      <div className="space-y-3">
                        <div className="flex items-center justify-between">
                          <div className="space-y-1">
                            <div className="font-medium">{job.bookId}</div>
                            <div className="text-xs text-muted-foreground">
                              {new Date(job.createdAt).toLocaleString()}
                            </div>
                          </div>
                          <div className="flex items-center gap-2">
                            <Badge className={getStatusColor(job.status)}>
                              {job.status}
                            </Badge>
                            {job.status === "running" && (
                              <Button
                                variant="outline"
                                size="sm"
                                onClick={() => handleCancel(job.id)}
                              >
                                <Square className="h-3 w-3" />
                              </Button>
                            )}
                          </div>
                        </div>

                        {job.currentStage && (
                          <div className="text-xs text-muted-foreground">
                            Current: {job.currentStage}
                          </div>
                        )}

                        <Progress value={job.progress} className="h-2" />

                        {job.error && (
                          <div className="text-xs text-red-500 bg-red-50 p-2 rounded">
                            {job.error}
                          </div>
                        )}
                      </div>
                    </CardContent>
                  </Card>
                ))}
              </div>
            </ScrollArea>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
