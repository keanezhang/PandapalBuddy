/**
 * src/components/CredentialGate.tsx
 *
 * 凭据门禁：在 sidecar 启动**之前**检查 LLM 凭据是否已配置。
 *
 * 核心设计（与"所有配置就绪后再启动 sidecar"原则匹配）：
 *   1. 挂载时调 `check_llm_credentials`（Rust 直接读 toml，不依赖 sidecar）
 *   2. 无配置 → 命令式 navigate 到 /model-config（配置向导，Rust 直接写 toml）
 *   3. 有配置 → 调 `start_sidecar` 启动 sidecar → 等 backend-ready → 放行
 *
 * 关键设计决策：跳转用命令式 navigate()（在 effect 里），不用声明式 <Navigate />。
 *   原因：<Navigate /> 在渲染时同步触发，会用到过期的 phase 状态。
 *   当用户在 /model-config 保存凭据后 navigate("/") 回来时，React 复用组件实例，
 *   phase 可能残留 "unconfigured"，<Navigate /> 会用这个过期状态再次跳转 → 死循环。
 *   命令式 navigate() 只在 checkAndStart 刚刚确认"确实未配置"时触发，不存在过期问题。
 *
 * 依赖关系（关键不变式）：
 *   本组件只在 WorkspaceGate 内部渲染（工作区已选定），因此 Rust 能定位
 *   toml 路径（<workspace>/.pandapal/users/<uid>/llm_credentials.toml）。
 *   sidecar 只在本组件判定"有配置"后才启动，保证 _build_blueprint 不会因
 *   无凭据崩溃。
 *
 * bypassCredentialCheck：用于配置向导页面自身（/model-config），避免死循环。
 */

import { useEffect, useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { invoke } from "@tauri-apps/api/core";
import { useCredentialStore } from "../store/credentialStore";
import { useConnectionStore } from "../store/connectionStore";
import { useWorkspaceStore } from "../store/workspaceStore";
import { GateScreen, GateLoading } from "./ui";

type Phase = "checking" | "starting" | "ready" | "error";

/**
 * sidecar 从 spawn 到 PANDAPAL_READY 的等待上限。
 * 取 60s：冷启动要跑完 schema 迁移 + blueprint 装配 + relay 连接（观测到约 4s），
 * 留足余量的同时保证异常时用户不会无限期干等。
 */
const SIDECAR_START_TIMEOUT_MS = 60_000;

interface CredentialGateProps {
  children: React.ReactNode;
  /** 绕过凭据门禁（用于向导页面自身，避免死循环） */
  bypassCredentialCheck?: boolean;
}

export function CredentialGate({ children, bypassCredentialCheck = false }: CredentialGateProps) {
  const { t } = useTranslation();
  const checkLocal = useCredentialStore((s) => s.checkLocal);
  const loadCatalog = useCredentialStore((s) => s.loadCatalog);
  const connStatus = useConnectionStore((s) => s.status);
  const connError = useConnectionStore((s) => s.errorMessage);
  const workspaceCurrent = useWorkspaceStore((s) => s.current);
  const navigate = useNavigate();
  const [phase, setPhase] = useState<Phase>("checking");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  // 挂载时拉取系统预置 provider 元信息（Rust 命令直读 toml，不依赖 sidecar）。
  // catalog 是静态系统配置，首次配置场景（sidecar 未启动）也能拉到，
  // 确保凭据表单 provider 下拉在任何阶段（含 /model-config 向导）都能渲染。
  useEffect(() => {
    void loadCatalog();
  }, [loadCatalog]);

  // 检查 toml + 启动 sidecar 的完整流程
  // connectionStore 由 BackendProvider 单一管理，本组件只读不写。
  // 旧 sidecar 被 kill 后的状态残留由 BackendProvider.setup() 的
  // warm-start 探测自行重置，无需本组件越权干涉。
  const checkAndStart = useCallback(async () => {
    setPhase("checking");
    setErrorMsg(null);
    try {
      const configured = await checkLocal();
      if (!configured) {
        // 命令式跳转（在 effect 里执行，不用 <Navigate />）：
        // 只在刚刚确认"确实未配置"时触发，不存在过期状态问题。
        navigate("/model-config", { replace: true });
        return;
      }
      // 有配置 → 启动 sidecar（toml 已就绪，_build_blueprint 不会崩）
      setPhase("starting");
      await invoke("start_sidecar");
      // backend-ready 事件由 BackendProvider 监听，会设置 connStatus="connected"
      // 本组件通过 connStatus 变化感知 sidecar 就绪（见下方 effect）
    } catch (e) {
      const msg = typeof e === "string" ? e : String(e);
      console.error("[credential-gate] check/start failed:", msg);
      setErrorMsg(msg);
      setPhase("error");
    }
  }, [checkLocal, navigate]);

  // 挂载时 + 工作区切换时（workspaceCurrent 变化）触发检查 + 启动
  useEffect(() => {
    if (bypassCredentialCheck) return;
    void checkAndStart();
  }, [bypassCredentialCheck, checkAndStart, workspaceCurrent]);

  // 监听连接状态：starting → connected 即放行；closed/error 即报错
  useEffect(() => {
    if (bypassCredentialCheck) return;
    if (phase !== "starting") return;
    if (connStatus === "connected") {
      setPhase("ready");
    } else if (connStatus === "closed" || connStatus === "error") {
      setErrorMsg(connError ?? t("credentialGate.startFailed"));
      setPhase("error");
    }
  }, [bypassCredentialCheck, phase, connStatus, connError, t]);

  // 启动超时兜底：sidecar 起来了但迟迟不发 PANDAPAL_READY（启动中崩溃、DB 被
  // 锁、卡在某个子系统）时，connStatus 既不会变 connected 也不会变 closed/error，
  // 上面的 effect 永远等不到出口 —— 用户对着转圈无限期干等，连重试按钮都没有
  // （重试只在 error 态渲染）。给一个明确的超时出口，把死等变成可操作的失败。
  useEffect(() => {
    if (bypassCredentialCheck) return;
    if (phase !== "starting") return;
    const timer = setTimeout(() => {
      setErrorMsg(
        t("credentialGate.startTimeout", { seconds: SIDECAR_START_TIMEOUT_MS / 1000 }),
      );
      setPhase("error");
    }, SIDECAR_START_TIMEOUT_MS);
    return () => clearTimeout(timer);
  }, [bypassCredentialCheck, phase, t]);

  // bypass 模式（配置向导自身）：直接放行
  if (bypassCredentialCheck) {
    return <>{children}</>;
  }

  if (phase === "checking") {
    return <GateLoading text={t("credentialGate.checking")} icon="🔑" />;
  }

  if (phase === "starting") {
    return <GateLoading text={t("credentialGate.starting")} />;
  }

  if (phase === "error") {
    return (
      <GateScreen>
        <div style={{ fontSize: "var(--icon-empty-lg)" }}>⚠️</div>
        <p className="gate-error-title">{t("credentialGate.startFailed")}</p>
        {errorMsg && <p className="gate-error-detail">{errorMsg}</p>}
        <button className="gate-btn" onClick={() => void checkAndStart()}>
          {t("credentialGate.retry")}
        </button>
      </GateScreen>
    );
  }

  // phase === "ready"：sidecar 已就绪，放行子组件
  return <>{children}</>;
}
