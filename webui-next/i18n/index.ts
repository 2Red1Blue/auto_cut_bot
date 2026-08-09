import i18n from "i18next";

export { i18n as default };

export function currentLocale(): string {
  return i18n.language || "zh-CN";
}