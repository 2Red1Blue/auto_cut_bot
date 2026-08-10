"use client";

import { useEffect, useRef, useCallback, useMemo } from "react";
import { useClient } from "@/providers/client-provider";
import { useMessageStore, type Message } from "@/lib/stores/message-store";
import type {
  InboundEvent,
} from "@/lib/types";

interface ChatStreamOptions {
  onError?: (error: Error) => void;
}

// Cached empty array to avoid new reference on every render (prevents infinite loop)
const EMPTY_MESSAGES: Message[] = [];

/**
 * Bridges NanobotClient chat events to Zustand message-store.
 * Handles real-time streaming, tool events, and message lifecycle.
 */
export function useChatStream(sessionId: string | null, options: ChatStreamOptions = {}) {
  const { client, status } = useClient();
  const addMessage = useMessageStore((s) => s.addMessage);
  const updateMessage = useMessageStore((s) => s.updateMessage);
  const messages = useMessageStore(
    (s) => (sessionId ? s.messagesBySession[sessionId] : undefined) ?? EMPTY_MESSAGES
  );
  const bufferRef = useRef<{
    assistantId: string | null;
    content: string;
    reasoning: string;
  }>({ assistantId: null, content: "", reasoning: "" });

  // Subscribe to chat events from NanobotClient
  useEffect(() => {
    if (!client || !sessionId || status !== "ready") return;

    // Attach to the chat session
    client.attach(sessionId);

    const unsub = client.onChat(sessionId, (event: InboundEvent) => {
      processInboundEvent(event, sessionId, bufferRef, addMessage, updateMessage);
    });

    return () => {
      unsub();
      // Flush any buffered content
      flushBuffer(sessionId, bufferRef, updateMessage);
    };
  }, [client, sessionId, status, addMessage, updateMessage]);

  // Send a message via NanobotClient
  const sendMessage = useCallback(
    async (content: string) => {
      if (!client || !sessionId) return;

      const userMsgId = `user-${Date.now()}`;
      const assistantMsgId = `assistant-${Date.now()}`;

      // Optimistic user message
      addMessage(sessionId, {
        id: userMsgId,
        role: "user",
        content,
        createdAt: new Date().toISOString(),
      });

      // Optimistic assistant placeholder
      addMessage(sessionId, {
        id: assistantMsgId,
        role: "assistant",
        content: "",
        createdAt: new Date().toISOString(),
        streaming: true,
      });

      bufferRef.current = { assistantId: assistantMsgId, content: "", reasoning: "" };

      try {
        client.sendMessage(sessionId, content, undefined, {
          turnId: userMsgId,
        });
      } catch (err) {
        updateMessage(sessionId, assistantMsgId, {
          content: "Error sending message. Please try again.",
          streaming: false,
        });
        options.onError?.(err instanceof Error ? err : new Error(String(err)));
      }
    },
    [client, sessionId, addMessage, updateMessage, options]
  );

  return {
    messages,
    sendMessage,
    isStreaming: messages.some((m) => m.streaming),
    isReady: status === "ready" && client !== null,
  };
}

// ── Event Processing ──

function processInboundEvent(
  event: InboundEvent,
  sessionId: string,
  buffer: React.MutableRefObject<{
    assistantId: string | null;
    content: string;
    reasoning: string;
  }>,
  addMessage: (sid: string, msg: Message) => void,
  updateMessage: (sid: string, mid: string, updates: Partial<Message>) => void
) {
  const kind = (event as any).event || (event as any).type || "";

  switch (kind) {
    case "delta": {
      const text = (event as any).text || "";
      buffer.current.content += text;
      if (buffer.current.assistantId) {
        updateMessage(sessionId, buffer.current.assistantId, {
          content: buffer.current.content,
        });
      }
      break;
    }

    case "reasoning_delta": {
      const text = (event as any).text || "";
      buffer.current.reasoning += text;
      if (buffer.current.assistantId) {
        updateMessage(sessionId, buffer.current.assistantId, {
          content: `[Thinking] ${buffer.current.reasoning}\n\n${buffer.current.content}`,
        });
      }
      break;
    }

    case "reasoning_end": {
      // Reasoning complete, finalize
      break;
    }

    case "turn_end": {
      // Finalize the assistant message
      if (buffer.current.assistantId) {
        updateMessage(sessionId, buffer.current.assistantId, {
          streaming: false,
        });
      }
      flushBuffer(sessionId, buffer, updateMessage);
      break;
    }

    case "tool_call": {
      const toolName = (event as any).tool_name || (event as any).name || "unknown";
      buffer.current.content += `\n\n🔧 Using tool: **${toolName}**\n`;
      if (buffer.current.assistantId) {
        updateMessage(sessionId, buffer.current.assistantId, {
          content: buffer.current.content,
        });
      }
      break;
    }

    case "tool_result": {
      const result = (event as any).result || (event as any).content || "";
      if (result && typeof result === "string" && result.length < 500) {
        buffer.current.content += `\n📋 Result: ${result}\n`;
      }
      if (buffer.current.assistantId) {
        updateMessage(sessionId, buffer.current.assistantId, {
          content: buffer.current.content,
        });
      }
      break;
    }

    case "error": {
      const errorMsg = (event as any).message || "Unknown error";
      if (buffer.current.assistantId) {
        updateMessage(sessionId, buffer.current.assistantId, {
          content: buffer.current.content + `\n\n❌ Error: ${errorMsg}`,
          streaming: false,
        });
      }
      flushBuffer(sessionId, buffer, updateMessage);
      break;
    }

    case "message": {
      // Full message event (non-streaming)
      const msg = event as any;
      addMessage(sessionId, {
        id: msg.id || `msg-${Date.now()}`,
        role: msg.role || "assistant",
        content: msg.content || "",
        createdAt: msg.createdAt || new Date().toISOString(),
      });
      break;
    }

    default:
      // Unknown event — log for debugging
      if (process.env.NODE_ENV !== "production") {
        console.debug("[ChatStream] Unknown event:", kind, event);
      }
  }
}

function flushBuffer(
  sessionId: string,
  buffer: React.MutableRefObject<{
    assistantId: string | null;
    content: string;
    reasoning: string;
  }>,
  updateMessage: (sid: string, mid: string, updates: Partial<Message>) => void
) {
  if (buffer.current.assistantId) {
    updateMessage(sessionId, buffer.current.assistantId, {
      streaming: false,
    });
  }
  buffer.current = { assistantId: null, content: "", reasoning: "" };
}