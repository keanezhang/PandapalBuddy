/**
 * settingsPanelCrash.test.tsx
 *
 * 复现「设置面板 → 模型服务 → 点击推荐模型选项后崩溃 / 设置面板自动关闭」。
 *
 * 测试方法：**真实渲染** SettingsPanel → ModelServiceSettings → CredentialForm
 * （组件零 mock），只 mock Tauri IPC 层（jsdom 无 Tauri 运行时）。
 *
 * 判据（对应症状）：
 *   1. 点击 4 个推荐项任一 → 不应抛渲染异常（测试失败即崩溃证据）
 *   2. onClose 不应被调用（面板不应自动关闭）
 *   3. 选择应生效（input value 更新、价格区切换为系统默认价只读展示）
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, fireEvent, act, waitFor } from "@testing-library/react";
import { SettingsPanel } from "../SettingsPanel";
import {
  useCredentialStore,
  type ProviderCredential,
} from "../../store/credentialStore";
import type { ModelPriceEntry, ProviderMeta } from "../../types/api";
import "../../i18n"; // i18next init（zh-CN）

// ── Tauri IPC mock（vi.hoisted：避免 mock factory 闭包引用 TDZ）──
const { mockInvoke, DEEPSEEK_MODELS, DEEPSEEK_META } = vi.hoisted(() => {
  const DEEPSEEK_MODELS: ModelPriceEntry[] = [
    { model_id: "deepseek-chat", provider: "deepseek", input_price_per_1k: 0.002, output_price_per_1k: 0.008, cache_read_price_per_1k: 0.0005 },
    { model_id: "deepseek-reasoner", provider: "deepseek", input_price_per_1k: 0.004, output_price_per_1k: 0.016, cache_read_price_per_1k: 0.001 },
    { model_id: "deepseek-v4-pro", provider: "deepseek", input_price_per_1k: 0.003, output_price_per_1k: 0.006, cache_read_price_per_1k: 0.000025 },
    { model_id: "deepseek-v4-flash", provider: "deepseek", input_price_per_1k: 0.001, output_price_per_1k: 0.002, cache_read_price_per_1k: 0.00002 },
  ];
  const DEEPSEEK_META: ProviderMeta[] = [
    {
      id: "deepseek",
      display_name: "DeepSeek",
      default_base_url: "https://api.deepseek.com",
      guide_url: "https://platform.deepseek.com",
    },
  ];
  const mockInvoke = vi.fn();
  return { mockInvoke, DEEPSEEK_MODELS, DEEPSEEK_META };
});

vi.mock("@tauri-apps/api/core", () => ({ invoke: mockInvoke }));

/** 预置 store：1 条 deepseek 凭据 + 4 个推荐模型（与 model_prices.toml 对齐） */
function seedStore(cred: ProviderCredential) {
  useCredentialStore.setState({
    credentials: [cred],
    providerCatalog: DEEPSEEK_META,
    catalogReady: true,
    catalogLoading: false,
    recommendedModels: DEEPSEEK_MODELS,
    pricesReady: true,
    pricesLoading: false,
    pricesError: null,
    exchangeRateUsd: 7.2,
    saving: false,
    saveError: null,
    verifyStatus: "idle",
    verifyResults: [],
    loading: false,
  });
  mockInvoke.mockImplementation(async (cmd: string) => {
    if (cmd === "get_model_prices") {
      return { prices: DEEPSEEK_MODELS, exchange_rate_usd: 7.2 };
    }
    if (cmd === "get_provider_catalog") {
      return { providers: DEEPSEEK_META };
    }
    return {};
  });
}

describe("设置面板 · 模型服务 · 点击推荐模型选项", () => {
  /** 空模型凭据（未选模型，等价用户刚配置时的卡片） */
  const freshCred: ProviderCredential = {
    provider: "deepseek",
    api_key: "sk-••••••••••••",
    model_id: "",
    is_default: true,
  };
  /** 已保存的凭据（选过 v4-flash，等价第一轮「保存配置后」的状态） */
  const savedCred: ProviderCredential = {
    provider: "deepseek",
    api_key: "sk-••••••••••••",
    model_id: "deepseek-v4-flash",
    is_default: true,
  };

  beforeEach(() => {
    seedStore(freshCred);
  });

  async function switchToModelTab() {
    const tabs = Array.from(
      document.querySelectorAll<HTMLButtonElement>(".modal-header button"),
    );
    const modelTab = tabs.find((b) => b.textContent?.includes("🔑"));
    expect(modelTab, "「模型服务」tab 应存在").toBeTruthy();
    await act(async () => {
      fireEvent.click(modelTab!);
    });
  }

  async function getCombobox() {
    await waitFor(() => {
      expect(document.querySelector('input[role="combobox"]')).not.toBeNull();
    });
    return document.querySelector<HTMLInputElement>('input[role="combobox"]')!;
  }

  it("点击 4 个推荐项任一：不崩溃、面板不关闭、选择生效", async () => {
    const onClose = vi.fn();
    render(<SettingsPanel onClose={onClose} />);
    await switchToModelTab();
    const combobox = await getCombobox();

    // 打开推荐列表
    await act(async () => {
      fireEvent.focus(combobox);
    });
    let items = document.querySelectorAll(".cred-combo-item");
    expect(items.length).toBe(4);
    const modelIds = Array.from(items).map(
      (el) => el.querySelector("span")?.textContent ?? "",
    );
    expect(modelIds).toEqual([
      "deepseek-chat",
      "deepseek-reasoner",
      "deepseek-v4-pro",
      "deepseek-v4-flash",
    ]);

    // 逐个点击 4 个选项。
    // 注意：点选后 model_id 有值 → comboCandidates 过滤只剩 1 项（真实行为），
    // 每轮先清空输入再点下一个，模拟用户从空输入开始点选。
    for (let round = 0; round < modelIds.length; round++) {
      await act(async () => {
        fireEvent.change(combobox, { target: { value: "" } }); // 清空 → 列表回到全量
        fireEvent.focus(combobox); // 展开列表
      });
      const currentItems = Array.from(
        document.querySelectorAll<HTMLElement>(".cred-combo-item"),
      );
      const el = currentItems.find(
        (n) => n.querySelector("span")?.textContent === modelIds[round],
      );
      expect(el, `第 ${round + 1} 轮应找到 ${modelIds[round]}`).toBeTruthy();
      await act(async () => {
        fireEvent.mouseDown(el!);
      });
      expect(combobox.value, `点选 ${modelIds[round]} 后 input 应更新`).toBe(
        modelIds[round],
      );
      expect(onClose, "面板不应因点选而关闭").not.toHaveBeenCalled();
      expect(document.querySelector(".cred-card")).not.toBeNull();
    }
  });

  it("点选后价格区从「待补价」切换为「系统默认价」只读展示", async () => {
    const onClose = vi.fn();
    render(<SettingsPanel onClose={onClose} />);
    await switchToModelTab();
    const combobox = await getCombobox();

    await act(async () => {
      fireEvent.focus(combobox);
    });
    const target = Array.from(
      document.querySelectorAll<HTMLElement>(".cred-combo-item"),
    ).find((el) => el.textContent?.includes("deepseek-v4-pro"));
    expect(target).toBeTruthy();
    await act(async () => {
      fireEvent.mouseDown(target!);
    });

    expect(combobox.value).toBe("deepseek-v4-pro");
    // 只读价格展示（系统默认价 badge）
    await waitFor(() => {
      expect(
        Array.from(document.querySelectorAll(".cred-badge")).some((b) =>
          b.textContent?.includes("系统默认价"),
        ),
      ).toBe(true);
    });
    // 单价输入框消失（只读态）
    expect(document.querySelector('.cred-price-cell input[type="number"]')).toBeNull();
  });

  it("保存成功后（后端确认回填凭据）再点击推荐项：不崩溃、面板不关闭", async () => {
    seedStore(savedCred);
    const onClose = vi.fn();
    render(<SettingsPanel onClose={onClose} />);
    await switchToModelTab();
    const combobox = await getCombobox();

    // 模拟保存成功后用户再次打开列表并点选（dirty=false 时 sync 已回填）。
    // 已选过 v4-flash → 列表按输入过滤只剩 1 项 → 先清空再点选新模型。
    await act(async () => {
      fireEvent.change(combobox, { target: { value: "" } });
      fireEvent.focus(combobox);
    });
    const items = Array.from(
      document.querySelectorAll<HTMLElement>(".cred-combo-item"),
    );
    expect(items.length).toBe(4);
    const target = items.find((el) => el.textContent?.includes("deepseek-chat"));
    expect(target).toBeTruthy();
    await act(async () => {
      fireEvent.mouseDown(target!);
    });
    expect(combobox.value).toBe("deepseek-chat");
    expect(onClose).not.toHaveBeenCalled();
    expect(document.querySelector(".cred-card")).not.toBeNull();
  });

  it("点击推荐项时调用 stopPropagation 阻止 mousedown 冒泡到 document", async () => {
    // 真实浏览器里 mousedown 会沿「dispatch 开始时构建的完整路径」冒泡到 document，
    // 而 handlePickRecommended 同步卸载了被点项 → SettingsPanel 的 document 级
    // 「点外部关闭」检测里 contains(e.target) 返回 false → 误关面板。
    // 修复手段：onMouseDown 里 e.stopPropagation()（会调用 nativeEvent.stopPropagation）。
    const onClose = vi.fn();
    const stopSpy = vi.spyOn(MouseEvent.prototype, "stopPropagation");
    try {
      render(<SettingsPanel onClose={onClose} />);
      await switchToModelTab();
      const combobox = await getCombobox();
      await act(async () => { fireEvent.focus(combobox); });
      const target = document.querySelector(".cred-combo-item");
      expect(target).toBeTruthy();

      stopSpy.mockClear();
      await act(async () => { fireEvent.mouseDown(target!); });
      expect(stopSpy).toHaveBeenCalled();
    } finally {
      stopSpy.mockRestore();
    }
  });

  it("点击面板外部仍正常关闭（防御检查不破坏「点外部关闭」）", async () => {
    const onClose = vi.fn();
    render(<SettingsPanel onClose={onClose} />);
    // 等 useEffect 里 setTimeout(0) 把 document 级 mousedown listener 注册上
    await new Promise((r) => setTimeout(r, 20));
    await act(async () => {
      document.body.dispatchEvent(
        new MouseEvent("mousedown", { bubbles: true, cancelable: true }),
      );
    });
    expect(onClose).toHaveBeenCalled();
  });
});
