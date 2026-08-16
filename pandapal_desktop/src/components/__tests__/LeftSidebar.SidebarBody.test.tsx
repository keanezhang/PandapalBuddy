/**
 * LeftSidebar.SidebarBody.test.tsx
 *
 * 覆盖「SidebarBody flex 吸收容器修复」的设计用例（TC-1 ~ TC-11）。
 * 被测：LeftSidebar → SidebarBody / SidebarDock / SidebarHeader / ResizeHandle。
 *
 * Mock 策略（沿用 settingsPanelCrash.test.tsx / InteractionInline.test.tsx 模式）：
 *   - 真实 Zustand store + beforeEach 种子 seedSidebarState()
 *   - 真实 <MemoryRouter> + 真实 i18n（zh-CN）
 *   - mock useBackend（全量 no-op stub，防止解构 undefined 崩溃）
 *   - mock Tauri 三模块：@tauri-apps/api/core / plugin-fs / plugin-dialog
 *
 * Oracle：inline style 的 golden value（React 序列化契约）与 store 状态。
 * 注：jsdom 不计算 flex 布局（offsetHeight/clientHeight 恒 0），以下四项
 * 不在本文件断言，转 playwright / 人工（见设计文档 §8）：
 *   - P-1 拖高会话区后 SidebarDock 仍完整可见（需真实 viewport boundingBox）
 *   - P-2 溢出被 bodyRoot overflow:hidden 裁剪、不泄漏到 Dock（需截图对比）
 *   - P-3 极小 viewport 下根容器无溢出（需 scrollHeight/clientHeight）
 *   - P-4 coding 模式同样不顶出 Dock 的视觉后果（需真实渲染）
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { LeftSidebar } from "../LeftSidebar";
import i18n from "../../i18n";
import { usePreferenceStore } from "../../store/preferenceStore";
import { useSessionStore } from "../../store/sessionStore";
import { useWorkspaceStore } from "../../store/workspaceStore";
import { useFileStore } from "../../store/fileStore";
import { useAuthStore } from "../../store/authStore";
import { useConnectionStore } from "../../store/connectionStore";
import { useCommandPaletteStore } from "../../store/commandPaletteStore";

// ── Tauri IPC mock（jsdom 无 Tauri 运行时，防御性 no-op）──
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/plugin-fs", () => ({
  readTextFile: vi.fn(),
  writeTextFile: vi.fn(),
  readDir: vi.fn(),
  remove: vi.fn(),
  stat: vi.fn(),
  readFile: vi.fn(),
}));
vi.mock("@tauri-apps/plugin-dialog", () => ({
  open: vi.fn(),
  save: vi.fn(),
}));

// ── useBackend mock：全量 no-op，避免 MainNav/SessionListPanel/GroupSection 解构崩溃 ──
vi.mock("../../providers/BackendProvider", () => ({
  useBackend: () => ({
    sendMessage: vi.fn(),
    stopGeneration: vi.fn(),
    sendHitlDecision: vi.fn(),
    sendInteractionResponse: vi.fn(),
    sendPlanApprovalDecision: vi.fn(),
    requestScheduledTasks: vi.fn(),
    requestDashboard: vi.fn(),
    deleteScheduledTask: vi.fn(),
    searchRequest: vi.fn(),
    requestSkillList: vi.fn(),
    requestSkillDetail: vi.fn(),
    saveSkill: vi.fn(),
    deleteSkill: vi.fn(),
    importSkill: vi.fn(),
    exportSkill: vi.fn(),
    pendingTaskNotification: null,
    clearTaskNotification: vi.fn(),
    requestSessionList: vi.fn(),
    requestGroupSessions: vi.fn(),
    createSession: vi.fn(),
    switchSession: vi.fn(),
    deleteSession: vi.fn(),
    renameSession: vi.fn(),
    groupMutate: vi.fn(),
    requestSessionHistory: vi.fn(),
    setBudget: vi.fn(),
    budgetQuery: vi.fn(),
  }),
}));

// ── 元素定位常量（zh-CN golden）──
const RESIZE_HINT = "拖动调整对话区高度，双击复位";
const LOGOUT = "退出登录";

/** 统一种子：隔离单例 store 的跨测试污染 */
async function seedSidebarState() {
  usePreferenceStore.setState({
    mode: "office",
    sidebarCollapsed: false,
    sessionPanelHeight: null,
    sidebarWidth: 260,
  });
  useSessionStore.setState({
    sessions: [],
    groups: [],
    currentSessionId: null,
    currentGroupFilter: "all",
    page: 1,
    hasMore: true,
    loading: false,
  });
  useWorkspaceStore.setState({ current: null, status: "idle", recent: [], last: null, error: null });
  useFileStore.setState({
    fileTree: [],
    fileTreeLoading: false,
    openFiles: [],
    activeFileId: null,
    fileContents: {},
    _cacheOrder: [],
    suggestions: {},
  });
  useAuthStore.setState({
    status: "authenticated",
    username: "tester",
    token: null,
    userId: "u1",
    authMode: "local",
    error: null,
  });
  useConnectionStore.setState({ status: "connected", errorMessage: null });
  useCommandPaletteStore.setState({ open: false });
  await i18n.changeLanguage("zh-CN");
}

function renderSidebar() {
  return render(
    <MemoryRouter>
      <LeftSidebar />
    </MemoryRouter>,
  );
}

/** ResizeHandle（两模式唯一） */
function getHandle(): HTMLElement {
  return screen.getByTitle(RESIZE_HINT);
}

/** SidebarBody 根节点 = handle 的唯一父级 */
function getBodyRoot(): HTMLElement {
  return getHandle().parentElement as HTMLElement;
}

/** office 上区 / coding 文件区 = handle 的前一个兄弟 */
function getUpperDiv(): HTMLElement {
  return getHandle().previousElementSibling as HTMLElement;
}

/** 会话区 div（两模式通用）：handle → SectionHeader「对话列表」→ session div */
function getSessionDiv(): HTMLElement {
  return getHandle().nextElementSibling?.nextElementSibling as HTMLElement;
}

/**
 * jsdom 的 CSSOM 会把长度 0 序列化为 "0px"（React 命令式/声明式赋值后 getter 均如此）。
 * 兼容 React 版本差异：接受 "0" 或 "0px"。
 */
function expectZeroLength(value: string) {
  expect(["0", "0px"]).toContain(value);
}

describe("LeftSidebar / SidebarBody flex 吸收容器修复", () => {
  beforeEach(async () => {
    await seedSidebarState();
  });

  // ── TC-1 · R1/R2/R5(office) / inv-S1 [P0] ──
  it("TC-1 office 模式 SidebarBody 根节点携带完整吸收容器样式", () => {
    renderSidebar();
    const bodyRoot = getBodyRoot();

    expect(bodyRoot.style.flex).toBe("1 1 0%");
    expectZeroLength(bodyRoot.style.minHeight);
    expect(bodyRoot.style.display).toBe("flex");
    expect(bodyRoot.style.flexDirection).toBe("column");
    expect(bodyRoot.style.overflow).toBe("hidden");
  });

  // ── TC-2 · R1/R2/R5(coding) / inv-S1 [P0] ──
  it("TC-2 coding 模式 SidebarBody 根节点同样携带吸收容器样式", () => {
    usePreferenceStore.setState({ mode: "coding" });
    renderSidebar();
    const bodyRoot = getBodyRoot();

    expect(bodyRoot.style.flex).toBe("1 1 0%");
    expectZeroLength(bodyRoot.style.minHeight);
    expect(bodyRoot.style.display).toBe("flex");
    expect(bodyRoot.style.flexDirection).toBe("column");
    expect(bodyRoot.style.overflow).toBe("hidden");
  });

  // ── TC-3 · R3 / inv-S2 [P0] ──
  it("TC-3 SidebarDock 容器 flexShrink:0，且是根容器最后一个 flex item", () => {
    renderSidebar();
    const bodyRoot = getBodyRoot();
    const dock = screen.getByRole("button", { name: LOGOUT }).parentElement as HTMLElement;

    expect(dock.style.flexShrink).toBe("0");
    expect(dock.previousElementSibling).toBe(bodyRoot);
  });

  // ── TC-4 · inv-S3 + 根容器裁剪边界 [P2] ──
  it("TC-4 固定外壳契约：Header flexShrink:0 + 根容器 overflow:hidden", () => {
    renderSidebar();
    const header = screen.getByText("PandaPal").parentElement as HTMLElement;
    const root = header.parentElement as HTMLElement;

    expect(header.style.flexShrink).toBe("0");
    expect(root.style.overflow).toBe("hidden");
    expect(root.style.display).toBe("flex");
    expect(root.style.flexDirection).toBe("column");
  });

  // ── TC-5 · inv-S4(null) + inv-S5(null) [P1] ──
  it("TC-5 office 默认（sessionPanelHeight=null）分区 flex 契约", () => {
    renderSidebar();
    const upperDiv = getUpperDiv();
    const sessionDiv = getSessionDiv();

    expect(upperDiv.style.flex).toBe("0 0 auto");
    // 会话区为弹性吸收区：flex-grow=1 + basis=0%（jsdom 会把 flex:1 展开为 "1 1 0%"）
    expect(sessionDiv.style.flexGrow).toBe("1");
    expect(sessionDiv.style.flexBasis).toBe("0%");
    expectZeroLength(sessionDiv.style.minHeight);
    expect(sessionDiv.style.overflowY).toBe("auto");
  });

  // ── TC-6 · inv-S4(fixed) + inv-S5(fixed) [P1] ──
  it("TC-6 office 已拖拽（sessionPanelHeight=300）分区 flex 契约", () => {
    usePreferenceStore.setState({ sessionPanelHeight: 300 });
    renderSidebar();
    const upperDiv = getUpperDiv();
    const sessionDiv = getSessionDiv();

    expect(upperDiv.style.flex).toBe("1 1 auto");
    expectZeroLength(upperDiv.style.minHeight);
    expect(upperDiv.style.overflowY).toBe("auto");

    expect(sessionDiv.style.height).toBe("300px");
    expect(sessionDiv.style.flexShrink).toBe("0");
    expectZeroLength(sessionDiv.style.minHeight);
    expect(sessionDiv.style.overflowY).toBe("auto");
  });

  // ── TC-7 · inv-S6 [P1] ──
  it("TC-7 coding 默认分区 flex 契约（文件区弹性、会话区固定矮区）", () => {
    usePreferenceStore.setState({ mode: "coding" });
    renderSidebar();
    const fileDiv = getUpperDiv();
    const sessionDiv = getSessionDiv();

    expect(fileDiv.style.flexGrow).toBe("1");
    expect(fileDiv.style.flexBasis).toBe("0%");
    expectZeroLength(fileDiv.style.minHeight);
    expect(fileDiv.style.overflowY).toBe("auto");

    expect(sessionDiv.style.height).toBe("180px");
    expect(sessionDiv.style.flexShrink).toBe("0");
    expect(sessionDiv.style.overflowY).toBe("auto");
  });

  // ── TC-8 · R4 / inv-L1 + inv-L3 [P0] ──
  it("TC-8 拖拽高度钳制 [120,600]，mouseup 提交钳制后高度", () => {
    renderSidebar();
    const handle = getHandle();
    const sessionDiv = getSessionDiv();

    fireEvent.mouseDown(handle, { clientY: 200 });
    // jsdom offsetHeight=0 → dragStartH=0；delta=60 → h=120
    act(() => {
      window.dispatchEvent(new MouseEvent("mousemove", { clientY: 140 }));
    });
    expect(sessionDiv.style.height).toBe("120px");

    // delta=700 → h=600
    act(() => {
      window.dispatchEvent(new MouseEvent("mousemove", { clientY: -500 }));
    });
    expect(sessionDiv.style.height).toBe("600px");

    act(() => {
      window.dispatchEvent(new MouseEvent("mouseup"));
    });
    expect(usePreferenceStore.getState().sessionPanelHeight).toBe(600);
  });

  // ── TC-9 · R4 / inv-L2 [P1]（unit：直接测真实 store，不渲染）──
  it("TC-9 setSessionPanelHeight 钳制 [120,600] 且 null 透传", () => {
    usePreferenceStore.setState({ sessionPanelHeight: null });
    const set = usePreferenceStore.getState().setSessionPanelHeight;

    set(50);
    expect(usePreferenceStore.getState().sessionPanelHeight).toBe(120);
    set(9999);
    expect(usePreferenceStore.getState().sessionPanelHeight).toBe(600);
    set(120);
    expect(usePreferenceStore.getState().sessionPanelHeight).toBe(120);
    set(600);
    expect(usePreferenceStore.getState().sessionPanelHeight).toBe(600);
    set(null);
    expect(usePreferenceStore.getState().sessionPanelHeight).toBe(null);
  });

  // ── TC-10 · R6 / inv-L5 [P1] ──
  it("TC-10 拖拽 mousedown 将 target 与上区钉死为拖拽态样式", () => {
    renderSidebar();
    const handle = getHandle();
    const upperDiv = getUpperDiv();
    const sessionDiv = getSessionDiv();

    fireEvent.mouseDown(handle, { clientY: 200 });

    expect(sessionDiv.style.flex).toBe("0 0 auto");
    expect(sessionDiv.style.height).toBe("0px");
    expect(upperDiv.style.flex).toBe("1 1 auto");
    expectZeroLength(upperDiv.style.minHeight);
    expect(upperDiv.style.overflowY).toBe("auto");
  });

  // ── TC-11 · R7 / inv-L4 [P2] ──
  it("TC-11 双击 ResizeHandle 复位 sessionPanelHeight 为 null，会话区回弹性态", () => {
    usePreferenceStore.setState({ sessionPanelHeight: 300 });
    renderSidebar();
    const handle = getHandle();
    const sessionDiv = getSessionDiv();

    act(() => {
      fireEvent.doubleClick(handle);
    });

    expect(usePreferenceStore.getState().sessionPanelHeight).toBe(null);
    expect(sessionDiv.style.flexGrow).toBe("1");
    expect(sessionDiv.style.flexBasis).toBe("0%");
    expect(sessionDiv.style.height).toBe("");
  });
});
