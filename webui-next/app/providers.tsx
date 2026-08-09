"use client";

import { ThemeProvider } from "@/components/common/theme-provider";
import { I18nProvider } from "@/components/common/i18n-provider";
import { WebSocketProvider } from "@/components/common/websocket-provider";
import { ClientProvider } from "@/providers/client-provider";

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <ThemeProvider>
      <I18nProvider>
        <ClientProvider>
          <WebSocketProvider>
            {children}
          </WebSocketProvider>
        </ClientProvider>
      </I18nProvider>
    </ThemeProvider>
  );
}