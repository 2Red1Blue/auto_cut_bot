"use client";

import { ThemeProvider } from "@/components/common/theme-provider";
import { I18nProvider } from "@/components/common/i18n-provider";
import { WebSocketProvider } from "@/components/common/websocket-provider";

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <ThemeProvider>
      <I18nProvider>
        <WebSocketProvider>
          {children}
        </WebSocketProvider>
      </I18nProvider>
    </ThemeProvider>
  );
}