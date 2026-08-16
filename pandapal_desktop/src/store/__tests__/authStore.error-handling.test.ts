/**
 * authStore login/register 错误处理 + readResponseJson 组件测试。
 *
 * 用例编号回链 docs/tests/authStore.error-handling.design.md §6（L1–L13, R1–R5）。
 * Mock 决策（设计 §3）：
 *   - invoke（@tauri-apps/api/core）→ mock，断言调用参数与次数（R5/inv-1/inv-2）
 *   - fetch → mock（vi.stubGlobal），控制 Response 形状与 reject
 *   - i18n → 真实实例，期望文案用 i18n.t(...) 运行时解析，防 locale flaky
 *   - readResponseJson → 不 mock，经 login/register 公开路径间接覆盖
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { invoke } from "@tauri-apps/api/core";
import i18n from "../../i18n";
import { useAuthStore } from "../authStore";

const { invokeMock } = vi.hoisted(() => ({ invokeMock: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));

let fetchMock: ReturnType<typeof vi.fn>;

interface AuthSnapshot {
  status: string;
  token: string | null;
  userId: string | null;
  username: string | null;
  authMode: string | null;
}

function snapshotAuth(): AuthSnapshot {
  const s = useAuthStore.getState();
  return {
    status: s.status,
    token: s.token,
    userId: s.userId,
    username: s.username,
    authMode: s.authMode,
  };
}

function resetAuthStore(): void {
  useAuthStore.setState({
    status: "loading",
    token: null,
    userId: null,
    username: null,
    authMode: null,
    error: null,
  });
}

/** inv-1：失败路径不调 invoke，且 status/token/userId/username/authMode 不变。 */
function expectNoAuthSideEffects(before: AuthSnapshot): void {
  expect(invokeMock).not.toHaveBeenCalled();
  const s = useAuthStore.getState();
  expect(s.status).toBe(before.status);
  expect(s.token).toBe(before.token);
  expect(s.userId).toBe(before.userId);
  expect(s.username).toBe(before.username);
  expect(s.authMode).toBe(before.authMode);
}

describe("authStore login/register 错误处理", () => {
  beforeEach(() => {
    invokeMock.mockReset();
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    resetAuthStore();
  });

  afterEach(async () => {
    vi.unstubAllGlobals();
    await i18n.changeLanguage("zh-CN");
  });

  // L1: R2 + inv-1
  it("L1: login !ok + 合法 JSON detail.error → 显示后端 detail.error", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 401,
      text: async () =>
        JSON.stringify({ detail: { error: "Invalid credentials", code: "invalid_credentials" } }),
    });
    const before = snapshotAuth();

    const result = await useAuthStore.getState().login("alice", "wrong");

    expect(result).toBe(false);
    expect(useAuthStore.getState().error).toBe("Invalid credentials");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: "alice", password: "wrong" }),
    });
    expectNoAuthSideEffects(before);
  });

  // L2: R3 + inv-1
  it("L2: login !ok + 合法 JSON 无 detail → fallback errLoginFailed(status)", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 500, text: async () => "{}" });
    const before = snapshotAuth();

    const result = await useAuthStore.getState().login("alice", "pw");

    expect(result).toBe(false);
    expect(useAuthStore.getState().error).toBe(i18n.t("auth.errLoginFailed", { status: 500 }));
    expectNoAuthSideEffects(before);
  });

  // L3: R1 + R4（语义确认 §4）+ inv-1
  it("L3: login !ok + 非法 JSON（HTML 错误页）→ errBadResponse 覆盖默认文案", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 502,
      text: async () => "<html><body>502 Bad Gateway</body></html>",
    });
    const before = snapshotAuth();

    const result = await useAuthStore.getState().login("alice", "pw");

    expect(result).toBe(false);
    expect(useAuthStore.getState().error).toBe(i18n.t("auth.errBadResponse"));
    expectNoAuthSideEffects(before);
  });

  // L4: R5 + R1 + inv-2
  it("L4: login 成功路径 → invoke(auth_notify_ready) 参数正确 + authenticated", async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      text: async () =>
        JSON.stringify({
          token: "t1",
          user_id: "u1",
          username: "alice",
          expires_at: "2026-01-01T00:00:00Z",
        }),
    });
    invokeMock.mockResolvedValue(undefined);

    const result = await useAuthStore.getState().login("alice", "pw");

    expect(result).toBe(true);
    expect(invokeMock).toHaveBeenCalledTimes(1);
    expect(invokeMock).toHaveBeenCalledWith("auth_notify_ready", {
      token: "t1",
      userId: "u1",
      username: "alice",
    });
    const s = useAuthStore.getState();
    expect(s.status).toBe("authenticated");
    expect(s.token).toBe("t1");
    expect(s.userId).toBe("u1");
    expect(s.username).toBe("alice");
    expect(s.authMode).toBe("cloud");
    expect(s.error).toBeNull();
  });

  // L5: R6（大小写不敏感 + tls 子类）+ inv-1
  it.each([
    ["certificate lowercase", new Error("The certificate for this server is invalid.")],
    ["TLS uppercase", new Error("TLS handshake failed")],
    ["CERTIFICATE uppercase", new Error("SSL CERTIFICATE verify failed")],
  ])("L5: 外层 catch certificate/tls → errTls（$case）", async (_case, err) => {
    fetchMock.mockRejectedValueOnce(err);
    const before = snapshotAuth();

    const result = await useAuthStore.getState().login("alice", "pw");

    expect(result).toBe(false);
    expect(useAuthStore.getState().error).toBe(i18n.t("auth.errTls"));
    expectNoAuthSideEffects(before);
  });

  // L6: R6 + R8（DOMException）+ inv-1
  it.each([
    ["TypeError fetch failed", new TypeError("fetch failed")],
    ["Error Failed to fetch", new Error("Failed to fetch")],
    ["DOMException NetworkError", new DOMException("Network request failed", "NetworkError")],
  ])("L6: 外层 catch fetch/network → errNetwork（$case）", async (_case, err) => {
    fetchMock.mockRejectedValueOnce(err);
    const before = snapshotAuth();

    const result = await useAuthStore.getState().login("alice", "pw");

    expect(result).toBe(false);
    expect(useAuthStore.getState().error).toBe(i18n.t("auth.errNetwork"));
    expectNoAuthSideEffects(before);
  });

  // L7: R6 + inv-1
  it("L7: 外层 catch 其他错误 → 原文显示", async () => {
    fetchMock.mockRejectedValueOnce(new Error("some unexpected server error"));
    const before = snapshotAuth();

    const result = await useAuthStore.getState().login("alice", "pw");

    expect(result).toBe(false);
    expect(useAuthStore.getState().error).toBe("some unexpected server error");
    expectNoAuthSideEffects(before);
  });

  // L8: R9 + inv-1（成功响应 body 非法，不得触发 sidecar 启动）
  it("L8: 成功响应（200）+ 非法 JSON → errBadResponse 且不被误分类", async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => "<!DOCTYPE html><html>...</html>",
    });
    const before = snapshotAuth();

    const result = await useAuthStore.getState().login("alice", "pw");

    expect(result).toBe(false);
    expect(useAuthStore.getState().error).toBe(i18n.t("auth.errBadResponse"));
    expectNoAuthSideEffects(before);
  });

  // L9: R10（防误分类前置约束，unit 静态断言）
  it("L9: errBadResponse 文案不含 certificate/tls/fetch/network（双 locale）", async () => {
    for (const lng of ["zh-CN", "en-US"] as const) {
      await i18n.changeLanguage(lng);
      const text = i18n.t("auth.errBadResponse").toLowerCase();
      expect(text).not.toContain("certificate");
      expect(text).not.toContain("tls");
      expect(text).not.toContain("fetch");
      expect(text).not.toContain("network");
    }
  });

  // L11: R12 + R6 + inv-1（tls/certificate 分支优先于 fetch/network）
  it("L11: 分类优先级：消息同时含 tls 与 fetch → errTls", async () => {
    fetchMock.mockRejectedValueOnce(new Error("fetch failed: tls certificate expired"));
    const before = snapshotAuth();

    const result = await useAuthStore.getState().login("alice", "pw");

    expect(result).toBe(false);
    expect(useAuthStore.getState().error).toBe(i18n.t("auth.errTls"));
    expectNoAuthSideEffects(before);
  });

  // L12: R14 + inv-1（非 Error 异常走 String(e) 原文）
  it("L12: 非 Error 异常 → String(e) 原文", async () => {
    fetchMock.mockRejectedValueOnce("boom");
    const before = snapshotAuth();

    const result = await useAuthStore.getState().login("alice", "pw");

    expect(result).toBe(false);
    expect(useAuthStore.getState().error).toBe("boom");
    expectNoAuthSideEffects(before);
  });

  // L13: R13 + R3 + inv-1（空串是 falsy，走 || 兜底）
  it("L13: login !ok + detail.error 为空串 → fallback errLoginFailed(status)", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 400,
      text: async () => JSON.stringify({ detail: { error: "", code: "E1" } }),
    });
    const before = snapshotAuth();

    const result = await useAuthStore.getState().login("alice", "pw");

    expect(result).toBe(false);
    expect(useAuthStore.getState().error).toBe(i18n.t("auth.errLoginFailed", { status: 400 }));
    expectNoAuthSideEffects(before);
  });

  // R1: R5 + R7 + inv-2（register 成功，/register endpoint）
  it("R1: register 成功路径 → invoke + authenticated（/register endpoint）", async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      text: async () =>
        JSON.stringify({ token: "rt", user_id: "ru", username: "bob", expires_at: "..." }),
    });
    invokeMock.mockResolvedValue(undefined);

    const result = await useAuthStore.getState().register("bob", "pw");

    expect(result).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith("/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: "bob", password: "pw" }),
    });
    expect(invokeMock).toHaveBeenCalledTimes(1);
    expect(invokeMock).toHaveBeenCalledWith("auth_notify_ready", {
      token: "rt",
      userId: "ru",
      username: "bob",
    });
    const s = useAuthStore.getState();
    expect(s.status).toBe("authenticated");
    expect(s.authMode).toBe("cloud");
    expect(s.error).toBeNull();
  });

  // R2: R2 + R7 + inv-1
  it("R2: register !ok + 合法 JSON detail.error → 显示 detail.error", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 409,
      text: async () => JSON.stringify({ detail: { error: "username already taken", code: "dup" } }),
    });
    const before = snapshotAuth();

    const result = await useAuthStore.getState().register("bob", "pw");

    expect(result).toBe(false);
    expect(useAuthStore.getState().error).toBe("username already taken");
    expectNoAuthSideEffects(before);
  });

  // R3: R3 + R7 + inv-1
  it("R3: register !ok + 无 detail → fallback errRegisterFailed(status)", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 422, text: async () => "{}" });
    const before = snapshotAuth();

    const result = await useAuthStore.getState().register("bob", "pw");

    expect(result).toBe(false);
    expect(useAuthStore.getState().error).toBe(i18n.t("auth.errRegisterFailed", { status: 422 }));
    expectNoAuthSideEffects(before);
  });

  // R4: R1 + R4 + R7 + inv-1
  it("R4: register !ok + 非法 JSON → errBadResponse", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 502,
      text: async () => "<html>Bad Gateway</html>",
    });
    const before = snapshotAuth();

    const result = await useAuthStore.getState().register("bob", "pw");

    expect(result).toBe(false);
    expect(useAuthStore.getState().error).toBe(i18n.t("auth.errBadResponse"));
    expectNoAuthSideEffects(before);
  });

  // R5: R6 + R7 + R8 + inv-1（register 使用与 login 相同的分类逻辑）
  it("R5: register 网络异常 → errNetwork", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("fetch failed"));
    const before = snapshotAuth();

    const result = await useAuthStore.getState().register("bob", "pw");

    expect(result).toBe(false);
    expect(useAuthStore.getState().error).toBe(i18n.t("auth.errNetwork"));
    expectNoAuthSideEffects(before);
  });
});

// L10 必须隔离：RELAY_AUTH_BASE_URL 是模块求值时计算的 const，须在导入前翻转 env 并重求值。
describe("authStore 无 RELAY_AUTH_BASE_URL（DEV=false + 空 VITE_RELAY_AUTH_URL）", () => {
  beforeEach(() => {
    invokeMock.mockReset();
    vi.stubEnv("DEV", false);
    vi.stubEnv("VITE_RELAY_AUTH_URL", "");
    vi.resetModules();
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  // L10: R11 + inv-1（不 fetch、不 invoke）
  it("L10: 无 RELAY_AUTH_BASE_URL → errNoServer（不 fetch、不 invoke）", async () => {
    const { useAuthStore: store } = await import("../authStore");

    const result = await store.getState().login("alice", "pw");

    expect(result).toBe(false);
    expect(store.getState().error).toBe(i18n.t("auth.errNoServer"));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(invokeMock).not.toHaveBeenCalled();
  });
});
