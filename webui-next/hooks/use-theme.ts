"use client";

import { useTheme } from "@/components/common/theme-provider";

export function useThemeValue(): "light" | "dark" {
  const { resolvedTheme } = useTheme();
  return resolvedTheme;
}

export { useTheme };