export interface PipelineJob {
  id: string;
  bookId: string;
  status: "pending" | "running" | "completed" | "failed" | "paused";
  progress: number;
  currentStage?: string;
  stages: PipelineStage[];
  createdAt: string;
  updatedAt: string;
  error?: string;
  result?: Record<string, unknown>;
}

export interface PipelineStage {
  name: string;
  status: "pending" | "running" | "completed" | "failed" | "skipped";
  startedAt?: string;
  completedAt?: string;
  error?: string;
  artifacts?: string[];
}

export interface PipelineTriggerRequest {
  bookId: string;
  mode?: "auto" | "manual";
  sourcePath?: string;
  stageFrom?: string | null;
  stageTo?: string | null;
}

export interface PipelineTriggerResponse {
  sessionId: string;
  status: string;
  message: string;
}

export interface PipelineResumeResponse {
  sessionId: string;
  stage: string;
  decision: "approved" | "rejected";
  status: string;
}

export interface MediaAsset {
  id: string;
  type: "video" | "image" | "audio";
  name: string;
  path: string;
  thumbnailUrl?: string;
  duration?: number; // seconds, for video/audio
  width?: number;
  height?: number;
  size: number; // bytes
  createdAt: string;
  metadata?: Record<string, unknown>;
}

export interface MediaFolder {
  id: string;
  name: string;
  parentId?: string;
  assetCount: number;
  createdAt: string;
}

export interface MediaLibrary {
  folders: MediaFolder[];
  assets: MediaAsset[];
}
