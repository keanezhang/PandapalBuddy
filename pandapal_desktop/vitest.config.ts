/**
 * vitest 独立配置（jsdom 组件测试）。
 * 与 vite.config.ts 分离：vitest 优先读本文件，不合并 vite.config.ts。
 */
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    globals: false,
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
