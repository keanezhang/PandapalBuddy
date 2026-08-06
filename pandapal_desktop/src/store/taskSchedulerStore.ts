/**
 * src/store/taskSchedulerStore.ts
 *
 * 定时任务调度 Store（D1 Pull + D2 Push）。
 *
 * 消息流：
 *   D1 Pull：前端发 REQUEST_SCHEDULED_TASKS → Python 回 SCHEDULED_TASK_LIST
 *   D2 Push：TaskScheduler 变更 → Python 主动推 SCHEDULED_TASK_LIST 或 SCHEDULED_TASK_CHANGED
 *
 * 数据类型对齐后端 ScheduledTaskItem（扁平 wire format）。
 */

import { create } from "zustand";
import type { ScheduledTaskItem } from "../types/api";

// ── 状态
interface TaskSchedulerState {
  tasks: ScheduledTaskItem[];
  loading: boolean;

  // 列表数据
  setLoading: (loading: boolean) => void;

  // D2 Push：全量替换
  replaceAll: (tasks: ScheduledTaskItem[]) => void;

  // D2 Push：增量变更
  upsertTask: (task: ScheduledTaskItem) => void;
  removeTask: (taskId: string) => void;

  // 详情面板选中态（方案A：右侧380px滑入面板）
  selectedTaskId: string | null;
  selectTask: (taskId: string) => void;
  clearSelection: () => void;
}

export const useTaskSchedulerStore = create<TaskSchedulerState>((set, get) => ({
  tasks: [],
  loading: false,

  setLoading: (loading) => set({ loading }),

  replaceAll: (tasks) => set({ tasks, loading: false }),

  upsertTask: (task) => {
    const tasks = [...get().tasks];
    const idx = tasks.findIndex((t) => t.task_id === task.task_id);
    if (idx >= 0) {
      tasks[idx] = task;
    } else {
      tasks.push(task);
    }
    set({ tasks });
  },

  removeTask: (taskId) => {
    set({ tasks: get().tasks.filter((t) => t.task_id !== taskId) });
  },

  selectedTaskId: null,
  selectTask: (taskId) => set({ selectedTaskId: taskId }),
  clearSelection: () => set({ selectedTaskId: null }),
}));
