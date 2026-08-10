"use client";

import { useState } from "react";
import { useSettingsStore } from "@/lib/stores/settings-store";
import { useTheme } from "@/components/common/theme-provider";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useUIStore } from "@/lib/stores/ui-store";
import {
  Settings2,
  Palette,
  Layers,
  Info,
  Sun,
  Moon,
  Monitor,
  ExternalLink,
  RotateCcw,
} from "lucide-react";

/* ------------------------------------------------------------------ */
/*  Constants                                                          */
/* ------------------------------------------------------------------ */

const MODEL_OPTIONS = [
  { value: "qwen3-max", label: "Qwen3 Max" },
  { value: "qwen3-flash", label: "Qwen3 Flash" },
  { value: "qwen3-coder", label: "Qwen3 Coder" },
  { value: "gpt-4o", label: "GPT-4o" },
  { value: "gpt-4o-mini", label: "GPT-4o Mini" },
  { value: "claude-3.5-sonnet", label: "Claude 3.5 Sonnet" },
  { value: "deepseek-v3", label: "DeepSeek V3" },
  { value: "doubao-1.5-pro", label: "Doubao 1.5 Pro" },
] as const;

const PROVIDER_OPTIONS = [
  { value: "openai", label: "OpenAI" },
  { value: "anthropic", label: "Anthropic" },
  { value: "qwen", label: "Qwen (DashScope)" },
  { value: "doubao", label: "Doubao (Ark)" },
  { value: "deepseek", label: "DeepSeek" },
  { value: "custom", label: "Custom (OpenAI-compatible)" },
] as const;

const BACKEND_OPTIONS = [
  { value: "qwen", label: "Qwen (DashScope)" },
  { value: "doubao", label: "Doubao (Ark)" },
  { value: "openai", label: "OpenAI" },
  { value: "custom", label: "Custom" },
] as const;

const FONT_SIZE_OPTIONS = [
  { value: "12", label: "12px — Compact" },
  { value: "13", label: "13px — Small" },
  { value: "14", label: "14px — Default" },
  { value: "15", label: "15px — Medium" },
  { value: "16", label: "16px — Large" },
] as const;

const DEFAULT_PIPELINE = {
  windowSeconds: 240,
  overlapSeconds: 12,
  workers: "auto" as "auto" | number,
  rpmLimit: 0,
  backend: "qwen",
};

/* ------------------------------------------------------------------ */
/*  Section Header                                                     */
/* ------------------------------------------------------------------ */

function SectionHeader({
  title,
  description,
}: {
  title: string;
  description?: string;
}) {
  return (
    <div className="mb-4">
      <h3 className="text-sm font-semibold text-foreground">{title}</h3>
      {description && (
        <p className="mt-0.5 text-xs text-muted-foreground">{description}</p>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Field Row                                                          */
/* ------------------------------------------------------------------ */

function FieldRow({
  label,
  htmlFor,
  hint,
  children,
}: {
  label: string;
  htmlFor?: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="grid gap-1.5">
      <div className="flex items-center justify-between">
        <Label htmlFor={htmlFor} className="text-xs font-medium">
          {label}
        </Label>
        {hint && (
          <span className="text-[11px] text-muted-foreground">{hint}</span>
        )}
      </div>
      {children}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Main Component                                                     */
/* ------------------------------------------------------------------ */

export function SettingsDialog() {
  const settingsOpen = useUIStore((s) => s.settingsOpen);
  const setSettingsOpen = useUIStore((s) => s.setSettingsOpen);

  /* ── Theme (from provider; also synced in store) ───────────────── */
  const { theme, setTheme } = useTheme();

  /* ── General ───────────────────────────────────────────────────── */
  const model = useSettingsStore((s) => s.model);
  const provider = useSettingsStore((s) => s.provider);
  const temperature = useSettingsStore((s) => s.temperature);
  const maxTokens = useSettingsStore((s) => s.maxTokens);
  const botName = useSettingsStore((s) => s.botName);
  const setModel = useSettingsStore((s) => s.setModel);
  const setProvider = useSettingsStore((s) => s.setProvider);
  const setTemperature = useSettingsStore((s) => s.setTemperature);
  const setMaxTokens = useSettingsStore((s) => s.setMaxTokens);
  const setBotName = useSettingsStore((s) => s.setBotName);

  /* ── Appearance ────────────────────────────────────────────────── */
  const [fontSize, setFontSize] = useState("14");

  /* ── Pipeline ──────────────────────────────────────────────────── */
  const pipeline = useSettingsStore((s) => s.pipeline);
  const setPipeline = useSettingsStore((s) => s.setPipeline);

  /* ── Reset helpers ─────────────────────────────────────────────── */
  const resetPipeline = () => setPipeline(DEFAULT_PIPELINE);
  const resetGeneral = () => {
    setModel("qwen3-max");
    setProvider("openai");
    setTemperature(0.7);
    setMaxTokens(4096);
    setBotName("AutoCutBot");
  };

  return (
    <Dialog open={settingsOpen} onOpenChange={setSettingsOpen}>
      <DialogContent
        className="
          sm:max-w-xl
          max-h-[85vh]
          overflow-hidden
          flex flex-col
          p-0
          gap-0
        "
      >
        {/* ── Header ──────────────────────────────────────────────── */}
        <DialogHeader className="px-6 pt-6 pb-2 shrink-0">
          <DialogTitle className="flex items-center gap-2 text-base">
            <Settings2 className="h-4 w-4 text-muted-foreground" />
            Settings
          </DialogTitle>
          <DialogDescription className="text-xs">
            Configure AutoCutBot pipeline, appearance, and model preferences.
          </DialogDescription>
        </DialogHeader>

        {/* ── Tabs ────────────────────────────────────────────────── */}
        <Tabs defaultValue="general" className="flex flex-col min-h-0 flex-1">
          <TabsList className="mx-6 mb-0 shrink-0">
            <TabsTrigger value="general" className="gap-1.5 text-xs">
              <Settings2 className="h-3.5 w-3.5" />
              General
            </TabsTrigger>
            <TabsTrigger value="appearance" className="gap-1.5 text-xs">
              <Palette className="h-3.5 w-3.5" />
              Appearance
            </TabsTrigger>
            <TabsTrigger value="pipeline" className="gap-1.5 text-xs">
              <Layers className="h-3.5 w-3.5" />
              Pipeline
            </TabsTrigger>
            <TabsTrigger value="about" className="gap-1.5 text-xs">
              <Info className="h-3.5 w-3.5" />
              About
            </TabsTrigger>
          </TabsList>

          {/* ── Scrollable content area ───────────────────────────── */}
          <div className="flex-1 overflow-y-auto px-6 py-4">
            {/* ======================================================== */}
            {/*  GENERAL                                                */}
            {/* ======================================================== */}
            <TabsContent value="general" className="mt-0 space-y-5">
              <SectionHeader
                title="Model Configuration"
                description="Select the LLM provider and model used for video analysis."
              />

              <FieldRow label="Provider" htmlFor="settings-provider">
                <Select value={provider} onValueChange={setProvider}>
                  <SelectTrigger id="settings-provider" className="h-8 text-xs">
                    <SelectValue placeholder="Select provider" />
                  </SelectTrigger>
                  <SelectContent>
                    {PROVIDER_OPTIONS.map((opt) => (
                      <SelectItem key={opt.value} value={opt.value}>
                        {opt.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FieldRow>

              <FieldRow label="Model" htmlFor="settings-model">
                <Select value={model} onValueChange={setModel}>
                  <SelectTrigger id="settings-model" className="h-8 text-xs">
                    <SelectValue placeholder="Select model" />
                  </SelectTrigger>
                  <SelectContent>
                    {MODEL_OPTIONS.map((opt) => (
                      <SelectItem key={opt.value} value={opt.value}>
                        {opt.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FieldRow>

              <FieldRow
                label="Temperature"
                htmlFor="settings-temperature"
                hint={String(temperature)}
              >
                <Input
                  id="settings-temperature"
                  type="range"
                  min={0}
                  max={2}
                  step={0.05}
                  value={temperature}
                  onChange={(e) => setTemperature(Number(e.target.value))}
                  className="h-6 cursor-pointer px-0 [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:h-3.5 [&::-webkit-slider-thumb]:w-3.5 [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-primary"
                />
              </FieldRow>

              <FieldRow
                label="Max Tokens"
                htmlFor="settings-max-tokens"
                hint={`${maxTokens.toLocaleString()} tokens`}
              >
                <Input
                  id="settings-max-tokens"
                  type="number"
                  min={256}
                  max={131072}
                  step={256}
                  value={maxTokens}
                  onChange={(e) => {
                    const v = parseInt(e.target.value, 10);
                    if (!isNaN(v) && v >= 256) setMaxTokens(v);
                  }}
                  className="h-8 text-xs"
                />
              </FieldRow>

              <FieldRow label="Bot Name" htmlFor="settings-bot-name">
                <Input
                  id="settings-bot-name"
                  value={botName}
                  onChange={(e) => setBotName(e.target.value)}
                  placeholder="AutoCutBot"
                  className="h-8 text-xs"
                />
              </FieldRow>

              <div className="pt-1">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={resetGeneral}
                  className="gap-1.5 text-xs"
                >
                  <RotateCcw className="h-3 w-3" />
                  Reset to defaults
                </Button>
              </div>
            </TabsContent>

            {/* ======================================================== */}
            {/*  APPEARANCE                                             */}
            {/* ======================================================== */}
            <TabsContent value="appearance" className="mt-0 space-y-5">
              <SectionHeader
                title="Theme"
                description="Choose between light, dark, or follow your system preference."
              />

              <div className="flex gap-2">
                <Button
                  variant={theme === "light" ? "default" : "outline"}
                  size="sm"
                  onClick={() => setTheme("light")}
                  className="gap-1.5 flex-1"
                >
                  <Sun className="h-3.5 w-3.5" />
                  Light
                </Button>
                <Button
                  variant={theme === "dark" ? "default" : "outline"}
                  size="sm"
                  onClick={() => setTheme("dark")}
                  className="gap-1.5 flex-1"
                >
                  <Moon className="h-3.5 w-3.5" />
                  Dark
                </Button>
                <Button
                  variant={theme === "system" ? "default" : "outline"}
                  size="sm"
                  onClick={() => setTheme("system")}
                  className="gap-1.5 flex-1"
                >
                  <Monitor className="h-3.5 w-3.5" />
                  System
                </Button>
              </div>

              <SectionHeader
                title="Font Size"
                description="Adjust the base font size for the interface."
              />

              <FieldRow label="Interface Font Size" htmlFor="settings-font-size">
                <Select
                  value={fontSize}
                  onValueChange={(v) => {
                    setFontSize(v);
                    document.documentElement.style.fontSize = `${v}px`;
                    localStorage.setItem("auto_cut_bot.fontSize", v);
                  }}
                >
                  <SelectTrigger id="settings-font-size" className="h-8 text-xs">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {FONT_SIZE_OPTIONS.map((opt) => (
                      <SelectItem key={opt.value} value={opt.value}>
                        {opt.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FieldRow>
            </TabsContent>

            {/* ======================================================== */}
            {/*  PIPELINE                                               */}
            {/* ======================================================== */}
            <TabsContent value="pipeline" className="mt-0 space-y-5">
              <SectionHeader
                title="Video Processing"
                description="Configure how the pipeline slices and analyzes videos."
              />

              <FieldRow
                label="Window Duration (seconds)"
                htmlFor="settings-window"
                hint={`${pipeline.windowSeconds}s per window`}
              >
                <Input
                  id="settings-window"
                  type="number"
                  min={60}
                  max={600}
                  step={10}
                  value={pipeline.windowSeconds}
                  onChange={(e) => {
                    const v = parseInt(e.target.value, 10);
                    if (!isNaN(v) && v >= 60) setPipeline({ windowSeconds: v });
                  }}
                  className="h-8 text-xs"
                />
              </FieldRow>

              <FieldRow
                label="Overlap Duration (seconds)"
                htmlFor="settings-overlap"
                hint={`${pipeline.overlapSeconds}s overlap`}
              >
                <Input
                  id="settings-overlap"
                  type="number"
                  min={0}
                  max={120}
                  step={1}
                  value={pipeline.overlapSeconds}
                  onChange={(e) => {
                    const v = parseInt(e.target.value, 10);
                    if (!isNaN(v) && v >= 0) setPipeline({ overlapSeconds: v });
                  }}
                  className="h-8 text-xs"
                />
              </FieldRow>

              <SectionHeader
                title="Concurrency"
                description="Control parallelism and rate limits."
              />

              <FieldRow
                label="Workers"
                htmlFor="settings-workers"
                hint={
                  pipeline.workers === "auto"
                    ? "auto (detected from CPU)"
                    : `${pipeline.workers} workers`
                }
              >
                <div className="flex gap-2">
                  <Input
                    id="settings-workers"
                    type="number"
                    min={1}
                    max={32}
                    step={1}
                    disabled={pipeline.workers === "auto"}
                    value={
                      pipeline.workers === "auto"
                        ? ""
                        : pipeline.workers
                    }
                    placeholder="e.g. 4"
                    onChange={(e) => {
                      const v = parseInt(e.target.value, 10);
                      if (!isNaN(v) && v >= 1) setPipeline({ workers: v });
                      else if (e.target.value === "")
                        setPipeline({ workers: "auto" });
                    }}
                    className="h-8 flex-1 text-xs"
                  />
                  <Button
                    variant={
                      pipeline.workers === "auto" ? "default" : "outline"
                    }
                    size="sm"
                    onClick={() => setPipeline({ workers: "auto" })}
                    className="text-xs shrink-0"
                  >
                    Auto
                  </Button>
                </div>
              </FieldRow>

              <FieldRow
                label="RPM Limit"
                htmlFor="settings-rpm"
                hint={
                  pipeline.rpmLimit <= 0
                    ? "No limit"
                    : `${pipeline.rpmLimit} req/min`
                }
              >
                <Input
                  id="settings-rpm"
                  type="number"
                  min={0}
                  max={10000}
                  step={50}
                  value={pipeline.rpmLimit}
                  onChange={(e) => {
                    const v = parseInt(e.target.value, 10);
                    if (!isNaN(v) && v >= 0) setPipeline({ rpmLimit: v });
                  }}
                  className="h-8 text-xs"
                />
              </FieldRow>

              <SectionHeader
                title="Backend"
                description="Select the AI backend used for video analysis."
              />

              <FieldRow label="Backend" htmlFor="settings-backend">
                <Select
                  value={pipeline.backend}
                  onValueChange={(v) => setPipeline({ backend: v })}
                >
                  <SelectTrigger id="settings-backend" className="h-8 text-xs">
                    <SelectValue placeholder="Select backend" />
                  </SelectTrigger>
                  <SelectContent>
                    {BACKEND_OPTIONS.map((opt) => (
                      <SelectItem key={opt.value} value={opt.value}>
                        {opt.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FieldRow>

              <div className="pt-1">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={resetPipeline}
                  className="gap-1.5 text-xs"
                >
                  <RotateCcw className="h-3 w-3" />
                  Reset pipeline defaults
                </Button>
              </div>
            </TabsContent>

            {/* ======================================================== */}
            {/*  ABOUT                                                  */}
            {/* ======================================================== */}
            <TabsContent value="about" className="mt-0 space-y-5">
              <SectionHeader
                title="AutoCutBot"
                description="AI-powered video editing pipeline."
              />

              <div className="rounded-lg border bg-card p-4 space-y-3">
                <div className="flex items-center justify-between text-xs">
                  <span className="text-muted-foreground">Version</span>
                  <span className="font-mono font-medium text-foreground">
                    0.1.0-alpha
                  </span>
                </div>

                <div className="flex items-center justify-between text-xs">
                  <span className="text-muted-foreground">Pipeline</span>
                  <span className="font-mono font-medium text-foreground">
                    auto-cut-bot
                  </span>
                </div>

                <div className="flex items-center justify-between text-xs">
                  <span className="text-muted-foreground">Backend</span>
                  <span className="font-mono font-medium text-foreground">
                    {pipeline.backend} / {model}
                  </span>
                </div>

                <div className="flex items-center justify-between text-xs">
                  <span className="text-muted-foreground">Build</span>
                  <span className="font-mono font-medium text-foreground">
                    Next.js + shadcn/ui
                  </span>
                </div>
              </div>

              <SectionHeader
                title="Links"
                description="Resources and documentation."
              />

              <div className="space-y-1.5">
                <a
                  href="https://github.com"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center justify-between rounded-md px-3 py-2 text-xs transition-colors hover:bg-muted"
                >
                  <span className="font-medium">GitHub Repository</span>
                  <ExternalLink className="h-3 w-3 text-muted-foreground" />
                </a>

                <a
                  href="https://github.com"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center justify-between rounded-md px-3 py-2 text-xs transition-colors hover:bg-muted"
                >
                  <span className="font-medium">Documentation</span>
                  <ExternalLink className="h-3 w-3 text-muted-foreground" />
                </a>

                <a
                  href="https://github.com"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center justify-between rounded-md px-3 py-2 text-xs transition-colors hover:bg-muted"
                >
                  <span className="font-medium">Report an Issue</span>
                  <ExternalLink className="h-3 w-3 text-muted-foreground" />
                </a>
              </div>
            </TabsContent>
          </div>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}