import { defineConfig } from "@playwright/test";

/**
 * 交互效果 e2e 测试配置（位于 tests/ 下）。
 *
 * testDir 相对本配置文件解析；webServer 命令在项目根（cwd）执行，
 * 通过 tests/vite.demo.config.ts 启动演示页 dev server，
 * 测试访问 http://localhost:5199/ 上真实 Monaco 渲染的组件。
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: "http://localhost:5199",
    viewport: { width: 1280, height: 800 },
    screenshot: "only-on-failure",
  },
  webServer: {
    command: "npx vite --config tests/vite.demo.config.ts",
    // Playwright 默认在配置文件所在目录（tests/）执行 command；
    // 必须回到项目根，否则 vite config 路径与 root 会错位
    cwd: "..",
    port: 5199,
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
