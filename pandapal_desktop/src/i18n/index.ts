/**
 * i18n 基础设施（i18next + react-i18next）
 *
 * - 初始语言：导入期同步读取 preferenceStore.locale（persist 已 hydration）
 * - 语言切换：applyLocale() 同时改 i18n 语言 + <html lang> + preferenceStore
 * - 资源：locales/{zh-CN,en-US}.json，Key 结构须两侧一致（测试保证）
 */
import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import { usePreferenceStore } from "../store/preferenceStore";
import type { AppLocale } from "../store/preferenceStore";
import zhCN from "./locales/zh-CN.json";
import enUS from "./locales/en-US.json";

export type { AppLocale } from "../store/preferenceStore";

export const SUPPORTED_LOCALES: AppLocale[] = ["zh-CN", "en-US"];

const resources = {
  "zh-CN": { translation: zhCN },
  "en-US": { translation: enUS },
} as const;

i18n.use(initReactI18next).init({
  resources,
  lng: (usePreferenceStore.getState().locale as AppLocale) ?? "zh-CN",
  fallbackLng: "zh-CN",
  interpolation: { escapeValue: false },
  returnEmptyString: false,
});

/** 切换界面语言：i18n 语言 + <html lang> + 持久化偏好 */
export function applyLocale(locale: AppLocale): void {
  void i18n.changeLanguage(locale);
  document.documentElement.lang = locale;
  usePreferenceStore.getState().setLocale(locale);
}

export default i18n;
