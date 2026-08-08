/**
 * src/components/ModelServiceSettings.tsx
 *
 * 设置页 · 模型服务管理面板。
 *
 * 职责：
 *   1. 查看已配置的 provider 凭据（密钥掩码显示）
 *   2. 修改已有凭据
 *   3. 新增其他服务商
 *   4. 删除凭据组
 *   5. 保存后提示用户手动重启客户端（新配置在下次启动时装配）
 *
 * 在 SettingsPanel 的「模型服务」Tab 中使用。
 */

import { useState, useCallback, useEffect, useRef } from "react";
import { CredentialForm } from "./CredentialForm";
import {
  useCredentialStore,
  emptyCredential,
  addableProviders,
  isMaskedKey,
  toSubmittable,
  type ProviderCredential,
  type LLMProvider,
} from "../store/credentialStore";

// ── 组件（样式类见 global-v2.css SECTION 31：.mss-* / .dashed-add-btn）─────
// 原 JS hover 效果（添加/保存按钮）已全部改为 CSS :hover 伪类。

/** 凭据主键：与后端 sentinel 的取值键 (provider, model_id) 一致 */
function credKeyOf(c: { provider: string; model_id: string }): string {
  return `${c.provider}::${(c.model_id ?? "").trim()}`;
}

interface RowMeta {
  saved: boolean;
  keyChanged: boolean;
  /** 后端持有的旧 key 所属主键；与当前行主键不一致 = 旧 key 已不适用 */
  savedKey: string | null;
}

/**
 * 该行提交时是否必须自带 api_key。
 * 三种情况需要：① 新增行 ② 用户点了「更换密钥」 ③ 改了 provider/model_id
 * （身份变了，后端按新主键取不到旧 key）。
 */
function rowNeedsKey(meta: RowMeta | undefined, cred: ProviderCredential): boolean {
  if (!meta?.saved || meta.keyChanged) return true;
  return meta.savedKey !== credKeyOf(cred);
}

export function ModelServiceSettings(_props: { onClose: () => void }) {
  const storedCredentials = useCredentialStore((s) => s.credentials);
  const saveLocal = useCredentialStore((s) => s.saveLocal);
  const verifyCredentials = useCredentialStore((s) => s.verifyCredentials);
  const providerCatalog = useCredentialStore((s) => s.providerCatalog);
  const loadModelPrices = useCredentialStore((s) => s.loadModelPrices);
  const recommendedModels = useCredentialStore((s) => s.recommendedModels);

  // 保存 / 校验的真相源在 store（后端确认后回填），不在本组件的乐观状态里
  const storeSaving = useCredentialStore((s) => s.saving);
  const storeSaveError = useCredentialStore((s) => s.saveError);
  const verifyStatus = useCredentialStore((s) => s.verifyStatus);
  const verifyResults = useCredentialStore((s) => s.verifyResults);

  // 本地编辑副本
  const [localCreds, setLocalCreds] = useState<ProviderCredential[]>([]);
  const [dirty, setDirty] = useState(false);
  const [saveSuccess, setSaveSuccess] = useState<string | null>(null);
  /**
   * 每条凭据的编辑元信息，与 localCreds **同索引、同增删**。
   *
   * - `saved`      : 该条来自后端（api_key 是脱敏值，走只读 + 「更换密钥」）
   * - `keyChanged` : 用户点过「更换密钥」并在重填 → 提交体才带 api_key（R3）
   * - `savedKey`   : 该条脱敏 key 在**后端**归属的主键 `provider::model_id`
   *
   * 用平行数组而不是 Record<index, …>：删除中间一条时 Record 的键不会跟着前移，
   * 会把「第 3 条已更换密钥」的标记错安到删除后的另一条上——那正好是
   * 「拿脱敏值当新 key 提交」的触发条件（R2/AC-07）。
   *
   * ★ savedKey 不可省：sentinel（省略 api_key = 沿用旧值）在后端是按
   *   (provider, model_id) 取回旧 key 的。只记 saved/keyChanged 的话，用户把
   *   已保存行的 model_id 改名后 saved 仍为 true → 提交体照样省略 api_key →
   *   后端按新主键找不到旧值 → 保存被拒，而界面还显示 `••••••••`，用户完全
   *   不知道该点「更换密钥」。身份变了，旧 key 就不再适用，必须重新要求输入。
   */
  const [rowMeta, setRowMeta] = useState<RowMeta[]>([]);
  /** 是否正在等待「保存」的后端确认（用于区分 store.saving 的归属） */
  const awaitingSave = useRef(false);

  const verifying = verifyStatus === "verifying";
  const saving = storeSaving;

  // 系统默认单价表：combobox 推荐 + 默认价展示都靠它，进面板即拉
  useEffect(() => {
    loadModelPrices();
  }, [loadModelPrices]);

  // 同步后端凭据到本地副本。
  // ⚠️ dirty 时**不覆盖**：用户正在编辑的输入不能被一条后台推送冲掉
  //    （PRD §4.3.1 异常分支「并发冲突：以用户输入为准，dirty 时忽略推送」）。
  useEffect(() => {
    if (dirty) return;
    setLocalCreds(JSON.parse(JSON.stringify(storedCredentials)));
    setRowMeta(
      storedCredentials.map((c) => ({
        saved: true,
        keyChanged: false,
        savedKey: credKeyOf(c),
      })),
    );
    // dirty 有意不进依赖数组：它只作为「要不要应用这次推送」的门禁读取一次，
    // 若进依赖，dirty 由 true 转 false 的瞬间会立刻拿旧数据回冲刚存好的编辑。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storedCredentials]);

  // 保存的后端确认：store.saving 落回 false 即代表 CREDENTIALS_SAVED 已到。
  // ⚠️ 禁止 fire-and-forget 后立刻报成功（PRD §4.3.1 业务规则）。
  useEffect(() => {
    if (!awaitingSave.current || storeSaving) return;
    awaitingSave.current = false;
    if (storeSaveError) {
      setSaveSuccess(null);
      return; // 失败：保留 dirty 与用户输入，不覆盖
    }
    setDirty(false);
    // 保存成功 → 全部转「已保存」只读态，更换密钥标记清零
    // 保存成功：新的归属主键 = 刚提交的这份 localCreds
    setRowMeta(
      localCreds.map((c) => ({ saved: true, keyChanged: false, savedKey: credKeyOf(c) })),
    );
    // ⚠️ 措辞是「请手动重启」而非「将自动重启」——后者是句谎话：代码里没有任何
    //    重启实现。且即便重启了 sidecar 进程也不够，LLMRouter 在
    //    run_local._build_blueprint 里**只装配一次**，新增/改动的模型必须走完整
    //    启动流程才会进入路由表。改文案前先确认自动重启真的做出来了。
    setSaveSuccess("配置已保存，请手动重启客户端以使新配置生效。");
    setTimeout(() => setSaveSuccess(null), 4000);
    // localCreds 进依赖是安全的：awaitingSave 守卫使非保存路径的重跑直接 return
  }, [storeSaving, storeSaveError, localCreds]);

  // 主键 = (provider, model_id)：同一 provider 可以配 N 个模型，
  // 所以「可添加的 provider」= 目录全量，不再排除已用过的（PRD G1）。
  const availableToAdd = addableProviders(providerCatalog);

  /** 每条的主键，用于卡片间即时查重（R4） */
  const credKeys = localCreds.map(credKeyOf);

  const allFilled = localCreds.every((c, i) => {
    if (!c?.provider) return false;
    if (typeof c.model_id !== "string" || c.model_id.trim().length === 0) return false;
    // 已保存且未更换密钥 → 不需要本地有 key（提交时会省略该字段，后端沿用旧值）
    const needsKey = rowNeedsKey(rowMeta[i], c);
    if (needsKey && (c.api_key ?? "").trim().length < 8) return false;
    // 更换密钥态下填的仍是脱敏值 → 拒绝（R2）
    if (needsKey && isMaskedKey(c.api_key)) return false;
    // 单价：输入价与输出价必须同填同空
    if ((c.input_price_per_1k == null) !== (c.output_price_per_1k == null)) return false;
    if (
      [c.input_price_per_1k, c.output_price_per_1k, c.cache_read_price_per_1k].some(
        (v) => v != null && v < 0,
      )
    ) {
      return false;
    }
    // 三级回落第③级：用户没填价、系统默认表也没有 → 拒绝保存，不在前端补 0（§九）
    const hasUserPrice = c.input_price_per_1k != null && c.output_price_per_1k != null;
    const hasSystemPrice = recommendedModels.some((p) => p.model_id === c.model_id.trim());
    if (!hasUserPrice && !hasSystemPrice) return false;
    return true;
  });

  // R4：model_id 冲突（同 provider 重复，或跨 provider 同名——路由键会坍缩）
  const hasDuplicate =
    new Set(credKeys).size !== credKeys.length ||
    new Set(localCreds.map((c) => c.model_id.trim())).size !== localCreds.length;

  const hasDefault = localCreds.some((c) => c.is_default);
  const canSave = dirty && allFilled && hasDefault && !hasDuplicate && !saving && !verifying;

  // 添加一个模型（可以是已配置过的 provider 下的另一个模型）
  const handleAdd = useCallback(
    (provider: LLMProvider) => {
      setLocalCreds((prev) => [
        ...prev,
        { ...emptyCredential(provider), is_default: prev.length === 0 },
      ]);
      // 新卡片：未保存过，api_key 直接输入
      setRowMeta((prev) => [...prev, { saved: false, keyChanged: false, savedKey: null }]);
      setDirty(true);
      setSaveSuccess(null);
    },
    [],
  );

  // 删除某组
  const handleDelete = useCallback(
    (index: number) => {
      setLocalCreds((prev) => {
        if (prev.length <= 1) {
          // 最后一组：清空全部（回退到未配置状态）
          return [];
        }
        const next = [...prev];
        next.splice(index, 1);
        // 如果删除的是默认组，将第一组设为默认
        if (prev[index].is_default && next.length > 0) {
          next[0] = { ...next[0], is_default: true };
        }
        return next;
      });
      // 元信息必须同步 splice，否则「已保存 / 已更换密钥」标记会整体错位一格
      setRowMeta((prev) => {
        if (prev.length <= 1) return [];
        const next = [...prev];
        next.splice(index, 1);
        return next;
      });
      setDirty(true);
      setSaveSuccess(null);
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
      setDirty(true);
      setSaveSuccess(null);
    },
    [],
  );

  // 设置默认组
  const handleSetDefault = useCallback((index: number) => {
    setLocalCreds((prev) =>
      prev.map((c, i) => ({ ...c, is_default: i === index })),
    );
    setDirty(true);
  }, []);

  /**
   * 提交体构造：未更换密钥的条目**整个省略** api_key 字段（PRD R3）。
   * 回写脱敏值会把真 key 永久毁掉且不可恢复（R2 / AC-07）。
   */
  const buildSubmittable = useCallback(
    () =>
      localCreds.map((c, i) => {
        // 新卡片 / 点了「更换密钥」/ 改了主键 → 才带 api_key
        return toSubmittable(c, rowNeedsKey(rowMeta[i], c));
      }),
    [localCreds, rowMeta],
  );

  /**
   * 连通性校验：走真实 IPC（VERIFY_CREDENTIALS → 后端打 verify_url）。
   *
   * ⚠️ 旧实现是 setTimeout(600) + 本地长度检查冒充校验——它对「key 拼错了 /
   *    被吊销了 / base_url 不通」一律报通过，属于制造虚假绿灯。
   *    PRD §4.3.1 业务规则：禁止以本地长度检查冒充校验结果。
   */
  const handleVerify = useCallback(() => {
    setSaveSuccess(null);
    verifyCredentials(buildSubmittable());
  }, [verifyCredentials, buildSubmittable]);

  /**
   * 保存：走 Rust `save_llm_credentials`（**唯一写入者**），等其确认才报成功。
   * 成功/失败的落点在上面那个 useEffect 里，这里不做任何乐观置态。
   *
   * ⚠️ 为什么不走 IPC 让 Python 写：用户 toml 的写入者必须**有且仅有一个**。
   *    历史上向导页走 Rust、设置页走 Python IPC，两条路径各带一套校验，
   *    正是「脱敏 key 覆盖真 key」事故的土壤（PRD·G4）。首次配置时 sidecar
   *    尚未启动，Python 不具备写入时机，所以 owner 只能是 Rust。
   *    saveLocal 失败时会 reject，这里 catch 掉——错误已落在 store.saveError。
   */
  const handleSave = useCallback(() => {
    setSaveSuccess(null);
    awaitingSave.current = true;
    void saveLocal(buildSubmittable()).catch(() => {
      /* 错误已由 store 记录并经上方 useEffect 呈现，此处无需重复处理 */
    });
  }, [saveLocal, buildSubmittable]);

  return (
    <div>
      {/* ── 头部 ── */}
      <div className="mss-header">
        <span className="mss-header-title">
          模型服务
          <span className="mss-header-count">
            {localCreds.length > 0 ? `${localCreds.length} 个模型已配置` : "未配置"}
          </span>
        </span>
      </div>

      <p className="mss-description">
        配置模型服务商的 API 凭据。同一服务商下可配置多个模型，已配置的模型将出现在
        对话页的下拉列表中。凭据仅存储在本机，不会上传。
      </p>

      {/* ── 空状态 ── */}
      {localCreds.length === 0 && !saving && (
        <div className="mss-empty">
          <div className="mss-empty-icon">🔑</div>
          <p className="mss-empty-text">尚未配置任何模型服务</p>
        </div>
      )}

      {/* ── 凭据列表 ── */}
      {localCreds.map((cred, i) => (
        <CredentialForm
          key={`settings-${i}`}
          credential={cred}
          onChange={(c) => handleChange(i, c)}
          onDelete={() => handleDelete(i)}
          verifyResult={verifyResults[i]}
          verifying={verifying}
          isDefault={cred.is_default}
          onSetDefault={() => handleSetDefault(i)}
          usedKeys={credKeys.filter((_, j) => j !== i)}
          showProviderSelect={true}
          index={i}
          providerCatalog={providerCatalog}
          // 改了 provider/model_id 后旧 key 不再适用 → 退出「已保存只读」态，
          // 否则界面继续显示 •••••••• 而保存必被后端拒绝
          isSaved={!rowNeedsKey(rowMeta[i], cred)}
          keyChanged={!!rowMeta[i]?.keyChanged}
          onKeyChangedToggle={(changed) => {
            setRowMeta((prev) => {
              const next = [...prev];
              next[i] = {
                saved: next[i]?.saved ?? false,
                keyChanged: changed,
                savedKey: next[i]?.savedKey ?? null,
              };
              return next;
            });
            setDirty(true);
          }}
        />
      ))}

      {/* ── 添加模型（同一 provider 可加多个）── */}
      {availableToAdd.length > 0 && !verifying && (
        <button
          type="button"
          className="dashed-add-btn"
          onClick={() => handleAdd(availableToAdd[0])}
        >
          + 添加模型
        </button>
      )}

      {/* ── 保存区域 ── */}
      <div className="mss-save-area">
        {verifying && (
          <div className="mss-status-text mss-status-text--muted">正在验证凭据…</div>
        )}
        {verifyStatus === "failed" && (
          <div className="mss-status-text mss-status-text--error">部分凭据验证未通过，详见各卡片内的错误提示</div>
        )}
        {hasDuplicate && (
          <div className="mss-status-text mss-status-text--error">
            存在重复的模型 ID —— 模型 ID 即路由键，重复会造成「装配了 A 却路由到 B」
          </div>
        )}
        {/* 保存错误来自后端确认（store.saveError），不是本地猜测 */}
        {storeSaveError && <div className="mss-status-text mss-status-text--error">{storeSaveError}</div>}
        {saveSuccess && <div className="mss-status-text mss-status-text--success">{saveSuccess}</div>}

        {/* 校验与保存拆开：校验是**可选**的连通性探测，失败不阻塞保存
            （PRD §4.3.1 异常分支：网络异常「不阻塞保存」） */}
        <button
          type="button"
          className="mss-save-btn mss-save-btn--ghost"
          disabled={verifying || localCreds.length === 0}
          onClick={handleVerify}
        >
          {verifying ? "验证中…" : "验证凭据连通性"}
        </button>

        <button
          type="button"
          className={saving ? "mss-save-btn mss-save-btn--loading" : "mss-save-btn"}
          disabled={!canSave}
          onClick={handleSave}
        >
          {saving ? "保存中…" : dirty ? "保存配置" : "已是最新配置"}
        </button>

        {dirty && (
          <p className="mss-status-text mss-status-text--warning">
            保存后请手动重启客户端，新配置才会生效
          </p>
        )}
      </div>
    </div>
  );
}
