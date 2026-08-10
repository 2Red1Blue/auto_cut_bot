"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const MAX_RECORDING_MS = 5 * 60 * 1000; // 5 minutes
const WAVEFORM_BAR_COUNT = 32;
const WAVEFORM_SILENT_HEIGHT = 2;
const WAVEFORM_MIN_HEIGHT = 4;
const WAVEFORM_MAX_HEIGHT = 28;
const MIN_VOLUME_LEVEL = 0.01;

const IDLE_WAVEFORM = Array.from(
  { length: WAVEFORM_BAR_COUNT },
  () => WAVEFORM_SILENT_HEIGHT
);

/** Ordered list of preferred MIME types for MediaRecorder. */
const AUDIO_MIME_CANDIDATES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/mp4",
  "audio/ogg;codecs=opus",
] as const;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type VoiceRecorderState = "idle" | "recording" | "stopping";

export interface UseVoiceRecorderOptions {
  /** Called when a recording is complete with the audio blob. */
  onRecordingComplete?: (blob: Blob) => void;
  /** Called when recording time exceeds the max duration. */
  onMaxDurationReached?: () => void;
  /** Called when the user denies microphone permission. */
  onPermissionDenied?: () => void;
  /** Called when an error occurs during recording. */
  onError?: (error: Error) => void;
}

export interface UseVoiceRecorderReturn {
  /** Current state of the recorder. */
  state: VoiceRecorderState;
  /** Whether recording is currently active. */
  isRecording: boolean;
  /** Elapsed recording time in milliseconds. */
  elapsedMs: number;
  /** Formatted elapsed time string (M:SS). */
  elapsedLabel: string;
  /** Waveform bar heights for visualization. */
  waveform: number[];
  /** Start recording. */
  startRecording: () => Promise<void>;
  /** Stop recording and return the audio blob. */
  stopRecording: () => void;
  /** Whether the browser supports audio recording. */
  isSupported: boolean;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

interface AudioPipeline {
  analyser: AnalyserNode;
  context: AudioContext;
  data: Uint8Array;
  frame: number | null;
  source: MediaStreamAudioSourceNode;
}

function mediaRecorderMimeType(
  MediaRecorderCtor: typeof MediaRecorder
): string | undefined {
  return AUDIO_MIME_CANDIDATES.find((type) =>
    MediaRecorderCtor.isTypeSupported?.(type)
  );
}

function getMediaRecorderConstructor(): typeof MediaRecorder | undefined {
  if (typeof window === "undefined") return undefined;
  const w = window as Window & {
    MediaRecorder?: typeof MediaRecorder;
  };
  return w.MediaRecorder;
}

function getAudioContextConstructor(): typeof AudioContext | undefined {
  if (typeof window === "undefined") return undefined;
  return (
    window.AudioContext ??
    (window as unknown as { webkitAudioContext?: typeof AudioContext })
      .webkitAudioContext
  );
}

function computeVolumeLevel(samples: Uint8Array): number {
  if (samples.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i++) {
    const centered = (samples[i] - 128) / 128;
    sum += centered * centered;
  }
  const rms = Math.sqrt(sum / samples.length);
  return Math.min(1, Math.pow(rms * 3.5, 0.7));
}

function waveformHeight(level: number): number {
  if (level < MIN_VOLUME_LEVEL) return WAVEFORM_SILENT_HEIGHT;
  const normalized = Math.min(
    1,
    (level - MIN_VOLUME_LEVEL) / (1 - MIN_VOLUME_LEVEL)
  );
  return Math.round(
    WAVEFORM_MIN_HEIGHT +
      normalized * (WAVEFORM_MAX_HEIGHT - WAVEFORM_MIN_HEIGHT)
  );
}

function formatElapsed(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useVoiceRecorder(
  options: UseVoiceRecorderOptions = {}
): UseVoiceRecorderReturn {
  const { onRecordingComplete, onMaxDurationReached, onPermissionDenied, onError } =
    options;

  const [state, setState] = useState<VoiceRecorderState>("idle");
  const [elapsedMs, setElapsedMs] = useState(0);
  const [waveform, setWaveform] = useState<number[]>(IDLE_WAVEFORM);

  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<BlobPart[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const audioPipelineRef = useRef<AudioPipeline | null>(null);
  const startedAtRef = useRef(0);
  const maxTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const isSupported =
    typeof window !== "undefined" &&
    !!getMediaRecorderConstructor() &&
    !!navigator.mediaDevices?.getUserMedia;

  // -----------------------------------------------------------------------
  // Audio visualization
  // -----------------------------------------------------------------------

  const stopWaveform = useCallback(() => {
    const pipeline = audioPipelineRef.current;
    audioPipelineRef.current = null;
    if (!pipeline) return;
    if (pipeline.frame !== null) cancelAnimationFrame(pipeline.frame);
    pipeline.source.disconnect();
    pipeline.analyser.disconnect();
    void pipeline.context.close().catch(() => {});
  }, []);

  const startWaveform = useCallback(
    (stream: MediaStream) => {
      const AudioCtx = getAudioContextConstructor();
      if (!AudioCtx) return;

      stopWaveform();
      setWaveform(IDLE_WAVEFORM);

      try {
        const context = new AudioCtx();
        const source = context.createMediaStreamSource(stream);
        const analyser = context.createAnalyser();
        analyser.fftSize = 256;
        analyser.smoothingTimeConstant = 0.65;
        source.connect(analyser);

        const pipeline: AudioPipeline = {
          analyser,
          context,
          data: new Uint8Array(analyser.fftSize),
          frame: null,
          source,
        };

        const tick = () => {
          const current = audioPipelineRef.current;
          if (!current) return;
          if (current.context.state !== "running") {
            void current.context.resume().catch(() => {});
            current.frame = requestAnimationFrame(tick);
            return;
          }
          current.analyser.getByteTimeDomainData(current.data);
          const level = computeVolumeLevel(current.data);
          setWaveform((prev) => [
            ...prev.slice(1),
            waveformHeight(level),
          ]);
          current.frame = requestAnimationFrame(tick);
        };

        audioPipelineRef.current = pipeline;
        void context.resume().catch(() => {});
        pipeline.frame = requestAnimationFrame(tick);
      } catch {
        stopWaveform();
      }
    },
    [stopWaveform]
  );

  // -----------------------------------------------------------------------
  // Cleanup
  // -----------------------------------------------------------------------

  const cleanup = useCallback(() => {
    if (maxTimerRef.current !== null) {
      clearTimeout(maxTimerRef.current);
      maxTimerRef.current = null;
    }
    stopWaveform();
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    mediaRecorderRef.current = null;
    chunksRef.current = [];
  }, [stopWaveform]);

  // -----------------------------------------------------------------------
  // Start / Stop
  // -----------------------------------------------------------------------

  const startRecording = useCallback(async () => {
    if (state !== "idle") return;

    const mediaDevices = navigator.mediaDevices;
    const MediaRecorderCtor = getMediaRecorderConstructor();

    if (!mediaDevices?.getUserMedia || !MediaRecorderCtor) {
      onError?.(new Error("MediaRecorder API not supported"));
      return;
    }

    try {
      const stream = await mediaDevices.getUserMedia({ audio: true });
      const mimeType = mediaRecorderMimeType(MediaRecorderCtor);
      const recorder = new MediaRecorderCtor(stream, {
        ...(mimeType ? { mimeType } : {}),
      });

      chunksRef.current = [];
      streamRef.current = stream;
      mediaRecorderRef.current = recorder;
      startedAtRef.current = Date.now();
      setElapsedMs(0);
      startWaveform(stream);

      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          chunksRef.current.push(event.data);
        }
      };

      recorder.onstop = () => {
        const chunks = chunksRef.current.splice(0);
        const mime = recorder.mimeType || "audio/webm";
        cleanup();

        if (chunks.length === 0) {
          setState("idle");
          return;
        }

        const blob = new Blob(chunks, { type: mime });
        onRecordingComplete?.(blob);
        setState("idle");
      };

      recorder.start();
      setState("recording");

      // Auto-stop after max duration
      maxTimerRef.current = setTimeout(() => {
        onMaxDurationReached?.();
        // Stop through the recorder
        const rec = mediaRecorderRef.current;
        if (rec && rec.state === "recording") {
          rec.stop();
        }
      }, MAX_RECORDING_MS);
    } catch (error) {
      cleanup();
      setState("idle");

      const err = error instanceof Error ? error : new Error(String(error));
      if (err.name === "NotAllowedError" || err.name === "PermissionDeniedError") {
        onPermissionDenied?.();
      } else {
        onError?.(err);
      }
    }
  }, [state, cleanup, startWaveform, onRecordingComplete, onMaxDurationReached, onPermissionDenied, onError]);

  const stopRecording = useCallback(() => {
    const recorder = mediaRecorderRef.current;
    if (recorder && recorder.state === "recording") {
      setState("stopping");
      recorder.stop();
    }
  }, []);

  // -----------------------------------------------------------------------
  // Elapsed time tracking
  // -----------------------------------------------------------------------

  useEffect(() => {
    if (state !== "recording") {
      setElapsedMs(0);
      return;
    }

    const update = () => {
      setElapsedMs(Math.max(0, Date.now() - startedAtRef.current));
    };
    update();
    const interval = setInterval(update, 250);
    return () => clearInterval(interval);
  }, [state]);

  // -----------------------------------------------------------------------
  // Cleanup on unmount
  // -----------------------------------------------------------------------

  useEffect(() => cleanup, [cleanup]);

  return {
    state,
    isRecording: state === "recording",
    elapsedMs,
    elapsedLabel: formatElapsed(elapsedMs),
    waveform,
    startRecording,
    stopRecording,
    isSupported,
  };
}