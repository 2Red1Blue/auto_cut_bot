"use client";

import { Suspense, lazy, memo, Component, type ReactNode } from "react";
import { cn } from "@/lib/utils";

interface MarkdownTextProps {
  children: string;
  className?: string;
  streaming?: boolean;
}

const loadMarkdownRenderer = () => import("./markdown-renderer");
const LazyRenderer = lazy(loadMarkdownRenderer);

const MemoizedRenderer = memo(function MemoizedRenderer({
  source,
  className,
  streaming,
}: {
  source: string;
  className?: string;
  streaming: boolean;
}) {
  return (
    <LazyRenderer className={className} streaming={streaming}>
      {source}
    </LazyRenderer>
  );
});

class ErrorBoundary extends Component<
  { children: ReactNode; fallback: ReactNode; resetKey: string },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidUpdate(prev: Readonly<{ resetKey: string }>) {
    if (this.state.failed && prev.resetKey !== this.props.resetKey) {
      this.setState({ failed: false });
    }
  }

  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}

export function MarkdownText({
  children,
  className,
  streaming = false,
}: MarkdownTextProps) {
  const phase = streaming ? "streaming" : "complete";

  const plainFallback = (
    <div
      className={cn(
        "whitespace-pre-wrap break-words leading-relaxed text-foreground/92",
        className
      )}
    >
      {children}
    </div>
  );

  return (
    <ErrorBoundary resetKey={phase} fallback={plainFallback}>
      <Suspense fallback={plainFallback}>
        <MemoizedRenderer
          source={children}
          className={className}
          streaming={streaming}
        />
      </Suspense>
    </ErrorBoundary>
  );
}