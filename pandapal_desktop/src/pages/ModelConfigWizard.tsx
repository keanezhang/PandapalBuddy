/**
 * src/pages/ModelConfigWizard.tsx
 *
 * 首次模型配置向导（全屏页面）。
 *
 * 职责：
 *   1. 引导终端用户完成第一组 LLM 凭据录入
 *   2. 支持添加多组 provider 凭据
 *   3. 逐组连通性校验
 *   4. 指定默认服务商
 *   5. 校验通过后跳转工作区
 *
 * 仅在「配置门禁判定为未配置」时展示，不提供跳过入口。
 */

import { useState, useCallback, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { CredentialForm } from "../components/CredentialForm";
import {
  useCredentialStore,
  emptyCredential,
  addableProviders,
  toSubmittable,
  type ProviderCredential,
  type LLMProvider,
} from "../store/credentialStore";
import { useAuthStore } from "../store/authStore";

// ── 样式常量 ─────────────────────────────────────────────────────────────

const S = {
  // body 全局 overflow:hidden，本页必须自己成为滚动容器：
  // 固定 height（而非 minHeight）才能产生滚动条
  page: {
    height: "100vh",
    background: "var(--bg-root)",
    padding: "40px 24px 80px",
    boxSizing: "border-box" as const,
    overflowY: "auto" as const,
  } as React.CSSProperties,

  container: {
    width: "100%",
    maxWidth: 600,
    margin: "0 auto",
  } as React.CSSProperties,

  header: {
    textAlign: "center" as const,
    marginBottom: "var(--space-8)",
  } as React.CSSProperties,

  logo: {
    fontSize: 48,
    marginBottom: "var(--space-3)",
  } as React.CSSProperties,

  title: {
    fontSize: "var(--text-2xl)",
    fontWeight: 600,
    color: "var(--text-primary)",
    margin: "0 0 var(--space-2)",
  } as React.CSSProperties,

  subtitle: {
    fontSize: "var(--text-md)",
    color: "var(--text-secondary)",
    lineHeight: 1.6,
    margin: 0,
  } as React.CSSProperties,

  stepIndicator: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    gap: "var(--space-2)",
    marginBottom: "var(--space-6)",
  } as React.CSSProperties,

  stepDot: {
    width: 8,
    height: 8,
    borderRadius: "50%",
    background: "var(--border-default)",
    transition: "background var(--duration-fast)",
  } as React.CSSProperties,

  stepDotActive: {
    background: "var(--accent)",
    boxShadow: "0 0 0 3px rgba(124,58,237,0.15)",
  } as React.CSSProperties,

  stepDotDone: {
    background: "var(--success)",
  } as React.CSSProperties,

  addBtn: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    gap: "var(--space-2)",
    width: "100%",
    padding: "10px 16px",
    fontSize: "var(--text-sm)",
    fontFamily: "inherit",
    fontWeight: 500,
    color: "var(--text-secondary)",
    background: "transparent",
    border: "1px dashed var(--border-default)",
    borderRadius: "var(--radius-md)",
    cursor: "pointer",
    marginTop: "var(--space-2)",
    marginBottom: "var(--space-5)",
    transition: "border-color var(--duration-fast), color var(--duration-fast)",
  } as React.CSSProperties,

  addBtnDisabled: {
    opacity: 0.35,
    cursor: "not-allowed",
  } as React.CSSProperties,

  submitArea: {
    display: "flex",
    flexDirection: "column" as const,
    alignItems: "center",
    gap: "var(--space-3)",
    marginTop: "var(--space-6)",
  } as React.CSSProperties,

  submitBtn: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    gap: "var(--space-2)",
    width: "100%",
    maxWidth: 320,
    padding: "14px 28px",
    fontSize: "var(--text-md)",
    fontFamily: "inherit",
    fontWeight: 600,
    color: "#fff",
    background: "var(--accent)",
    border: "none",
    borderRadius: "var(--radius-md)",
    cursor: "pointer",
    transition: "all var(--duration-fast)",
  } as React.CSSProperties,

  submitBtnDisabled: {
    opacity: 0.5,
    cursor: "not-allowed",
  } as React.CSSProperties,

  submitBtnLoading: {
    opacity: 0.7,
    cursor: "wait",
  } as React.CSSProperties,

  hint: {
    fontSize: "var(--text-xs)",
    color: "var(--text-tertiary)",
    textAlign: "center" as const,
    lineHeight: 1.5,
    maxWidth: 400,
  } as React.CSSProperties,

  logoutBtn: {
    marginTop: "var(--space-8)",
    padding: "6px 16px",
    fontSize: "var(--text-xs)",
    fontFamily: "inherit",
    color: "var(--text-tertiary)",
    background: "transparent",
    border: "1px solid var(--border-default)",
    borderRadius: "var(--radius-sm)",
    cursor: "pointer",
    transition: "all var(--duration-fast)",
  } as React.CSSProperties,

  globalError: {
    fontSize: "var(--text-sm)",
    color: "var(--danger)",
    textAlign: "center" as const,
    marginBottom: "var(--space-3)",
  } as React.CSSProperties,

  userInfo: {
    position: "fixed" as const,
    top: 16,
    right: 20,
    display: "flex",
    alignItems: "center",
    gap: "var(--space-2)",
    fontSize: "var(--text-sm)",
    color: "var(--text-tertiary)",
  } as React.CSSProperties,

  avatar: {
    width: 28,
    height: 28,
    borderRadius: "var(--radius-full)",
    background: "linear-gradient(135deg, var(--accent), #5B21B6)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontSize: 12,
    fontWeight: 700,
    color: "#fff",
  } as React.CSSProperties,
};

// ── 组件 ─────────────────────────────────────────────────────────────────

export function ModelConfigWizard() {
  const navigate = useNavigate();
  const username = useAuthStore((s) => s.username);
  const logout = useAuthStore((s) => s.logout);

  const verifyStatus = useCredentialStore((s) => s.verifyStatus);
  const verifyResults = useCredentialStore((s) => s.verifyResults);
  const saveLocal = useCredentialStore((s) => s.saveLocal);
  const saving = useCredentialStore((s) => s.saving);
  const saveError = useCredentialStore((s) => s.saveError);
  const providerCatalog = useCredentialStore((s) => s.providerCatalog);
  const loadCatalog = useCredentialStore((s) => s.loadCatalog);
  const loadModelPrices = useCredentialStore((s) => s.loadModelPrices);
  const recommendedModels = useCredentialStore((s) => s.recommendedModels);

  // 本地编辑态凭据列表。
  // ⚠️ 初始为空：不能拿 emptyCredential("dashscope") 当种子。
  //    硬编码 provider id 会让「新增/移除 provider 只改 provider_catalog.toml 一处」
  //    这个目标失效——catalog 里没了 dashscope，向导仍会以它开局（PRD Story 5 / G5）。
  const [localCreds, setLocalCreds] = useState<ProviderCredential[]>([]);

  const [globalError, setGlobalError] = useState<string | null>(null);

  // 系统配置：catalog（provider 下拉）+ 默认单价表（combobox 推荐与默认价）
  useEffect(() => {
    loadCatalog();
    loadModelPrices();
  }, [loadCatalog, loadModelPrices]);

  // 进入向导时清掉上一处（设置面板）留下的校验结果与保存错误。
  // 二者都是 store 级全局状态：本页没有校验入口却按下标渲染 verifyResults[i]，
  // 会把无关凭据的红色报错画到第 i 张卡上；saveError 则会以「保存失败：…」
  // 的形式出现在首次配置的欢迎界面上。
  useEffect(() => {
    useCredentialStore.getState().resetVerify();
    useCredentialStore.getState().clearSaveError();
  }, []);

  // catalog 到位后再播下第一张卡片，provider 取**目录首项**（PRD §4.3.1-1）
  useEffect(() => {
    if (localCreds.length > 0 || providerCatalog.length === 0) return;
    setLocalCreds([
      { ...emptyCredential(providerCatalog[0].id), is_default: true },
    ]);
  }, [providerCatalog, localCreds.length]);

  // 主键 = (provider, model_id)：同一 provider 可加多个模型，候选即目录全量
  const availableToAdd = addableProviders(providerCatalog);

  /** 每条主键，用于卡片间即时查重（R4） */
  const credKeys = localCreds.map((c) => `${c.provider}::${c.model_id.trim()}`);

  // 检查是否所有必填字段都已填写（含单价三级回落的可保存性）
  const allFilled =
    localCreds.length > 0 &&
    localCreds.every((c) => {
      if (!c?.provider) return false;
      if (typeof c.api_key !== "string" || c.api_key.trim().length < 8) return false;
      if (typeof c.model_id !== "string" || c.model_id.trim().length === 0) return false;
      if ((c.input_price_per_1k == null) !== (c.output_price_per_1k == null)) return false;
      if (
        [c.input_price_per_1k, c.output_price_per_1k, c.cache_read_price_per_1k].some(
          (v) => v != null && v < 0,
        )
      ) {
        return false;
      }
      // 三级回落第③级：无任何单价来源 → 拒绝保存（绝不在前端补 0，§九）
      const hasUserPrice = c.input_price_per_1k != null && c.output_price_per_1k != null;
      const hasSystemPrice = recommendedModels.some(
        (p) => p.model_id === c.model_id.trim(),
      );
      return hasUserPrice || hasSystemPrice;
    });

  // R4：model_id 即路由键，跨 provider 同名也会坍缩，一并拒绝
  const hasDuplicate =
    new Set(credKeys).size !== credKeys.length ||
    new Set(localCreds.map((c) => c.model_id.trim())).size !== localCreds.length;

  // 有且仅有一组默认
  const hasDefault = localCreds.some((c) => c.is_default);

  const canSubmit =
    allFilled &&
    hasDefault &&
    !hasDuplicate &&
    !saving &&
    localCreds.length >= 1;

  // 添加一个模型（provider 可与已有卡片相同）
  const handleAdd = useCallback(
    (provider: LLMProvider) => {
      setLocalCreds((prev) => [
        ...prev,
        { ...emptyCredential(provider), is_default: false },
      ]);
      setGlobalError(null);
    },
    [],
  );

  // 删除某组
  const handleDelete = useCallback(
    (index: number) => {
      setLocalCreds((prev) => {
        if (prev.length <= 1) return prev; // 至少保留一组
        const next = [...prev];
        next.splice(index, 1);
        // 如果删除的是默认组，将第一组设为默认
        if (prev[index].is_default && next.length > 0) {
          next[0] = { ...next[0], is_default: true };
        }
        return next;
      });
      setGlobalError(null);
    },
    [],
  );

  // 更新某组
  const handleChange = useCallback(
    (index: number, cred: ProviderCredential) => {
      setLocalCreds((prev) => {
        const next = [...prev];
        next[index] = cred;
        return next;
      });
      setGlobalError(null);
    },
    [],
  );

  // 设置默认组
  const handleSetDefault = useCallback((index: number) => {
    setLocalCreds((prev) =>
      prev.map((c, i) => ({ ...c, is_default: i === index })),
    );
  }, []);

  // 保存凭据（Rust 直接写 toml，不依赖 sidecar）
  // 连通性校验延后到 sidecar 启动后（设置页面），配置界面只做格式校验 + 保存。
  const handleSubmit = useCallback(async () => {
    setGlobalError(null);
    try {
      // 向导场景全部是新建卡片，key 必然是用户刚填的明文；走同一条提交构造，
      // 保证「脱敏值绝不进提交体」这条规则只有一个实现（R2/R3）。
      await saveLocal(localCreds.map((c) => toSubmittable(c, true)));
      // 保存成功 → 跳 /，CredentialGate 会检查 toml → 有配置 → 启动 sidecar
      navigate("/", { replace: true });
    } catch (e) {
      const msg = typeof e === "string" ? e : String(e);
      setGlobalError(msg);
    }
  }, [localCreds, saveLocal, navigate]);

  return (
    <div style={S.page}>
      {/* 右上角用户信息 */}
      {username && (
        <div style={S.userInfo}>
          <div style={S.avatar}>{(username ?? "?")[0].toUpperCase()}</div>
          <span>{username}</span>
        </div>
      )}

      <div style={S.container}>
        {/* ── 头部 ── */}
        <div style={S.header}>
          <div style={S.logo}>🔑</div>
          <h1 style={S.title}>配置模型服务</h1>
          <p style={S.subtitle}>
            PandaPal 需要您的模型服务凭据才能运行。
            <br />
            请输入您的 API Key，凭据将安全存储在本机，不会上传。
          </p>
        </div>

        {/* ── 全局错误 ── */}
        {globalError && <div style={S.globalError}>{globalError}</div>}

        {/* ── 系统目录未就绪：fail-closed 且可感知，不静默灰化（PRD AC-09）── */}
        {providerCatalog.length === 0 && (
          <div style={S.globalError}>
            系统配置加载中或已损坏。若持续如此，请重新安装以修复
            provider_catalog.toml。
          </div>
        )}

        {/* ── 凭据表单列表 ── */}
        {localCreds.map((cred, i) => (
          <CredentialForm
            key={`wizard-${i}`}
            credential={cred}
            onChange={(c) => handleChange(i, c)}
            onDelete={localCreds.length > 1 ? () => handleDelete(i) : undefined}
            verifyResult={verifyResults[i]}
            verifying={verifyStatus === "verifying"}
            isDefault={cred.is_default}
            onSetDefault={() => handleSetDefault(i)}
            usedKeys={credKeys.filter((_, j) => j !== i)}
            showProviderSelect={true}
            index={i}
            providerCatalog={providerCatalog}
            // 向导场景全部是新建卡片：api_key 直接输入，无脱敏只读态
            isSaved={false}
          />
        ))}

        {/* ── 添加模型（同一 provider 可加多个）── */}
        {availableToAdd.length > 0 && !saving && (
          <div style={{ position: "relative" }}>
            <button
              type="button"
              style={{
                ...S.addBtn,
                ...(availableToAdd.length === 0 ? S.addBtnDisabled : {}),
              }}
              disabled={availableToAdd.length === 0}
              onClick={() => {
                if (availableToAdd.length > 0) {
                  handleAdd(availableToAdd[0]);
                }
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.borderColor = "var(--accent)";
                e.currentTarget.style.color = "var(--accent)";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.borderColor = "var(--border-default)";
                e.currentTarget.style.color = "var(--text-secondary)";
              }}
            >
              + 添加模型
            </button>
          </div>
        )}

        {/* ── 提交区域 ── */}
        <div style={S.submitArea}>
          <button
            type="button"
            style={{
              ...S.submitBtn,
              ...(!canSubmit ? S.submitBtnDisabled : {}),
              ...(saving ? S.submitBtnLoading : {}),
            }}
            disabled={!canSubmit}
            onClick={() => void handleSubmit()}
            onMouseEnter={(e) => {
              if (canSubmit) {
                e.currentTarget.style.background = "var(--accent-soft)";
                e.currentTarget.style.transform = "translateY(-1px)";
              }
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.background = "var(--accent)";
              e.currentTarget.style.transform = "none";
            }}
          >
            {saving
              ? "正在保存…"
              : "保存并继续"}
          </button>

          {!allFilled && (
            <p style={S.hint}>
              请填写所有必填字段（API Key、模型 ID）；无系统默认价的模型还需填写单价
            </p>
          )}
          {hasDuplicate && (
            <p style={{ ...S.hint, color: "var(--danger)" }}>
              存在重复的模型 ID —— 模型 ID 即路由键，重复会导致路由错配
            </p>
          )}
          {(saveError || globalError) && (
            <p style={{ ...S.hint, color: "var(--danger)" }}>
              保存失败：{saveError || globalError}
            </p>
          )}
        </div>

        {/* ── 退出登录 ── */}
        <div style={{ textAlign: "center" }}>
          <button
            type="button"
            style={S.logoutBtn}
            onClick={() => logout()}
            onMouseEnter={(e) => {
              e.currentTarget.style.borderColor = "var(--danger)";
              e.currentTarget.style.color = "var(--danger)";
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.borderColor = "var(--border-default)";
              e.currentTarget.style.color = "var(--text-tertiary)";
            }}
          >
            退出登录
          </button>
        </div>
      </div>
    </div>
  );
}
