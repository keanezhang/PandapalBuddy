/**
 * 演示页专用 Vite 配置（位于 tests/ 下，不影响库构建 vite.config.ts）。
 *
 * - root 指向 tests/demo/，开发服务器 http://localhost:5199/
 * - publicDir 把 node_modules/monaco-editor/min 映射到站点根，
 *   使 loader.config({ paths: { vs: "/vs" } }) 能本地加载 Monaco（与生产一致）
 */
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  root: "tests/demo",
  publicDir: "../../node_modules/monaco-editor/min",
  plugins: [react()],
  server: {
    port: 5199,
    strictPort: true,
  },
  preview: {
    port: 5199,
    strictPort: true,
  },
});
