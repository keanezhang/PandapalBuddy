/**
 * vitest 全局 setup：jsdom 环境 + 测试间 DOM 清理。
 */
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
});
