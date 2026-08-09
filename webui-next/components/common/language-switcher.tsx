"use client";

import { useTranslation } from "react-i18next";
import { changeLanguage } from "./i18n-provider";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Globe } from "lucide-react";

const LOCALES: Record<string, string> = {
  en: "English",
  "zh-CN": "简体中文",
  "zh-TW": "繁體中文",
  ja: "日本語",
  ko: "한국어",
  fr: "Français",
  es: "Español",
  "pt-BR": "Português (Brasil)",
  vi: "Tiếng Việt",
  id: "Bahasa Indonesia",
};

export function LanguageSwitcher() {
  const { i18n } = useTranslation();
  const language = i18n.language;
  const setLanguage = changeLanguage;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button variant="ghost" size="icon" title="Switch language" />}>
          <Globe className="h-4 w-4" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {Object.entries(LOCALES).map(([code, label]) => (
          <DropdownMenuItem
            key={code}
            onClick={() => setLanguage(code)}
            className={code === language ? "bg-accent" : ""}
          >
            {label}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}