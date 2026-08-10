"use client";

import { useMemo, useCallback, useState, type ReactNode } from "react";
import { Copy, Check, ExternalLink } from "lucide-react";
import rehypeKatex from "rehype-katex";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import { Streamdown, type Components } from "streamdown";
import { cn } from "@/lib/utils";
import { CodeBlock } from "./code-block";

import "katex/dist/katex.min.css";
import "streamdown/styles.css";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface MarkdownRendererProps {
  children: string;
  className?: string;
  streaming?: boolean;
}

// ---------------------------------------------------------------------------
// Inline code with copy-to-clipboard button
// ---------------------------------------------------------------------------

function InlineCode({ code }: { code: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard unavailable — silently ignore
    }
  }, [code]);

  return (
    <span className="not-prose group relative inline-flex items-center">
      <code className="rounded-md bg-muted px-1.5 py-0.5 font-mono text-[0.85em] leading-normal text-foreground/85">
        {code}
      </code>
      {code.length > 0 && (
        <button
          type="button"
          onClick={handleCopy}
          className={cn(
            "ml-1 inline-flex h-5 w-5 items-center justify-center rounded-sm opacity-0 transition-opacity",
            "text-muted-foreground hover:bg-muted hover:text-foreground",
            "group-hover:opacity-100 focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60",
          )}
          aria-label={copied ? "Copied" : "Copy inline code"}
          title={copied ? "Copied" : "Copy"}
        >
          {copied ? (
            <Check className="h-3 w-3" aria-hidden />
          ) : (
            <Copy className="h-3 w-3" aria-hidden />
          )}
        </button>
      )}
    </span>
  );
}

// ---------------------------------------------------------------------------
// URL safety helpers
// ---------------------------------------------------------------------------

const SAFE_PROTOCOL = /^(https?|ircs?|mailto|xmpp)$/i;

function safeUrl(url: string): string {
  const colon = url.indexOf(":");
  const slash = url.indexOf("/");
  const question = url.indexOf("?");
  const hash = url.indexOf("#");
  const relative =
    colon === -1 ||
    (slash !== -1 && colon > slash) ||
    (question !== -1 && colon > question) ||
    (hash !== -1 && colon > hash);
  return relative || SAFE_PROTOCOL.test(url.slice(0, colon)) ? url : "";
}

// ---------------------------------------------------------------------------
// Markdown renderer
// ---------------------------------------------------------------------------

export default function MarkdownRenderer({
  children,
  className,
  streaming = false,
}: MarkdownRendererProps) {
  const components = useMemo<Components>(
    () => ({
      // ── Code blocks ──────────────────────────────────────────────
      code({ className: codeClassName, children: codeChildren, node: _node, ...props }: any) {
        void _node;
        const match = /language-(\w+)/.exec(codeClassName || "");
        const raw = String(codeChildren).replace(/\n$/, "");

        // Fenced code block with language
        if (match) {
          return (
            <CodeBlock
              language={match[1]}
              code={raw}
              className="my-3"
              highlight={!streaming}
              showLineNumbers={raw.includes("\n")}
            />
          );
        }

        // Multi-line code without language or very long one-liner
        const widePlainBlock = raw.includes("\n") || raw.length > 120;
        if (widePlainBlock) {
          return (
            <code
              className={cn(
                "block min-w-0 max-w-full overflow-x-auto whitespace-pre bg-transparent p-0 font-mono text-[0.8125rem] leading-snug text-inherit",
                codeClassName,
              )}
              {...props}
            >
              {codeChildren}
            </code>
          );
        }

        // Inline code with copy button
        return <InlineCode code={raw} />;
      },

      // ── Pre blocks ───────────────────────────────────────────────
      pre({ children: preChildren }: any) {
        // Streamdown renders highlighted code blocks as <pre>
        // with a single child; pass through to avoid double-wrapping.
        return <>{preChildren}</>;
      },

      // ── Links ────────────────────────────────────────────────────
      a({ href, children: linkChildren, node: _node, ...props }: any) {
        void _node;
        if (!href) {
          return <>{linkChildren}</>;
        }
        // Streaming partial link
        if (href === "streamdown:incomplete-link") {
          return <>{linkChildren}</>;
        }
        // Skip empty anchor links
        if (href === "#") {
          return <>{linkChildren}</>;
        }

        const resolved = safeUrl(href);
        if (!resolved) {
          return <>{linkChildren}</>;
        }

        const isExternal = /^https?:\/\//i.test(resolved);

        return (
          <a
            href={resolved}
            target={isExternal ? "_blank" : undefined}
            rel={isExternal ? "noopener noreferrer" : undefined}
            className={cn(
              "inline-flex items-center gap-0.5 text-primary underline underline-offset-2 decoration-primary/40",
              "transition-colors hover:text-primary/80 hover:decoration-primary/60",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60 rounded-sm",
            )}
            {...props}
          >
            {linkChildren}
            {isExternal && (
              <ExternalLink className="inline h-3 w-3 shrink-0 text-muted-foreground/60" aria-hidden />
            )}
          </a>
        );
      },

      // ── Tables ───────────────────────────────────────────────────
      table({ children: tableChildren, node: _node, ...props }: any) {
        void _node;
        return (
          <div
            role="region"
            tabIndex={0}
            aria-label="Data table"
            className={cn(
              "not-prose my-4 w-full max-w-full overflow-x-auto rounded-lg",
              "border border-border/65 bg-muted/20",
              "overscroll-x-contain focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            )}
          >
            <table
              className={cn(
                "w-full min-w-max border-collapse text-[13px] leading-5",
                "[&_thead]:bg-muted/45 [&_thead]:text-muted-foreground",
                "[&_th]:border-b [&_th]:border-border/65 [&_th]:px-3 [&_th]:py-2",
                "[&_th]:text-left [&_th]:font-medium",
                "[&_td]:border-b [&_td]:border-border/55 [&_td]:px-3 [&_td]:py-2",
                "[&_th:not(:last-child)]:border-r [&_th:not(:last-child)]:border-border/45",
                "[&_td:not(:last-child)]:border-r [&_td:not(:last-child)]:border-border/45",
                "[&_tbody_tr:last-child_td]:border-b-0",
              )}
              {...props}
            >
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

      // ── Lists ────────────────────────────────────────────────────
      li({ children: liChildren, className: liClassName }: any) {
        const taskItem = liClassName?.includes("task-list-item");
        return (
          <li
            className={cn(
              liClassName,
              taskItem
                ? "flex min-w-0 items-start gap-2 text-[13px] leading-5 [&>p]:m-0"
                : "[&>p]:inline",
            )}
          >
            {liChildren}
          </li>
        );
      },

      // ── Task list checkboxes ─────────────────────────────────────
      input({ type, checked }: any) {
        if (type !== "checkbox") return null;
        return (
          <span
            aria-hidden
            className={cn(
              "mt-0.5 inline-grid h-4 w-4 shrink-0 place-items-center rounded-full",
              "border border-dashed border-muted-foreground/55 bg-background text-background",
              checked && "border-solid border-emerald-500 bg-emerald-500 text-white",
            )}
          >
            {checked ? <Check className="h-3 w-3 stroke-[3]" /> : null}
          </span>
        );
      },

      // ── Inline semantics ─────────────────────────────────────────
      strong({ children: strongChildren }: any) {
        return <strong className="font-semibold text-foreground">{strongChildren}</strong>;
      },

      em({ children: emChildren }: any) {
        return <em className="italic">{emChildren}</em>;
      },

      del({ children: delChildren }: any) {
        return <del className="line-through text-muted-foreground">{delChildren}</del>;
      },

      mark({ children: markChildren }: any) {
        return (
          <mark className="rounded-[5px] bg-yellow-200/75 px-1 py-0.5 text-inherit dark:bg-yellow-300/25">
            {markChildren}
          </mark>
        );
      },

      sub({ children: subChildren }: any) {
        return <sub className="text-[0.72em] leading-none">{subChildren}</sub>;
      },

      sup({ children: supChildren }: any) {
        return <sup className="text-[0.72em] leading-none">{supChildren}</sup>;
      },

      // ── Details / Summary ────────────────────────────────────────
      details({ children: detailsChildren }: any) {
        return (
          <details className="my-3 rounded-xl border border-border/65 bg-muted/25 px-4 py-3 open:pb-4">
            {detailsChildren}
          </details>
        );
      },

      summary({ children: summaryChildren }: any) {
        return (
          <summary className="cursor-pointer select-none text-sm font-medium text-foreground/88 marker:text-muted-foreground">
            {summaryChildren}
          </summary>
        );
      },

      // ── Images ───────────────────────────────────────────────────
      img({ src, alt }: any) {
        if (!src || typeof src !== "string") return null;
        const label = typeof alt === "string" ? alt : "";
        return (
          <span className="not-prose my-3 block">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={src}
              alt={label || "Image"}
              className="max-h-96 max-w-full rounded-lg border border-border/60 object-contain"
              loading="lazy"
              referrerPolicy="no-referrer"
              draggable={false}
            />
            {label && (
              <span className="mt-1 block text-center text-xs text-muted-foreground">
                {label}
              </span>
            )}
          </span>
        );
      },

      // ── Blockquote ───────────────────────────────────────────────
      blockquote({ children: bqChildren }: any) {
        return (
          <blockquote className="my-3 border-l-[3px] border-border/70 pl-4 text-foreground/80">
            {bqChildren}
          </blockquote>
        );
      },

      // ── Horizontal rule ──────────────────────────────────────────
      hr() {
        return <hr className="my-6 border-border/60" />;
      },
    }),
    [streaming],
  );

  return (
    <Streamdown
      mode={streaming ? "streaming" : "static"}
      parseIncompleteMarkdown
      isAnimating={false}
      animated={false}
      linkSafety={{ enabled: false }}
      urlTransform={safeUrl}
      remarkPlugins={[
        remarkBreaks,
        remarkGfm,
        [remarkMath, { singleDollarTextMath: false }],
      ]}
      rehypePlugins={[rehypeKatex]}
      components={components}
      className={cn(
        // ── Typography base (prose) ──
        "prose prose-neutral max-w-none dark:prose-invert",
        // ── Headings ──
        "prose-headings:mt-4 prose-headings:mb-2 prose-headings:font-semibold prose-headings:tracking-tight prose-headings:text-foreground",
        "prose-h1:text-lg prose-h2:text-base prose-h3:text-sm prose-h4:text-[13px]",
        // ── Paragraphs ──
        "prose-p:my-2 prose-p:text-foreground/90",
        // ── Lists ──
        "prose-ul:my-2 prose-ol:my-2 prose-li:my-0.5 prose-li:text-foreground/90",
        // ── Blockquotes ──
        "prose-blockquote:my-3 prose-blockquote:border-l-2 prose-blockquote:font-normal",
        "prose-blockquote:not-italic prose-blockquote:text-foreground/80",
        // ── Links ──
        "prose-a:text-primary prose-a:underline-offset-2 hover:prose-a:text-primary/80",
        // ── Horizontal rules ──
        "prose-hr:my-6 prose-hr:border-border/60",
        // ── Code ──
        "prose-pre:my-0 prose-pre:bg-transparent prose-pre:p-0",
        "prose-code:before:content-none prose-code:after:content-none prose-code:font-normal",
        // ── Strong / emphasis ──
        "prose-strong:text-foreground prose-em:italic",
        // ── Images ──
        "prose-img:my-3 prose-img:rounded-lg",
        // ── Caller override ──
        className,
      )}
    >
      {children}
    </Streamdown>
  );
}