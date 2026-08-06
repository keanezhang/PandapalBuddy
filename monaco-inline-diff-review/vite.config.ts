import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import dts from "vite-plugin-dts";
import { resolve } from "path";

export default defineConfig({
  plugins: [
    react(),
    dts({
      include: ["src"],
      // 不开 rollupTypes：多入口（index + engine/index）basename 相同，
      // vite-plugin-dts 按 basename 计算 rollup 输出路径会互相覆盖，
      // 导致 dist/index.d.ts 被 engine 类型覆盖（缺 CodeRenderer 等导出）。
      rollupTypes: false,
    }),
  ],
  build: {
    lib: {
      entry: {
        index: resolve(__dirname, "src/index.ts"),
        "engine/index": resolve(__dirname, "src/engine/index.ts"),
      },
      formats: ["es", "cjs"],
    },
    rollupOptions: {
      external: [
        "react",
        "react-dom",
        "react/jsx-runtime",
        "@monaco-editor/react",
        "monaco-editor",
      ],
      output: {
        globals: {
          react: "React",
          "react-dom": "ReactDOM",
          "@monaco-editor/react": "MonacoEditorReact",
          "monaco-editor": "monaco",
        },
      },
    },
  },
});
