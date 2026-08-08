/**
 * src/components/pet/FloatingPet.tsx
 *
 * 聊天页右下角的浮游宠物。
 *   - 展示当前宠物（PetSprite），随 Agent 活动做动作（见 usePetReactions）。
 *   - 点击宠物 → 打开控制面板：安装（petdex slug）/ 切换 / 删除 / 显隐 / 进商店。
 *   - 可拖拽移动位置。
 *   - 未安装任何宠物时，显示一个 🐾 入口按钮。
 *
 * 视觉：全部走 global-v2.css 设计 Token（.pet-* 类），不硬编码颜色。
 */

import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { usePetStore } from "../../store/petStore";
import { PetSprite } from "./PetSprite";
import { PetStore } from "./PetStore";

export function FloatingPet() {
  const { t } = useTranslation();
  const installedPets = usePetStore((s) => s.installedPets);
  const currentSlug = usePetStore((s) => s.currentSlug);
  const enabled = usePetStore((s) => s.enabled);
  const anim = usePetStore((s) => s.anim);
  const aliases = usePetStore((s) => s.aliases);

  const current = installedPets.find((p) => p.slug === currentSlug) ?? null;
  const nameOf = (p: { slug: string; displayName: string }) =>
    aliases[p.slug]?.trim() || p.displayName;

  const [open, setOpen] = useState(false);
  const [storeOpen, setStoreOpen] = useState(false);
  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  // 右下角锚定的偏移量（正数=向左/上偏移）
  const [pos, setPos] = useState({ right: 24, bottom: 24 });
  const dragRef = useRef<{ startX: number; startY: number; right: number; bottom: number } | null>(null);
  const movedRef = useRef(false);
  const widgetRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    usePetStore.getState().refresh().catch((e) => console.error("[pet] refresh 失败:", e));
  }, []);

  // 点击面板外区域关闭面板（商店弹窗自己管，不受影响：它是 widget 的 DOM 子节点）
  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (!widgetRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [open]);

  const onPointerDown = (e: React.PointerEvent) => {
    dragRef.current = { startX: e.clientX, startY: e.clientY, right: pos.right, bottom: pos.bottom };
    movedRef.current = false;
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const d = dragRef.current;
    if (!d) return;
    const dx = e.clientX - d.startX;
    const dy = e.clientY - d.startY;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) movedRef.current = true;
    setPos({
      right: Math.max(8, d.right - dx),
      bottom: Math.max(8, d.bottom - dy),
    });
  };
  const onPointerUp = (e: React.PointerEvent) => {
    dragRef.current = null;
    (e.target as HTMLElement).releasePointerCapture(e.pointerId);
  };

  const handleAvatarClick = () => {
    if (movedRef.current) return; // 拖拽结束不当作点击
    setOpen((v) => !v);
  };

  // 起名：点击标题进入编辑
  const startEditName = () => {
    if (!current) return;
    setNameDraft(nameOf(current));
    setEditingName(true);
  };
  const commitName = () => {
    if (current) usePetStore.getState().setAlias(current.slug, nameDraft);
    setEditingName(false);
  };

  return (
    <div ref={widgetRef} className="pet-widget" style={{ right: pos.right, bottom: pos.bottom }}>
      {open && (
        <div className="pet-panel">
          {/* 头部：当前宠物名（可点击起名） */}
          <div className="pet-panel-header">
            {current ? (
              editingName ? (
                <input
                  className="pet-name-input"
                  autoFocus
                  maxLength={20}
                  value={nameDraft}
                  placeholder={current.displayName}
                  onChange={(e) => setNameDraft(e.target.value)}
                  onBlur={commitName}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") commitName();
                    if (e.key === "Escape") setEditingName(false);
                  }}
                />
              ) : (
                <span
                  className="pet-panel-title editable"
                  onClick={startEditName}
                  title={t("pet.renameTitle")}
                >
                  🐾 {nameOf(current)}
                </span>
              )
            ) : (
              <span className="pet-panel-title">🐾 {t("pet.title")}</span>
            )}
            <button
              className="btn btn-primary btn-sm"
              onClick={() => setStoreOpen(true)}
              title={t("pet.browseStoreTitle")}
            >
              {t("pet.goStore")}
            </button>
          </div>

          {/* 已安装列表 */}
          <div className="pet-section-label">{t("pet.installedSection")}</div>
          {installedPets.length === 0 ? (
            <div className="pet-empty">{t("pet.empty")}</div>
          ) : (
            <div className="pet-list">
              {installedPets.map((p) => (
                <div
                  key={p.slug}
                  className={`pet-row${p.slug === currentSlug ? " active" : ""}`}
                  onClick={() => usePetStore.getState().setCurrent(p.slug)}
                  title={p.description}
                >
                  <span className="pet-row-dot" />
                  <span className="pet-row-name">{nameOf(p)}</span>
                  <button
                    className="pet-row-remove"
                    onClick={(e) => {
                      e.stopPropagation();
                      usePetStore.getState().remove(p.slug).catch(console.error);
                    }}
                    title={t("pet.removeTitle")}
                  >
                    ✕
                  </button>
                </div>
              ))}
            </div>
          )}

          {/* 显隐开关 */}
          <div className="pet-panel-footer">
            <div
              className={`toggle${enabled ? " on" : ""}`}
              onClick={() => usePetStore.getState().setEnabled(!enabled)}
            >
              <span className="toggle-track">
                <span className="toggle-thumb" />
              </span>
              {t("pet.showPet")}
            </div>
          </div>
        </div>
      )}

      {/* 头像 / 入口 */}
      <div
        className="pet-avatar"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onClick={handleAvatarClick}
        title={t("pet.avatarTitle")}
      >
        {enabled && current ? (
          <PetSprite spritesheetPath={current.spritesheetPath} anim={anim} size={96} />
        ) : (
          <div className="pet-launcher">🐾</div>
        )}
      </div>

      {storeOpen && <PetStore onClose={() => setStoreOpen(false)} />}
    </div>
  );
}
