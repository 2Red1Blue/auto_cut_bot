"use client";

import { useState, useRef, useCallback } from "react";
import { Button } from "@/components/ui/button";
import { Mic, MicOff, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

interface VoiceRecorderProps {
  onRecordingComplete: (blob: Blob) => void;
  disabled?: boolean;
  className?: string;
}

export function VoiceRecorder({
  onRecordingComplete,
  disabled,
  className,
}: VoiceRecorderProps) {
  const [recording, setRecording] = useState(false);
  const [permissionDenied, setPermissionDenied] = useState(false);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);

  const startRecording = useCallback(async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const recorder = new MediaRecorder(stream, {
        mimeType: MediaRecorder.isTypeSupported("audio/webm")
          ? "audio/webm"
          : "audio/mp4",
      });

      chunksRef.current = [];
      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };

      recorder.onstop = () => {
        const blob = new Blob(chunksRef.current, {
          type: recorder.mimeType,
        });
        stream.getTracks().forEach((t) => t.stop());
        onRecordingComplete(blob);
      };

      mediaRecorderRef.current = recorder;
      recorder.start();
      setRecording(true);
      setPermissionDenied(false);
    } catch (err) {
      console.error("Microphone access denied:", err);
      setPermissionDenied(true);
    }
  }, [onRecordingComplete]);

  const stopRecording = useCallback(() => {
    mediaRecorderRef.current?.stop();
    setRecording(false);
  }, []);

  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={recording ? stopRecording : startRecording}
      disabled={disabled || permissionDenied}
      className={cn(
        recording && "text-red-500 animate-pulse",
        className
      )}
      title={
        permissionDenied
          ? "Microphone access denied"
          : recording
          ? "Stop recording"
          : "Start recording"
      }
    >
      {recording ? (
        <MicOff className="h-4 w-4" />
      ) : (
        <Mic className="h-4 w-4" />
      )}
    </Button>
  );
}