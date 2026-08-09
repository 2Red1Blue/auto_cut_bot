"use client";

import dynamic from "next/dynamic";
import { Suspense } from "react";

const ChatContainer = dynamic(
  () =>
    import("@/components/chat/chat-container").then((mod) => ({
      default: mod.ChatContainer,
    })),
  {
    loading: () => (
      <div className="flex h-screen items-center justify-center">
        <div className="text-muted-foreground">Loading...</div>
      </div>
    ),
    ssr: false,
  }
);

export default function Home() {
  return (
    <Suspense
      fallback={
        <div className="flex h-screen items-center justify-center">
          <div className="text-muted-foreground">Loading...</div>
        </div>
      }
    >
      <ChatContainer />
    </Suspense>
  );
}