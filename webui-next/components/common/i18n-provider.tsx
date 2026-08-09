"use client";

import { I18nextProvider } from "react-i18next";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { useEffect, useState, type ReactNode } from "react";

// Import all locale files
import en from "@/i18n/locales/en.json";
import zhCN from "@/i18n/locales/zh-CN.json";
import zhTW from "@/i18n/locales/zh-TW.json";
import ja from "@/i18n/locales/ja.json";
import ko from "@/i18n/locales/ko.json";
import fr from "@/i18n/locales/fr.json";
import es from "@/i18n/locales/es.json";
import ptBR from "@/i18n/locales/pt-BR.json";
import vi from "@/i18n/locales/vi.json";
import id from "@/i18n/locales/id.json";

const resources = {
  en: { common: en },
  "zh-CN": { common: zhCN },
  "zh-TW": { common: zhTW },
  ja: { common: ja },
  ko: { common: ko },
  fr: { common: fr },
  es: { common: es },
  "pt-BR": { common: ptBR },
  vi: { common: vi },
  id: { common: id },
};

const defaultLocale =
  typeof window !== "undefined"
    ? localStorage.getItem("language") || "zh-CN"
    : "zh-CN";

let i18nInitialized = false;

if (!i18nInitialized) {
  i18next.use(initReactI18next).init({
    resources,
    lng: defaultLocale,
    fallbackLng: "zh-CN",
    defaultNS: "common",
    interpolation: { escapeValue: false },
    returnObjects: true,
  });
  i18nInitialized = true;
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false);

  useEffect(() => {
    setReady(true);
  }, []);

  if (!ready) return null;

  return <I18nextProvider i18n={i18next}>{children}</I18nextProvider>;
}

export function changeLanguage(lang: string) {
  i18next.changeLanguage(lang);
  localStorage.setItem("language", lang);
}
