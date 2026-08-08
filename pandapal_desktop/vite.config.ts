import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  // 认证接口地址统一由 VITE_RELAY_AUTH_URL 提供（完整 URL，含 /auth 路径，见 .env.example）。
  // 开发代理目标从它推导：去掉尾部 /auth 得到源站根，供 dev server 转发 /auth 请求（绕 CORS）。
  const env = loadEnv(mode, process.cwd(), "");
  const relayAuthUrl = (env.VITE_RELAY_AUTH_URL || "").replace(/\/+$/, "");
  const proxyTarget = relayAuthUrl.replace(/\/auth$/i, "");

  return {
    plugins: [react()],

    resolve: {
      alias: {
        // monaco-inline-diff-review 已内联为 src/monacoInlineDiff 子模块（相对路径引用）
      },
    },

    clearScreen: false,
    server: {
      hmr: true,
      port: 5173,
      strictPort: true,
      watch: {
        ignored: ["**/src-tauri/**"],
      },
      proxy: proxyTarget
        ? {
            "/auth": {
              target: proxyTarget,
              changeOrigin: true,
              secure: true,
            },
          }
        : undefined,
    },
  };
});
