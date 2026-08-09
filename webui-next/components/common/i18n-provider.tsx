"use client";

import { createContext, useContext, useEffect, useState } from "react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";

// Lazy-load translations — copied from old webui
const locales: Record<string, () => Promise<Record<string, unknown>>> = {
  en: () => import("@/i18n/locales/en.json").then((m) => m.default),
  "zh-CN": () => import("@/i18n/locales/zh-CN.json").then((m) => m.default),
  "zh-TW": () => import("@/i18n/locales/zh-TW.json").then((m) => m.default),
  ja: () => import("@/i18n/locales/ja.json").then((m) => m.default),
  ko: () => import("@/i18n/locales/ko.json").then((m) => m.default),
};

interface I18nContextValue {
  language: string;
  setLanguage: (lang: string) => void;
  ready: boolean;
}

const I18nContext = createContext<I18nContextValue>({
  language: "zh-CN",
  setLanguage: () => {},
  ready: false,
});

export function useI18n() {
  return useContext(I18nContext);
}

let initialized = false;

export function I18nProvider({ children }: { children: React.ReactNode }) {
  const [language, setLanguageState] = useState("zh-CN");
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (initialized) {
      setReady(true);
      return;
    }

    const stored = localStorage.getItem("language") || "zh-CN";
    setLanguageState(stored);

    i18next.use(initReactI18next).init({
      lng: stored,
      fallbackLng: "zh-CN",
      resources: {},
      interpolation: {
        escapeValue: false,
      },
    });

    // Load stored language
    const loader = locales[stored];
    if (loader) {
      loader().then((resources) => {
        i18next.addResourceBundle(stored, "translation", resources, true, true);
        initialized = true;
        setReady(true);
      });
    } else {
      initialized = true;
      setReady(true);
    }
  }, []);

  const setLanguage = (lang: string) => {
    setLanguageState(lang);
    localStorage.setItem("language", lang);

    const loader = locales[lang];
    if (loader) {
      loader().then((resources) => {
        i18next.addResourceBundle(lang, "translation", resources, true, true);
        i18next.changeLanguage(lang);
      });
    } else {
      i18next.changeLanguage(lang);
    }
  };

  return (
    <I18nContext.Provider value={{ language, setLanguage, ready }}>
      {children}
    </I18nContext.Provider>
  );
}