"use client";

import { useState } from "react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { PipelinePanel } from "./pipeline-panel";
import { PipelineTrigger } from "./pipeline-trigger";
import { ConflictsPanel } from "./conflicts-panel";

export function PipelineView() {
  return (
    <div className="container mx-auto p-6 space-y-6">
      <div>
        <h1 className="text-3xl font-bold">Pipeline 管理</h1>
        <p className="text-muted-foreground mt-2">
          管理和监控视频处理流水线
        </p>
      </div>

      <Tabs defaultValue="jobs" className="space-y-4">
        <TabsList>
          <TabsTrigger value="jobs">任务列表</TabsTrigger>
          <TabsTrigger value="trigger">触发新任务</TabsTrigger>
          <TabsTrigger value="conflicts">冲突解决</TabsTrigger>
        </TabsList>

        <TabsContent value="jobs" className="space-y-4">
          <PipelinePanel />
        </TabsContent>

        <TabsContent value="trigger">
          <PipelineTrigger />
        </TabsContent>

        <TabsContent value="conflicts">
          <ConflictsPanel />
        </TabsContent>
      </Tabs>
    </div>
  );
}
