/**
 * src/store/budgetStore.ts — 预算额度态（按 provider 分账）。
 *
 * 真相源：后端 BUDGET_STATUS 事件（BudgetLedger.get_status → BudgetView[]）。
 * 本 store 只持有 budgets 列表 + loading，发送/查询逻辑在 BackendProvider（与 dashboardStore 一致）。
 */

import { create } from "zustand";
import type { BudgetView } from "../types/api";

interface BudgetState {
  budgets: BudgetView[];
  loading: boolean;
  setBudgets: (b: BudgetView[]) => void;
  setLoading: (v: boolean) => void;
}

export const useBudgetStore = create<BudgetState>((set) => ({
  budgets: [],
  loading: false,
  setBudgets: (b) => set({ budgets: b, loading: false }),
  setLoading: (v) => set({ loading: v }),
}));
