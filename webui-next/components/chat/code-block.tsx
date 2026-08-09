"use client";

import { Suspense, lazy, useState, useCallback } from "react";
import { Check, Copy } from "lucide-react";
import { cn } from "@/lib/utils";

interface CodeBlockProps {
  language?: string;
  code: string;
  className?: string;
  chrome?: "default" | "none";
  highlight?: boolean;
  showLineNumbers?: boolean;
  wrapLongLines?: boolean;
}

const CODE_FONT = [
  '"JetBrains Mono"', '"SFMono-Regular"', '"SF Mono"',
  '"Fira Code"', '"Cascadia Code"', '"Source Code Pro"',
  "Menlo", "Consolas", "monospace",
].join(", ");

function PlainCodeBlock({
  code,
  chrome,
  showLineNumbers,
}: {
  code: string;
  chrome: "default" | "none";
  showLineNumbers: boolean;
}) {
  const lines = code.split("\n");

  return (
    <pre
      className={cn(
        "m-0 overflow-x-auto bg-transparent font-mono text-[13px] text-foreground/90",
        showLineNumbers ? "whitespace-pre" : "whitespace-pre-wrap",
        chrome === "default"
          ? "py-4 pl-5 pr-14 leading-[1.6]"
          : "p-3 leading-[1.55]"
      )}
    >
      <code className="text-inherit">
        {showLineNumbers
          ? lines.map((line, i) => (
              <span key={i} className="flex min-w-max">
                <span className="w-10 shrink-0 select-none pr-4 text-right text-muted-foreground/60">
                  {i + 1}
                </span>
                <span className="whitespace-pre">{line || " "}</span>
                {i < lines.length - 1 ? "\n" : null}
              </span>
            ))
          : code}
      </code>
    </pre>
  );
}

const LazyHighlightedCode = lazy(async () => {
  const [SyntaxHighlighter, oneDark, oneLight] = await Promise.all([
    import("react-syntax-highlighter/dist/esm/prism-async-light"),
    import("react-syntax-highlighter/dist/esm/styles/prism/one-dark"),
    import("react-syntax-highlighter/dist/esm/styles/prism/one-light"),
  ]);

  return {
    default({
      language,
      code,
      isDark,
      chrome,
      showLineNumbers,
      wrapLongLines,
    }: {
      language: string;
      code: string;
      isDark: boolean;
      chrome: "default" | "none";
      showLineNumbers: boolean;
      wrapLongLines: boolean;
    }) {
      const theme = isDark ? oneDark.default : oneLight.default;
      const transparentTheme = chrome === "none" ? {
        ...theme,
        'pre[class*="language-"]': {
          ...theme['pre[class*="language-"]'],
          background: "transparent",
        },
        'code[class*="language-"]': {
          ...theme['code[class*="language-"]'],
          background: "transparent",
        },
      } : theme;

      return (
        <SyntaxHighlighter.default
          language={language || "text"}
          style={transparentTheme}
          customStyle={{
            background: "transparent",
            margin: 0,
            padding: chrome === "none" ? "0.75rem 1rem" : "1rem 3.5rem 1rem 1.25rem",
            fontFamily: CODE_FONT,
            fontSize: "13px",
            lineHeight: chrome === "none" ? 1.55 : 1.6,
            tabSize: 2,
          }}
          codeTagProps={{ style: { background: "transparent", fontFamily: CODE_FONT } }}
          showLineNumbers={showLineNumbers}
          wrapLongLines={wrapLongLines}
        >
          {code}
        </SyntaxHighlighter.default>
      );
    },
  };
});

export function CodeBlock({
  language,
  code,
  className,
  chrome = "default",
  highlight = true,
  showLineNumbers = false,
  wrapLongLines = true,
}: CodeBlockProps) {
  const [copied, setCopied] = useState(false);
  const hasChrome = chrome === "default";

  const onCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Fallback
    }
  }, [code]);

  return (
    <div
      className={cn(
        "not-prose relative overflow-hidden",
        hasChrome && "rounded-[18px] bg-secondary/70",
        className
      )}
    >
      {highlight ? (
        <Suspense
          fallback={
            <PlainCodeBlock
              code={code}
              chrome={chrome}
              showLineNumbers={showLineNumbers}
            />
          }
        >
          <LazyHighlightedCode
            language={language || "text"}
            code={code}
            isDark={false}
            chrome={chrome}
            showLineNumbers={showLineNumbers}
            wrapLongLines={wrapLongLines}
          />
        </Suspense>
      ) : (
        <PlainCodeBlock
          code={code}
          chrome={chrome}
          showLineNumbers={showLineNumbers}
        />
      )}
      {hasChrome && (
        <button
          type="button"
          onClick={onCopy}
          className={cn(
            "absolute right-2.5 top-2.5 z-10 inline-flex h-8 w-8 items-center justify-center rounded-full",
            "text-muted-foreground/75 transition-colors hover:bg-background/70 hover:text-foreground",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          )}
          aria-label={copied ? "Copied" : "Copy code"}
          title={copied ? "Copied" : "Copy code"}
        >
          {copied ? (
            <Check className="h-4 w-4" aria-hidden />
          ) : (
            <Copy className="h-4 w-4" aria-hidden />
          )}
        </button>
      )}
    </div>
  );
}