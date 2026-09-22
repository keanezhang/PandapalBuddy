/**
 * src/pages/KnowledgeBaseEditorPage.tsx
 *
 * 知识库编辑器 / 详情页（R6 改造：不新建路由，编辑模式改为双栏）。
 *  - 新建模式：表单 + 创建（沿用旧版）
 *  - 编辑模式：顶部操作栏 + 左栏文件树（KBDropZone/KBFileTree）+ 右栏三 Tab
 *    （预览 / 库设置 / 检索测试）
 *
 * 布局对齐 docs/design/knowledge-base-ui-prototype.html。
 * 契约：pandapal/desktop_ipc/message_codec.py ⇄ src/types/api.ts。
 */

import { useEffect, useMemo, useState } from "react";
import type { ReactNode, CSSProperties } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { open } from "@tauri-apps/plugin-dialog";
import { useKbStore, type KbRightTab } from "../store/kbStore";
import { useBackend } from "../providers/BackendProvider";
import {
  KBFileTree,
  KBPreviewPanel,
  KBContextMenu,
  ConfirmDialog,
  KBDropZone,
  findNode,
  findParentPath,
  countFiles,
} from "../components/knowledgeBase";
import type { KbMenuAction, KBPreviewDoc } from "../components/knowledgeBase";
import type { KBConfig, KBDocNode } from "../types/api";

const EMPTY_CONFIG: KBConfig = {
  name: "",
  description: "",
  documents_dir: "",
  embedding_api_key: "",
  embedding_model: "",
  embedding_api_type: "text",
  embedding_api_url: "",
  embedding_dimension: 1024,
  llm_provider: "",
  llm_api_key: "",
  llm_model: "",
  llm_api_url: "",
  enable_bm25: true,
  enabled_in_chat: true,
  auto_rebuild: false,
  top_k: 5,
};

const PICKER_EXTENSIONS = ["pdf", "docx", "doc", "md", "markdown", "txt", "csv", "json"];

export function KnowledgeBaseEditorPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { name } = useParams<{ name: string }>();
  const isEdit = Boolean(name);
  const kName = name ?? "";

  const {
    requestKbDetail,
    requestKbTree,
    createKb,
    saveKb,
    uploadKbDocument,
    deleteKbDocument,
    buildKb,
    cancelBuild,
    searchKb,
    createKbFolder,
    renameKbFolder,
    deleteKbFolder,
    renameKbDocument,
    moveKbDocument,
  } = useBackend();

  const detail = useKbStore((s) => s.detail);
  const buildByName = useKbStore((s) => s.buildByName);
  const treeByName = useKbStore((s) => s.treeByName);
  const selectedPath = useKbStore((s) => s.selectedPath);
  const rightTab = useKbStore((s) => s.rightTab);
  const setSelectedPath = useKbStore((s) => s.setSelectedPath);
  const setRightTab = useKbStore((s) => s.setRightTab);
  const searchResults = useKbStore((s) => s.searchResults);
  const uploadFeedbackByName = useKbStore((s) => s.uploadFeedbackByName);

  const [config, setConfig] = useState<KBConfig>(EMPTY_CONFIG);
  const [saved, setSaved] = useState(false);
  const [treeSearch, setTreeSearch] = useState("");
  const [searchText, setSearchText] = useState("");
  const [menu, setMenu] = useState<{ node: KBDocNode; x: number; y: number } | null>(null);
  const [confirmNode, setConfirmNode] = useState<KBDocNode | null>(null);
  const [folderPrompt, setFolderPrompt] = useState<{ parentPath: string } | null>(null);
  const [folderName, setFolderName] = useState("");
  const [renameRequest, setRenameRequest] = useState<{ path: string; token: number } | null>(null);
  const [dropTarget, setDropTarget] = useState<string | null>(null);

  const tree = useMemo(() => treeByName[kName] ?? [], [treeByName, kName]);
  const buildState = buildByName[kName];
  const uploadFeedback = uploadFeedbackByName[kName] ?? null;

  const detailLoaded = detail?.name === kName ? detail : null;
  const status = detailLoaded?.status ?? undefined;
  const documentsDir = detailLoaded?.config.documents_dir || config.documents_dir;
  const busy = buildState !== undefined || status === "building";

  const selectedNode = useMemo(
    () => (selectedPath ? findNode(tree, selectedPath) : null),
    [tree, selectedPath],
  );
  const previewDoc: KBPreviewDoc | null =
    selectedNode && !selectedNode.is_dir
      ? { path: selectedNode.path, name: selectedNode.name, suffix: selectedNode.suffix, size: selectedNode.size }
      : null;

  // 编辑模式：加载详情 + 树
  useEffect(() => {
    if (isEdit && name) {
      requestKbDetail(name);
      requestKbTree(name);
      setSelectedPath(null);
      setRightTab("preview");
    }
  }, [isEdit, name, requestKbDetail, requestKbTree, setSelectedPath, setRightTab]);

  useEffect(() => {
    if (isEdit && detailLoaded) setConfig(detailLoaded.config);
  }, [isEdit, detailLoaded]);

  useEffect(() => {
    setSaved(false);
  }, [config]);

  const set = <K extends keyof KBConfig>(key: K, value: KBConfig[K]) =>
    setConfig((c) => ({ ...c, [key]: value }));

  const targetDirOf = (node: KBDocNode | null): string => {
    if (!node) return "";
    return node.is_dir ? node.path : (findParentPath(tree, node.path) ?? "");
  };

  const handleSave = () => {
    if (isEdit) {
      saveKb(config);
      setSaved(true);
    } else {
      createKb(config);
      setSaved(true);
      const trimmed = config.name.trim();
      if (trimmed) navigate(`/knowledge/${encodeURIComponent(trimmed)}/edit`);
    }
  };

  const pickAndUpload = async (targetDir: string) => {
    const selected = await open({
      multiple: true,
      filters: [{ name: "文档", extensions: PICKER_EXTENSIONS }],
    });
    if (!selected) return;
    const paths = Array.isArray(selected) ? selected : [selected];
    if (paths.length > 0) uploadKbDocument(kName, paths, targetDir);
  };

  const handleFilesDropped = (paths: string[], targetDir: string) => {
    setDropTarget(null);
    if (paths.length > 0) uploadKbDocument(kName, paths, targetDir);
  };

  const handleMenuAction = (action: KbMenuAction) => {
    const node = menu?.node;
    if (!node) return;
    switch (action) {
      case "preview":
        setSelectedPath(node.path);
        setRightTab("preview");
        break;
      case "new-folder":
        setFolderName("新建文件夹");
        setFolderPrompt({ parentPath: targetDirOf(node) });
        break;
      case "upload-here":
        void pickAndUpload(targetDirOf(node));
        break;
      case "move-to-root":
        moveKbDocument(kName, node.path, "");
        break;
      case "rename":
        setRenameRequest({ path: node.path, token: Date.now() });
        break;
      case "delete":
        setConfirmNode(node);
        break;
    }
  };

  const handleRenameNode = (node: KBDocNode, newName: string) => {
    if (node.is_dir) renameKbFolder(kName, node.path, newName);
    else renameKbDocument(kName, node.path, newName);
  };

  const handleSelectNode = (node: KBDocNode) => {
    setSelectedPath(node.path);
    if (!node.is_dir) setRightTab("preview");
  };

  const submitFolder = () => {
    const nm = folderName.trim();
    if (folderPrompt && nm) createKbFolder(kName, folderPrompt.parentPath, nm);
    setFolderPrompt(null);
  };

  const doDelete = () => {
    const node = confirmNode;
    setConfirmNode(null);
    if (!node) return;
    if (node.is_dir) deleteKbFolder(kName, node.path);
    else deleteKbDocument(kName, node.path);
  };

  const handleSearch = () => {
    if (kName && searchText.trim()) searchKb(kName, searchText.trim(), config.top_k || 5);
  };

  const docCount = countFiles(tree) || (detailLoaded?.documents?.length ?? 0);

  // ══════════════ 新建模式 ══════════════
  if (!isEdit) {
    return (
      <div style={{ height: "100vh", display: "flex", flexDirection: "column", background: "var(--bg-root)" }}>
        <PageHeader onBack={() => navigate("/knowledge")} title="新建知识库" />
        <div style={{ flex: 1, overflowY: "auto", padding: "28px 36px", minHeight: 0 }}>
          <div style={{ width: "80%", margin: "0 auto", display: "flex", flexDirection: "column", gap: 24 }}>
            {renderConfigForm(config, set)}
            <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
              <button className="btn btn-success" onClick={handleSave}>创建知识库</button>
              <span style={{ fontSize: "var(--text-xs)", color: "var(--text-tertiary)" }}>
                创建后将进入文档上传，导入文档后即可建库
              </span>
              {saved && <span style={{ fontSize: "var(--text-xs)", color: "var(--info)" }}>已保存</span>}
            </div>
          </div>
        </div>
      </div>
    );
  }

  // ══════════════ 编辑模式（双栏） ══════════════
  return (
    <div style={{ height: "100vh", display: "flex", flexDirection: "column", background: "var(--bg-root)", overflow: "hidden" }}>
      {/* 顶部操作栏 */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          padding: "9px 14px",
          borderBottom: "1px solid var(--border-subtle)",
          background: "var(--bg-panel)",
          flex: "0 0 auto",
        }}
      >
        <button className="btn btn-ghost btn-sm" onClick={() => navigate("/knowledge")}>← 知识库</button>
        <div style={{ display: "flex", alignItems: "center", gap: 9, fontSize: "var(--text-md)", fontWeight: 600, minWidth: 0 }}>
          <span>📚</span>
          <span style={{ maxWidth: 260, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{kName}</span>
          {status && <StatusBadge status={status} incremental={buildState?.incremental} />}
        </div>
        <div style={{ flex: 1 }} />
        {busy && (
          <button className="btn btn-ghost btn-sm" onClick={() => cancelBuild(kName)}>取消建库</button>
        )}
        <button
          className="btn btn-ghost btn-sm"
          disabled={busy}
          onClick={() => {
            setFolderName("新建文件夹");
            setFolderPrompt({ parentPath: targetDirOf(selectedNode) });
          }}
        >
          🗂 新建文件夹
        </button>
        <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => void pickAndUpload(targetDirOf(selectedNode))}>
          ⬆ 上传文件
        </button>
        <button
          className={status === "ready" ? "btn btn-primary btn-sm" : "btn btn-success btn-sm"}
          disabled={busy || docCount === 0}
          onClick={() => buildKb(kName, status === "ready")}
        >
          {busy ? "建库中…" : status === "ready" ? "重新建库" : "开始建库"}
        </button>
      </div>

      {/* 主体双栏 */}
      <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
        {/* 左栏：文件树 */}
        <aside
          style={{
            width: 300,
            flex: "0 0 auto",
            borderRight: "1px solid var(--border-subtle)",
            background: "var(--bg-panel)",
            display: "flex",
            flexDirection: "column",
            minHeight: 0,
          }}
        >
          <KBDropZone onFilesDropped={handleFilesDropped} onDropTargetChange={setDropTarget}>
            <KBFileTree
              tree={tree}
              selectedPath={selectedPath}
              searchQuery={treeSearch}
              onSearchChange={setTreeSearch}
              onSelect={handleSelectNode}
              onContextMenu={(node, x, y) => setMenu({ node, x, y })}
              onMoveNode={(sourcePath, targetDir) => moveKbDocument(kName, sourcePath, targetDir)}
              onRename={handleRenameNode}
              externalDropTarget={dropTarget}
              uploadFeedback={uploadFeedback}
              renameRequest={renameRequest}
            />
          </KBDropZone>
        </aside>

        {/* 右栏：三 Tab */}
        <main style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, background: "var(--bg-root)" }}>
          <div
            style={{
              display: "flex",
              gap: 2,
              padding: "8px 16px 0",
              borderBottom: "1px solid var(--border-subtle)",
              background: "var(--bg-panel)",
              flex: "0 0 auto",
            }}
          >
            {(
              [
                ["preview", "预览"],
                ["settings", "库设置"],
                ["search", "检索测试"],
              ] as [KbRightTab, string][]
            ).map(([key, label]) => (
              <button
                key={key}
                onClick={() => setRightTab(key)}
                style={{
                  padding: "8px 14px",
                  fontSize: "var(--text-sm)",
                  cursor: "pointer",
                  border: "none",
                  borderBottom: rightTab === key ? "2px solid var(--accent)" : "2px solid transparent",
                  background: "none",
                  color: rightTab === key ? "var(--text-primary)" : "var(--text-tertiary)",
                }}
              >
                {label}
              </button>
            ))}
          </div>

          <div style={{ flex: 1, overflow: "auto", padding: "20px 22px", minHeight: 0, display: "flex", flexDirection: "column" }}>
            {rightTab === "preview" && (
              <KBPreviewPanel doc={previewDoc} documentsDir={documentsDir} />
            )}

            {rightTab === "settings" && (
              <div style={{ display: "flex", flexDirection: "column", gap: 24, maxWidth: 720 }}>
                {renderConfigForm(config, set)}
                <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
                  <button className="btn btn-success" onClick={handleSave}>保存配置</button>
                  {saved && <span style={{ fontSize: "var(--text-xs)", color: "var(--info)" }}>已保存</span>}
                  {uploadFeedback && (uploadFeedback.uploaded.length > 0 || uploadFeedback.rejected.length > 0) && (
                    <span style={{ fontSize: "var(--text-xs)", color: "var(--text-tertiary)" }}>
                      最近上传 {uploadFeedback.uploaded.length} 个，跳过 {uploadFeedback.rejected.length} 个
                    </span>
                  )}
                </div>
                {buildState && (
                  <div style={{ fontSize: "var(--text-xs)", color: "var(--text-secondary)" }}>
                    建库进度：{buildState.message}（{buildState.percent}%）
                  </div>
                )}
              </div>
            )}

            {rightTab === "search" && (
              <div style={{ display: "flex", flexDirection: "column", gap: 12, maxWidth: 760 }}>
                <div style={{ fontSize: "var(--text-md)", fontWeight: 600, color: "var(--text-primary)" }}>检索测试</div>
                {status === "ready" ? (
                  <>
                    <div style={{ display: "flex", gap: 8 }}>
                      <input
                        className="skills-search-input"
                        value={searchText}
                        onChange={(e) => setSearchText(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") handleSearch();
                        }}
                        placeholder="输入问题，测试检索效果…"
                        style={{ flex: 1 }}
                      />
                      <button className="btn btn-primary btn-sm" onClick={handleSearch}>检索</button>
                    </div>
                    {searchResults && (
                      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                        {searchResults.map((r, i) => (
                          <div key={r.parent_id + i} style={{ padding: "10px 12px", borderRadius: "var(--radius-sm)", background: "var(--bg-panel)" }}>
                            <div style={{ fontSize: "var(--text-xs)", color: "var(--text-secondary)", fontWeight: 600, marginBottom: 6 }}>
                              {r.title || r.source || `结果 ${i + 1}`}
                              {r.chapter_title ? ` · ${r.chapter_title}` : ""}
                            </div>
                            <div style={{ fontSize: "var(--text-sm)", color: "var(--text-primary)", whiteSpace: "pre-wrap" }}>
                              {r.content.length > 400 ? r.content.slice(0, 400) + "…" : r.content}
                            </div>
                          </div>
                        ))}
                        {searchResults.length === 0 && (
                          <div style={{ fontSize: "var(--text-xs)", color: "var(--text-tertiary)" }}>无匹配结果</div>
                        )}
                      </div>
                    )}
                  </>
                ) : (
                  <div style={{ fontSize: "var(--text-sm)", color: "var(--text-tertiary)" }}>建库完成后可进行检索测试</div>
                )}
              </div>
            )}
          </div>
        </main>
      </div>

      {/* 右键菜单 */}
      {menu && (
        <KBContextMenu
          x={menu.x}
          y={menu.y}
          node={menu.node}
          onAction={handleMenuAction}
          onClose={() => setMenu(null)}
        />
      )}

      {/* 删除二次确认 */}
      <ConfirmDialog
        open={confirmNode !== null}
        danger
        title={confirmNode?.is_dir ? "删除文件夹" : "删除文档"}
        message={
          confirmNode
            ? `确定删除「${confirmNode.name}」？${
                confirmNode.is_dir && countDescendants(confirmNode) > 0
                  ? `其中含 ${countDescendants(confirmNode)} 个文档，`
                  : ""
              }将从库内副本移除并触发索引更新。`
            : ""
        }
        confirmLabel="确认删除"
        onConfirm={doDelete}
        onCancel={() => setConfirmNode(null)}
      />

      {/* 新建文件夹命名 */}
      {folderPrompt && (
        <div
          onClick={() => setFolderPrompt(null)}
          style={{
            position: "fixed",
            inset: 0,
            background: "color-mix(in srgb, var(--bg-root) 60%, transparent)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 1000,
            padding: 24,
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              background: "var(--bg-elevated)",
              border: "1px solid var(--border-subtle)",
              borderRadius: "var(--radius-md)",
              width: "100%",
              maxWidth: 380,
              padding: 20,
            }}
          >
            <div style={{ fontSize: "var(--text-md)", fontWeight: 600, color: "var(--text-primary)", marginBottom: 12 }}>
              新建文件夹
            </div>
            <input
              autoFocus
              className="skills-search-input"
              value={folderName}
              onChange={(e) => setFolderName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") submitFolder();
                else if (e.key === "Escape") setFolderPrompt(null);
              }}
              style={{ width: "100%" }}
            />
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 9, marginTop: 20 }}>
              <button className="btn btn-ghost" onClick={() => setFolderPrompt(null)}>{t("common.cancel", "取消")}</button>
              <button className="btn btn-primary" onClick={submitFolder}>{t("common.confirm", "创建")}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/* ── 配置表单（新建 / 库设置 共用） ───────────────────────── */

function renderConfigForm(config: KBConfig, set: <K extends keyof KBConfig>(k: K, v: KBConfig[K]) => void) {
  return (
    <>
      <Section title="基本信息">
        <Field label="名称">
          <input
            className="skills-search-input"
            value={config.name}
            disabled
            onChange={(e) => set("name", e.target.value)}
            placeholder="知识库名称（如：公司制度）"
          />
        </Field>
        <Field label="描述">
          <input
            className="skills-search-input"
            value={config.description}
            onChange={(e) => set("description", e.target.value)}
            placeholder="可选，简要说明这个库的用途"
          />
        </Field>
      </Section>

      <Section title="Embedding 配置">
        <Field label="模型名称">
          <input
            className="skills-search-input"
            value={config.embedding_model}
            onChange={(e) => set("embedding_model", e.target.value)}
            placeholder="如 text-embedding-v3 / v4"
          />
        </Field>
        <Field label="模型 Key">
          <input
            type="password"
            className="skills-search-input"
            value={config.embedding_api_key}
            onChange={(e) => set("embedding_api_key", e.target.value)}
            placeholder="Embedding 服务的 API Key"
          />
        </Field>
        <Field label="模型地址">
          <input
            className="skills-search-input"
            value={config.embedding_api_url}
            onChange={(e) => set("embedding_api_url", e.target.value)}
            placeholder="完整地址，需以 /embeddings 结尾（如 .../compatible-mode/v1/embeddings）"
          />
        </Field>
        <div style={{ display: "flex", gap: 16 }}>
          <Field label="模型类型" style={{ flex: 1 }}>
            <select
              className="skills-search-input"
              value={config.embedding_api_type}
              onChange={(e) => set("embedding_api_type", e.target.value as "text" | "multimodal")}
            >
              <option value="text">文本 (text)</option>
              <option value="multimodal">多模态 (multimodal)</option>
            </select>
          </Field>
          <Field label="向量维度" style={{ flex: 1 }}>
            <input
              type="number"
              className="skills-search-input"
              value={config.embedding_dimension}
              onChange={(e) => set("embedding_dimension", Number(e.target.value))}
            />
          </Field>
        </div>
      </Section>

      <Section title="抽取 LLM 配置">
        <Field label="服务商 (Provider)">
          <select
            className="skills-search-input"
            value={config.llm_provider}
            onChange={(e) => set("llm_provider", e.target.value)}
          >
            <option value="">请选择…</option>
            <option value="dashscope">通义千问 (DashScope)</option>
            <option value="volcengine">豆包 (Volcengine)</option>
            <option value="openai">OpenAI</option>
            <option value="deepseek">DeepSeek</option>
          </select>
        </Field>
        <Field label="模型名称">
          <input
            className="skills-search-input"
            value={config.llm_model}
            onChange={(e) => set("llm_model", e.target.value)}
            placeholder="如 qwen-plus / deepseek-chat"
          />
        </Field>
        <Field label="API Key">
          <input
            type="password"
            className="skills-search-input"
            value={config.llm_api_key}
            onChange={(e) => set("llm_api_key", e.target.value)}
            placeholder="用于建库实体 / 关键词抽取"
          />
        </Field>
        <Field label="API 地址（可选）">
          <input
            className="skills-search-input"
            value={config.llm_api_url}
            onChange={(e) => set("llm_api_url", e.target.value)}
            placeholder="留空则使用该服务商默认地址"
          />
        </Field>
      </Section>

      <Section title="索引与对话">
        <CheckRow label="启用 BM25 关键词检索" checked={config.enable_bm25} onChange={(v) => set("enable_bm25", v)} />
        <CheckRow label="在对话中启用（AI 可检索此库作答）" checked={config.enabled_in_chat} onChange={(v) => set("enabled_in_chat", v)} />
        <CheckRow label="文档变更后自动重建索引" checked={config.auto_rebuild} onChange={(v) => set("auto_rebuild", v)} />
        <Field label="引用来源数量（top-K）">
          <input
            type="number"
            min={1}
            max={20}
            className="skills-search-input"
            value={config.top_k}
            onChange={(e) => set("top_k", Number(e.target.value))}
          />
        </Field>
      </Section>
    </>
  );
}

/* ── 子组件 ───────────────────────────────────────────────── */

function PageHeader({ title, onBack }: { title: string; onBack: () => void }) {
  return (
    <div className="page-header page-header--gold">
      <div style={{ flex: 1 }}>
        <h1 className="page-title">
          <span style={{ width: 34, height: 34, fontSize: "var(--text-lg)", display: "flex", alignItems: "center", justifyContent: "center" }}>📚</span>
          {title}
        </h1>
      </div>
      <div className="skills-page-actions">
        <button onClick={onBack} className="btn btn-ghost btn-sm">返回列表</button>
      </div>
    </div>
  );
}

function StatusBadge({ status, incremental }: { status: string; incremental?: boolean }) {
  const info = statusInfo(status, incremental);
  return (
    <span
      style={{
        fontSize: "var(--text-2xs, 11px)",
        fontWeight: 500,
        padding: "2px 9px",
        borderRadius: 999,
        border: `1px solid ${info.color}`,
        color: info.color,
        whiteSpace: "nowrap",
      }}
    >
      {info.label}
    </span>
  );
}

function statusInfo(status: string, incremental?: boolean): { label: string; color: string } {
  if (status === "building") {
    return incremental
      ? { label: "增量更新中", color: "var(--info, #3b82f6)" }
      : { label: "建库中", color: "var(--info, #3b82f6)" };
  }
  switch (status) {
    case "ready":
      return { label: "已就绪", color: "var(--success, #22c55e)" };
    case "pending":
      return { label: "待索引", color: "var(--warning, #f59e0b)" };
    case "failed":
      return { label: "失败", color: "var(--danger, #ef4444)" };
    case "empty":
      return { label: "空库", color: "var(--text-tertiary)" };
    default:
      return { label: "—", color: "var(--text-tertiary)" };
  }
}

function countDescendants(node: KBDocNode): number {
  let n = 0;
  for (const c of node.children ?? []) {
    if (c.is_dir) n += countDescendants(c);
    else n += 1;
  }
  return n;
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ fontSize: "var(--text-md)", fontWeight: 600, color: "var(--text-primary)" }}>{title}</div>
      {children}
    </div>
  );
}

function Field({ label, children, style }: { label: string; children: ReactNode; style?: CSSProperties }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6, ...style }}>
      <label style={{ fontSize: "var(--text-xs)", color: "var(--text-secondary)", fontWeight: 500 }}>{label}</label>
      {children}
    </div>
  );
}

function CheckRow({ label, checked, onChange }: { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: "var(--text-sm)", color: "var(--text-primary)", cursor: "pointer" }}>
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      {label}
    </label>
  );
}

