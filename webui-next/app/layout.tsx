import type { Metadata } from "next";
import { Providers } from "./providers";
import "./globals.css";
import { Geist } from "next/font/google";
import { cn } from "@/lib/utils";

const geist = Geist({subsets:['latin'],variable:'--font-sans'});

export const metadata: Metadata = {
  title: "Auto Cut Bot",
  description:
    "A lightweight, open-source AI agent framework with a React/TypeScript WebUI",
  keywords: ["AI", "agent", "chatbot", "automation", "Auto Cut Bot"],
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="zh-CN" suppressHydrationWarning className={cn("h-full antialiased", "font-sans", geist.variable)}>
      <body className="h-full">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}