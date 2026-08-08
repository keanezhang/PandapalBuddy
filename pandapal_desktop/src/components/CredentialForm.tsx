/**
 * src/components/CredentialForm.tsx
 *
 * 可复用的单组 Provider 凭据表单。
 * 同时用于「首次配置向导」和「设置页·模型服务」。
 *
 * 提供：provider 选择、API Key 输入（已保存者脱敏只读 + 「更换密钥」）、
 * model_id combobox（推荐清单 + **任意手填**）、单价填写（CNY / 1k token）、
 * Base URL（占位提示默认值）、默认组标记、删除按钮。
 */

import { useState, useCallback, useMemo } from "react";
import { useTranslation } from "react-i18next";
import {
  useCredentialStore,
  getProviderMeta,
  emptyCredential,
  findSystemPrice,
  recommendedForProvider,
  isMaskedKey,
  type ProviderCredential,
  type LLMProvider,
} from "../store/credentialStore";
import type { ModelPriceEntry, ProviderMeta } from "../types/api";
import type { VerifyResult } from "../store/credentialStore";

// ── Props ─────────────────────────────────────────────────────────────────

interface CredentialFormProps {
  /** 当前凭据数据 */
  credential: ProviderCredential;
  /** 凭据变更回调 */
  onChange: (cred: ProviderCredential) => void;
  /** 删除该组凭据（undefined = 不显示删除按钮） */
  onDelete?: () => void;
  /** 该校验结果（undefined = 未校验 / idle） */
  verifyResult?: VerifyResult;
  /** 是否正在整组校验中（禁用控件） */
  verifying?: boolean;
  /** 是否为默认组 */
  isDefault: boolean;
  /** 设置该组为默认 */
  onSetDefault?: () => void;
  /**
   * 其他卡片已占用的 `(provider, model_id)` 组合，用于即时标红重复。
   *
   * ⚠️ 不再是「已用 provider 列表」：主键已改为 `(provider, model_id)`，
   *    同一 provider 可配 N 个模型（PRD G1），provider 本身不构成冲突。
   */
  usedKeys?: string[];
  /** 是否显示 provider 下拉（向导首组隐藏，仅展示） */
  showProviderSelect?: boolean;
  /** 表单索引（用于显示序号） */
  index?: number;
  /**
   * 系统预置 provider 元信息（从后端 catalog 拉取）。
   * 未传时从 useCredentialStore 读；空数组 = catalog 未拉到，控件 disabled。
   */
  providerCatalog?: ProviderMeta[];
  /**
   * 该卡片是否为「已保存」态。
   * 已保存 → api_key 走脱敏只读 + 「更换密钥」流程（PRD §4.3.1-3）。
   */
  isSaved?: boolean;
  /**
   * 用户是否已点「更换密钥」并在重填。
   * 由父组件持有：它决定提交体带不带 api_key（R3），是保存语义的一部分，
   * 不能只活在卡片内部 state 里。
   */
  keyChanged?: boolean;
  /** 切换「更换密钥」态 */
  onKeyChangedToggle?: (changed: boolean) => void;
}

// ── 样式 ─────────────────────────────────────────────────────────────────
// 样式类定义在 global-v2.css SECTION 31（.cred-*）。
// ⚠️ 覆盖态（--error/--default）一律用整组 border 简写的修饰类叠加：
//    基础类（.cred-card/.cred-select/.cred-input）用 border 简写，修饰类
//    也用 border 简写 → CSS 层叠天然生效，不存在 JS 对象合并时的
//    shorthand/longhand diff 冲突告警。

const CX = (...parts: Array<string | false | null | undefined>) =>
  parts.filter(Boolean).join(" ");

// ── 组件 ─────────────────────────────────────────────────────────────────

export function CredentialForm({
  credential,
  onChange,
  onDelete,
  verifyResult,
  verifying,
  isDefault,
  onSetDefault,
  usedKeys = [],
  showProviderSelect = true,
  index,
  providerCatalog,
  isSaved = false,
  keyChanged = false,
  onKeyChangedToggle,
}: CredentialFormProps) {
  const { t } = useTranslation();
  const [showKey, setShowKey] = useState(false);
  const [comboOpen, setComboOpen] = useState(false);

  // ⚠️ Hook 必须无条件调用（Rules of Hooks）。
  // 旧写法 `providerCatalog ?? useCredentialStore(...)` 是**条件调用 Hook**：
  // props 传了就跳过 useStore，React 的 hook 序号会在两次渲染间错位。
  // 正确做法是永远订阅，再在值层面二选一。
  const storeCatalog = useCredentialStore((s) => s.providerCatalog);
  const catalog = providerCatalog ?? storeCatalog;
  const catalogReady = catalog.length > 0;

  // 推荐清单 = 系统默认单价表（同一份 model_prices.toml，PRD §4.3.1-2）。
  const recommendedModels = useCredentialStore((s) => s.recommendedModels);
  const pricesError = useCredentialStore((s) => s.pricesError);

  const meta = getProviderMeta(credential.provider, catalog);
  const error = verifyResult?.status === "failed" ? verifyResult.error : null;
  const isVerifying = verifying || verifyResult?.status === "verifying";
  // catalog 未拉到时整体禁用（provider 下拉无选项，用户无法操作）
  const disabled = isVerifying || !catalogReady;

  // provider 候选 = catalog 全量。
  // ⚠️ 刻意不排除「已用过的 provider」：主键是 (provider, model_id)，
  //    同一 provider 下配多个模型是本次重构的核心目标（PRD G1 / Story 1）。
  const availableProviders = catalog;

  // ── model_id 冲突检测（主键 = provider + model_id，R4）──
  const selfKey = `${credential.provider}::${credential.model_id.trim()}`;
  const duplicateKey =
    credential.model_id.trim().length > 0 && usedKeys.includes(selfKey);

  // ── 单价解析（三级回落 R5 的前两级；第三级「拒绝保存」由保存按钮门禁承担）──
  const systemPrice = useMemo(
    () => findSystemPrice(credential.model_id, recommendedModels),
    [credential.model_id, recommendedModels],
  );
  const hasUserPrice =
    credential.input_price_per_1k != null || credential.output_price_per_1k != null;
  // 展示态：用户填了 → 「我填的价」；否则命中系统表 → 「系统默认价」；都没有 → 「待补价」
  const priceSource: "user" | "system" | "missing" = hasUserPrice
    ? "user"
    : systemPrice
      ? "system"
      : "missing";
  // 需要展开可编辑的单价输入框：用户主动覆盖，或压根没有系统默认价可用
  const priceEditable = hasUserPrice || !systemPrice;

  // ── combobox 候选：该 provider 下的推荐模型，按已输入内容过滤 ──
  // ⚠️ 这只是**过滤展示**，绝不过滤「可提交值」——见下方输入框注释（R11）。
  const comboCandidates = useMemo<ModelPriceEntry[]>(() => {
    const forProvider = recommendedForProvider(credential.provider, recommendedModels);
    const q = credential.model_id.trim().toLowerCase();
    if (!q) return forProvider;
    return forProvider.filter((m) => m.model_id.toLowerCase().includes(q));
  }, [credential.provider, credential.model_id, recommendedModels]);
  // 该 provider 完全没有推荐模型时，combobox 退化为纯文本框（不显示空列表）
  const hasRecommendations =
    recommendedForProvider(credential.provider, recommendedModels).length > 0;

  const updateField = useCallback(
    <K extends keyof ProviderCredential>(field: K, value: ProviderCredential[K]) => {
      onChange({ ...credential, [field]: value });
    },
    [credential, onChange],
  );

  const handleProviderChange = useCallback(
    (newProvider: LLMProvider) => {
      onChange({
        ...emptyCredential(newProvider),
        is_default: credential.is_default,
      });
    },
    [credential, onChange],
  );

  /** 选中推荐项：带出 model_id。单价保持留空 = 走系统默认价（R5 第②级）。 */
  const handlePickRecommended = useCallback(
    (entry: ModelPriceEntry) => {
      onChange({
        ...credential,
        model_id: entry.model_id,
        // 清空用户覆盖价：既然是从推荐清单选的，默认就用系统默认价。
        // 用户想覆盖，点「我要自己填」即可（不静默沿用上一个模型的价）。
        input_price_per_1k: undefined,
        output_price_per_1k: undefined,
        cache_read_price_per_1k: undefined,
      });
      setComboOpen(false);
    },
    [credential, onChange],
  );

  /**
   * 单价输入。空串 → undefined（表示「留空」），**不是 0**。
   * §九：金额类字段缺失绝不默认 0——0 会被当成「这个模型免费」，静默吃掉全部费用。
   */
  const handlePriceInput = useCallback(
    (field: "input_price_per_1k" | "output_price_per_1k" | "cache_read_price_per_1k",
     raw: string) => {
      const t = raw.trim();
      if (t === "") {
        onChange({ ...credential, [field]: undefined });
        return;
      }
      const n = Number(t);
      onChange({ ...credential, [field]: Number.isFinite(n) ? n : undefined });
    },
    [credential, onChange],
  );

  /** 「我要自己填」：把系统默认价复制进输入框作为起点，标签转「我填的价」 */
  const handleOverridePrice = useCallback(() => {
    onChange({
      ...credential,
      input_price_per_1k: systemPrice?.input_price_per_1k,
      output_price_per_1k: systemPrice?.output_price_per_1k,
      cache_read_price_per_1k: systemPrice?.cache_read_price_per_1k,
    });
  }, [credential, onChange, systemPrice]);

  /** 「恢复默认价」：清空用户值，回落系统默认表（仅在系统表命中时可见） */
  const handleRestoreDefaultPrice = useCallback(() => {
    onChange({
      ...credential,
      input_price_per_1k: undefined,
      output_price_per_1k: undefined,
      cache_read_price_per_1k: undefined,
    });
  }, [credential, onChange]);

  // ── 单价校验：输入价与输出价必须同填同空（PRD §4.3.2-2）──
  const priceHalfFilled =
    (credential.input_price_per_1k == null) !==
    (credential.output_price_per_1k == null);
  const priceNegative = [
    credential.input_price_per_1k,
    credential.output_price_per_1k,
    credential.cache_read_price_per_1k,
  ].some((v) => v != null && v < 0);

  // 确定卡片的视觉状态
  const cardClass = CX(
    "cred-card",
    error && "cred-card--error",
    isDefault && !error && "cred-card--default",
  );

  // base_url placeholder：catalog 拉到时显示对应 provider 的 default_base_url
  const defaultBaseUrl = meta?.default_base_url ?? "";
  // guide_url：catalog 拉到时显示对应 provider 的获取密钥链接
  const guideUrl = meta?.guide_url ?? "";
  // provider 展示名：catalog 拉到时用 display_name，否则用 id 兜底
  const providerDisplayName = meta?.display_name ?? credential.provider;

  return (
    <div className={cardClass}>
      {/* ── 卡片头部：provider 标签 + 状态徽章 ── */}
      <div className="cred-header">
        <div className="cred-provider-label">
          {index !== undefined && (
            <span style={{ color: "var(--text-muted)", fontWeight: 400 }}>
              #{index + 1}
            </span>
          )}
          {showProviderSelect ? (
            <select
              value={credential.provider}
              onChange={(e) => handleProviderChange(e.target.value as LLMProvider)}
              disabled={disabled}
              className="cred-select"
              style={{
                width: "auto",
                minWidth: 180,
                paddingRight: 28,
                backgroundImage:
                  "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='8' height='5'%3E%3Cpath d='M0 0l4 5 4-5z' fill='rgba(255,255,255,0.35)'/%3E%3C/svg%3E\")", // ui-lint-ok: 内联 SVG data URI 箭头图标色（非 UI 颜色）
                backgroundRepeat: "no-repeat",
                backgroundPosition: "right 10px center",
                backgroundSize: "8px 5px",
              }}
            >
              {availableProviders.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.display_name}
                </option>
              ))}
            </select>
          ) : (
            <>
              <span>{providerDisplayName}</span>
              <span className="cred-badge cred-badge--accent">{credential.provider}</span>
            </>
          )}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
          {isDefault && <span className="cred-badge cred-badge--success">{t("cred.default")}</span>}
          {guideUrl && (
            <a
              href={guideUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="cred-guide-link"
              onClick={(e) => e.stopPropagation()}
            >
              {t("cred.getKey")} →
            </a>
          )}
        </div>
      </div>

      {/* ── API Key ──
          已保存且未点「更换密钥」→ 脱敏只读展示。
          这不只是 UI 偏好：可编辑就意味着脱敏值可能被当成用户输入回写，
          从而把真 key 永久覆盖掉（PRD R2/R3 · AC-07 的事故原型）。
          真正的保证在提交侧——toSubmittable() 此时**整个省略** api_key 字段。 */}
      <div className="cred-field">
        <label className="cred-label">API Key *</label>
        {isSaved && !keyChanged ? (
          <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
            <div className="cred-masked-key">{credential.api_key || "••••••••"}</div>
            <button
              type="button"
              className="cred-action-btn"
              disabled={disabled}
              onClick={() => {
                // 进入「更换密钥」态：清空输入框，要求重新填完整 key。
                // 不能拿脱敏值当初值继续编辑——那等于把脱敏值送进提交体。
                onChange({ ...credential, api_key: "" });
                onKeyChangedToggle?.(true);
                setShowKey(false);
              }}
              title={t("cred.changeKeyTitle")}
            >
              {t("cred.changeKey")}
            </button>
          </div>
        ) : (
          <>
            <div className="cred-input-wrap">
              <input
                type={showKey ? "text" : "password"}
                value={credential.api_key ?? ""}
                onChange={(e) => updateField("api_key", e.target.value)}
                placeholder="sk-..."
                disabled={disabled}
                className={CX(
                  "cred-input",
                  error && !(credential.api_key ?? "").trim() && "cred-input--error",
                )}
                style={{ paddingRight: 56 }}
              />
              <div className="cred-input-right">
                <button
                  type="button"
                  className="cred-toggle-btn"
                  onClick={() => setShowKey((v) => !v)}
                  tabIndex={-1}
                >
                  {showKey ? t("cred.hide") : t("cred.show")}
                </button>
              </div>
            </div>
            {isSaved && keyChanged && (
              <div className="cred-placeholder">
                {t("cred.fillNewKey")}
                <button
                  type="button"
                  className="cred-link-btn"
                  onClick={() => {
                    // 取消更换：恢复「不提交 api_key」语义，旧 key 保持不动
                    onKeyChangedToggle?.(false);
                    onChange({ ...credential, api_key: "" });
                  }}
                >
                  {t("cred.cancelChange")}
                </button>
              </div>
            )}
            {isMaskedKey(credential.api_key) && (
              <div className="cred-error-text">
                ⚠ {t("cred.maskedKeyWarning")}
              </div>
            )}
          </>
        )}
      </div>

      {/* ── 模型 ID（combobox：推荐清单 + 任意手填）──
          ╔══════════════════════════════════════════════════════════════════╗
          ║ ⚠️ 推荐清单**绝不限制**可填值（PRD R11 / AC-06）。                 ║
          ║   下面是一个 <input> 加一层建议浮层，不是 <select>：用户可以键入   ║
          ║   任意字符串（私有部署名、volcengine endpoint id、刚发布的新模型）║
          ║   并正常保存、装配、路由。清单只用来省去常见模型的输入。          ║
          ║   任何「不在推荐清单内就不让填 / 不让存」的改动，都是已删除的      ║
          ║   _DECLARED_MODELS 白名单换马甲复活，明令禁止。                   ║
          ╚══════════════════════════════════════════════════════════════════╝ */}
      <div className="cred-field">
        <label className="cred-label">{t("cred.modelId")} *</label>
        <div className="cred-combo-wrap">
          <input
            type="text"
            value={credential.model_id}
            onChange={(e) => {
              updateField("model_id", e.target.value);
              setComboOpen(true);
            }}
            onFocus={() => setComboOpen(true)}
            // 延迟收起：否则 mousedown 尚未触发 onClick，列表就先被 blur 关掉了
            onBlur={() => setTimeout(() => setComboOpen(false), 120)}
            // 占位文案不举具体 model_id 例子：代码内不留任何模型字面量（PRD G2 上线检查清单）
            placeholder={t("cred.modelIdPlaceholder")}
            disabled={disabled}
            className={CX("cred-input", duplicateKey && "cred-input--error")}
            role="combobox"
            aria-expanded={comboOpen}
            aria-autocomplete="list"
          />
          {comboOpen && hasRecommendations && (
            <div className="cred-combo-list">
              {comboCandidates.map((m) => (
                <div
                  key={m.model_id}
                  className="cred-combo-item"
                  // 用 mouseDown：input 的 blur 会先于 click 触发
                  onMouseDown={(e) => {
                    e.preventDefault();
                    handlePickRecommended(m);
                  }}
                >
                  <span>{m.model_id}</span>
                  <span className="cred-combo-price">
                    ¥{m.input_price_per_1k} / ¥{m.output_price_per_1k}
                  </span>
                </div>
              ))}
              {/* 无匹配项也**不阻塞输入**：明确告诉用户手填是受支持的路径 */}
              <div className="cred-combo-hint">
                {comboCandidates.length === 0
                  ? t("cred.comboNoMatch")
                  : t("cred.comboHint")}
              </div>
            </div>
          )}
        </div>
        {duplicateKey && (
          <div className="cred-error-text">⚠ {t("cred.duplicateModel")}</div>
        )}
        <div className="cred-placeholder">
          {t("cred.modelIdHint")}
        </div>
      </div>

      {/* ── 计费设置（单价三级回落 R5：用户填写值 > 系统默认表 > 拒绝保存）── */}
      <div className="cred-price-section">
        <div className="cred-price-header">
          <span className="cred-label">
            {t("cred.pricing")}{" "}
            <span style={{ fontWeight: 400, color: "var(--text-muted)" }}>
              CNY / 1k token
            </span>
          </span>
          <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
            {priceSource === "system" && (
              <span className="cred-badge cred-badge--muted">{t("cred.systemPrice")}</span>
            )}
            {priceSource === "user" && (
              <span className="cred-badge cred-badge--accent">{t("cred.userPrice")}</span>
            )}
            {priceSource === "missing" && (
              <span className="cred-badge cred-badge--warning">{t("cred.missingPrice")}</span>
            )}
            {/* 「我要自己填」：仅在正用系统默认价时有意义 */}
            {priceSource === "system" && (
              <button
                type="button"
                className="cred-link-btn"
                onClick={handleOverridePrice}
                disabled={disabled}
              >
                {t("cred.fillMyself")}
              </button>
            )}
            {/* 「恢复默认价」：仅在系统表命中且用户已覆盖时可见 */}
            {priceSource === "user" && systemPrice && (
              <button
                type="button"
                className="cred-link-btn"
                onClick={handleRestoreDefaultPrice}
                disabled={disabled}
              >
                {t("cred.restoreDefault")}
              </button>
            )}
          </div>
        </div>

        {!priceEditable && systemPrice ? (
          // 命中系统默认表且用户未覆盖：只读展示，用户零输入（PRD Story 2）
          <div className="cred-placeholder">
            {t("cred.systemPriceDetail", {
              input: systemPrice.input_price_per_1k,
              output: systemPrice.output_price_per_1k,
            })}
            {systemPrice.cache_read_price_per_1k != null && (
              <> {t("cred.systemPriceCache", { cache: systemPrice.cache_read_price_per_1k })}</>
            )}
            <br />
            {t("cred.systemPriceHint")}
          </div>
        ) : (
          <>
            <div className="cred-price-row">
              <div className="cred-price-cell">
                <label className="cred-label">{t("cred.inputPrice")} *</label>
                <input
                  type="number"
                  min={0}
                  step="0.00001"
                  value={credential.input_price_per_1k ?? ""}
                  onChange={(e) => handlePriceInput("input_price_per_1k", e.target.value)}
                  placeholder="0.0112"
                  disabled={disabled}
                  className={CX("cred-input", (priceHalfFilled || priceNegative) && "cred-input--error")}
                />
              </div>
              <div className="cred-price-cell">
                <label className="cred-label">{t("cred.outputPrice")} *</label>
                <input
                  type="number"
                  min={0}
                  step="0.00001"
                  value={credential.output_price_per_1k ?? ""}
                  onChange={(e) => handlePriceInput("output_price_per_1k", e.target.value)}
                  placeholder="0.0448"
                  disabled={disabled}
                  className={CX("cred-input", (priceHalfFilled || priceNegative) && "cred-input--error")}
                />
              </div>
              <div className="cred-price-cell">
                <label className="cred-label">
                  {t("cred.cachePrice")}{" "}
                  <span style={{ fontWeight: 400, color: "var(--text-muted)" }}>
                    {t("cred.optional")}
                  </span>
                </label>
                <input
                  type="number"
                  min={0}
                  step="0.00001"
                  value={credential.cache_read_price_per_1k ?? ""}
                  onChange={(e) =>
                    handlePriceInput("cache_read_price_per_1k", e.target.value)
                  }
                  placeholder={t("cred.cachePricePlaceholder")}
                  disabled={disabled}
                  className={CX("cred-input", priceNegative && "cred-input--error")}
                />
              </div>
            </div>
            {priceSource === "missing" && (
              <div className="cred-placeholder">
                {t("cred.missingPriceHint")}
              </div>
            )}
            {priceHalfFilled && (
              <div className="cred-error-text">⚠ {t("cred.priceHalfFilled")}</div>
            )}
            {priceNegative && (
              <div className="cred-error-text">⚠ {t("cred.priceNegative")}</div>
            )}
            <div className="cred-placeholder">
              {t("cred.cachePriceHint")}
            </div>
          </>
        )}

        {/* 默认价表拉取失败必须让用户看见：否则用户会误以为「这模型本来就没默认价」 */}
        {pricesError && <div className="cred-error-text">⚠ {pricesError}</div>}
      </div>

      {/* ── Base URL ── */}
      <div className="cred-field">
        <label className="cred-label">
          {t("cred.baseUrl")}{" "}
          <span style={{ fontWeight: 400, color: "var(--text-muted)" }}>
            {t("cred.optional")}
          </span>
        </label>
        <input
          type="text"
          value={credential.base_url ?? ""}
          onChange={(e) => updateField("base_url", e.target.value || undefined)}
          placeholder={defaultBaseUrl || t("cred.baseUrlPlaceholder")}
          disabled={disabled}
          className="cred-input"
        />
        {defaultBaseUrl && (
          <div className="cred-placeholder">
            {t("cred.baseUrlHint", { url: defaultBaseUrl })}
          </div>
        )}
      </div>

      {/* ── 校验状态 ── */}
      {isVerifying && (
        <div className="verify-progress" style={{ marginTop: "var(--space-1)" }}>⏳ {t("cred.verifying", { provider: providerDisplayName })}…</div>
      )}
      {error && (
        <div className="cred-error-text">
          ⚠ {error}
        </div>
      )}

      {/* ── 底部操作栏 ── */}
      <div className="cred-footer">
        <div className="cred-footer-actions">
          {onSetDefault && !isDefault && (
            <button
              type="button"
              className="cred-action-btn"
              onClick={onSetDefault}
              disabled={disabled}
              title={t("cred.setDefaultTitle")}
            >
              {t("cred.setDefault")}
            </button>
          )}
          {isDefault && (
            <span style={{ fontSize: "var(--text-xs)", color: "var(--success)", fontWeight: 500 }}>
              ✓ {t("cred.currentDefault")}
            </span>
          )}
        </div>
        <div className="cred-footer-actions">
          {onDelete && (
            <button
              type="button"
              className="cred-action-btn cred-action-btn--danger"
              onClick={onDelete}
              disabled={disabled}
              title={t("cred.deleteTitle")}
            >
              {t("cred.delete")}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
