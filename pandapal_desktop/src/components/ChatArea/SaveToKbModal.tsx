/**
 * src/components/ChatArea/SaveToKbModal.tsx
 *
 * 「存为知识库」弹窗：把一段对话文本存为某知识库的 .md 文档。
 *
 * 数据流：选择目标库 → useBackend().saveTextToKb(name, filename, content, autoBuild)
 *        → 后端 KB_SAVE_TEXT → manager.save_text_as_document() → 发 KB_DOCUMENTS_CHANGED
 *
 * 设计取舍：新建知识库需完整填写 embedding + 抽取 LLM 配置（见 models.KBConfig.validate），
 * 不适合在聊天小弹窗内录入；故此处只做「选已有库」，新建引导用户前往「知识库」页面。
 */
import { useEffect, useState } from "react";
import { useKbStore } from "../../store/kbStore";
import { useBackend } from "../../providers/BackendProvider";
import { Modal } from "../ui/Modal";
import { Button } from "../ui/Button";
import { toast } from "../ui/toastStore";

interface Props {
  /** 是否打开 */
  open: boolean;
  /** 要保存的文本内容 */
  content: string;
  /** 关闭回调 */
  onClose: () => void;
}

/** 默认文件名：对话摘录_YYYYMMDD_HHmm.md */
function defaultFilename(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `对话摘录_${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}.md`;
}

export function SaveToKbModal({ open, content, onClose }: Props) {
  const { requestKbList, saveTextToKb } = useBackend();
  const kbs = useKbStore((s) => s.kbs);
  const loading = useKbStore((s) => s.loading);

  const [target, setTarget] = useState("");
  const [filename, setFilename] = useState("");
  const [autoBuild, setAutoBuild] = useState(true);

  // 打开时拉取最新库列表 + 重置表单
  useEffect(() => {
    if (!open) return;
    requestKbList();
    setFilename(defaultFilename());
    setAutoBuild(true);
  }, [open, requestKbList]);

  // 列表到位后默认选中第一个库
  useEffect(() => {
    if (open && !target && kbs.length > 0) setTarget(kbs[0].name);
  }, [open, kbs, target]);

  if (!open) return null;

  const canSave = !!target && !loading;

  const handleSave = () => {
    if (!canSave) return;
    saveTextToKb(target, (filename || "").trim() || defaultFilename(), content, autoBuild);
    toast.success(`已存入知识库「${target}」`, autoBuild ? "正在构建索引…" : undefined);
    onClose();
  };

  return (
    <Modal
      title="存为知识库"
      width={440}
      onClose={onClose}
      footer={
        <div style={{ display: "flex", gap: "var(--space-2)", justifyContent: "flex-end", width: "100%" }}>
          <Button variant="ghost" size="sm" onClick={onClose}>取消</Button>
          <Button variant="primary" size="sm" disabled={!canSave} onClick={handleSave}>保存</Button>
        </div>
      }
    >
      <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
        <div>
          <label style={{ display: "block", fontSize: "var(--text-sm)", color: "var(--text-secondary)", marginBottom: "var(--space-1)" }}>
            目标知识库
          </label>
          {kbs.length > 0 ? (
            <select
              className="input"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              style={{ width: "100%" }}
            >
              {kbs.map((kb) => (
                <option key={kb.name} value={kb.name}>
                  {kb.name}（{kb.document_count} 文档 · {kb.status}）
                </option>
              ))}
            </select>
          ) : (
            <div style={{ fontSize: "var(--text-sm)", color: "var(--text-tertiary)", lineHeight: 1.6 }}>
              {loading ? "正在加载知识库…" : "还没有知识库。请先到「知识库」页面创建一个（需填写 Embedding 与抽取 LLM 配置）。"}
            </div>
          )}
        </div>

        <div>
          <label style={{ display: "block", fontSize: "var(--text-sm)", color: "var(--text-secondary)", marginBottom: "var(--space-1)" }}>
            文件名
          </label>
          <input
            className="input"
            value={filename}
            onChange={(e) => setFilename(e.target.value)}
            placeholder={defaultFilename()}
            style={{ width: "100%" }}
          />
        </div>

        <label style={{ display: "flex", alignItems: "center", gap: "var(--space-2)", fontSize: "var(--text-sm)", color: "var(--text-secondary)", cursor: "pointer" }}>
          <input type="checkbox" checked={autoBuild} onChange={(e) => setAutoBuild(e.target.checked)} />
          保存后立即构建索引（增量）
        </label>
      </div>
    </Modal>
  );
}
