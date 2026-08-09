"use client";

import { useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import remarkBreaks from "remark-breaks";
import rehypeKatex from "rehype-katex";
import { cn } from "@/lib/utils";
import { CodeBlock } from "./code-block";

interface MarkdownRendererProps {
  children: string;
  className?: string;
  streaming?: boolean;
}

export default function MarkdownRenderer({
  children,
  className,
  streaming = false,
}: MarkdownRendererProps) {
  const components = useMemo(
    () => ({
      code({ node, inline, className: codeClassName, children: codeChildren, ...props }: any) {
        const match = /language-(\w+)/.exec(codeClassName || "");
        const code = String(codeChildren).replace(/\n$/, "");

        if (!inline && (match || code.includes("\n"))) {
          return (
            <CodeBlock
              language={match?.[1]}
              code={code}
              highlight={!streaming}
            />
          );
        }

        return (
          <code className={cn("rounded bg-muted px-1.5 py-0.5 font-mono text-[0.9em]", codeClassName)} {...props}>
            {codeChildren}
          </code>
        );
      },
      pre({ children }: any) {
        return <>{children}</>;
      },
      a({ href, children: linkChildren, ...props }: any) {
        return (
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="text-primary underline underline-offset-2"
            {...props}
          >
            {linkChildren}
          </a>
        );
      },
      table({ children: tableChildren }: any) {
        return (
          <div className="overflow-x-auto my-4">
            <table className="min-w-full border-collapse border border-border text-sm">
              {tableChildren}
            </table>
          </div>
        );
      },
      th({ children: thChildren }: any) {
        return (
          <th className="border border-border bg-muted px-3 py-2 text-left font-semibold">
            {thChildren}
          </th>
        );
      },
      td({ children: tdChildren }: any) {
        return (
          <td className="border border-border px-3 py-2">{tdChildren}</td>
        );
      },
    }),
    [streaming]
  );

  return (
    <div className={cn("prose prose-neutral dark:prose-invert max-w-none", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath, remarkBreaks]}
        rehypePlugins={[rehypeKatex]}
        components={components}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}