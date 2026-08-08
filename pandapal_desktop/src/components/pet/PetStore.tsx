/**
 * src/components/pet/PetStore.tsx
 *
 * 宠物商店弹窗。数据来自官方 manifest（usePetStore.fetchCatalog）。
 *
 * 设计取舍：
 *   - 无缩略图资源，预览用完整 sprite.webp 的「第 0 帧」裁剪显示（div + background）。
 *   - 用「搜索 + 分类筛选 + 分页(24/页)」控制同时加载的 sprite 数量，规避 3700×2MB 全量加载。
 *   - 一键安装走 install_pet_urls（manifest 直链），比解析安装脚本更快更稳。
 *   - 视觉走 global-v2.css 设计 Token（.pet-store-* / .pet-card / .btn），不硬编码颜色。
 */

import { useEffect, useMemo, useState } from "react";
import { usePetStore } from "../../store/petStore";
import { FRAME_H, FRAME_W, type CatalogEntry } from "../../types/pet";
import { Modal } from "../ui";

const PAGE_SIZE = 24;
const THUMB = 72;

export function PetStore({ onClose }: { onClose: () => void }) {
  const catalog = usePetStore((s) => s.catalog);
  const catalogLoading = usePetStore((s) => s.catalogLoading);
  const installedPets = usePetStore((s) => s.installedPets);
  const installing = usePetStore((s) => s.installing);

  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("全部");
  const [page, setPage] = useState(0);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    usePetStore.getState().fetchCatalog().catch((e) => setError(String(e)));
  }, []);

  // 分类选项（从清单动态提取）
  const kinds = useMemo(() => {
    const set = new Set<string>();
    catalog.forEach((p) => p.kind && set.add(p.kind));
    return ["全部", ...Array.from(set).sort()];
  }, [catalog]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return catalog.filter((p) => {
      if (kind !== "全部" && p.kind !== kind) return false;
      if (!q) return true;
      return (
        p.displayName.toLowerCase().includes(q) ||
        p.slug.toLowerCase().includes(q)
      );
    });
  }, [catalog, query, kind]);

  // 筛选变化时回到第一页
  useEffect(() => setPage(0), [query, kind]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const pageItems = filtered.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE);
  const installedSlugs = useMemo(
    () => new Set(installedPets.map((p) => p.slug)),
    [installedPets],
  );

  const handleInstall = async (entry: CatalogEntry) => {
    setError(null);
    try {
      await usePetStore.getState().installFromCatalog(entry);
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <Modal
      title="🐾 宠物商店"
      headerExtra={
        <span className="pet-store-count">
          {catalogLoading ? "加载中…" : `共 ${catalog.length} 只`}
        </span>
      }
      onClose={onClose}
      className="pet-store"
      bare
    >

        {/* 版权免责声明 */}
        <div className="pet-store-disclaimer">
          ⚠️ 宠物为 petdex 上的第三方同人内容，安装即表示你自行承担相应责任。
        </div>

        {/* 搜索 + 分类 */}
        <div className="pet-store-toolbar">
          <input
            className="pet-input"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索名字或 slug…"
            style={{ flex: 1, minWidth: 160 }}
          />
          <select
            className="pet-input"
            value={kind}
            onChange={(e) => setKind(e.target.value)}
            style={{ flex: "0 0 auto", cursor: "pointer" }}
          >
            {kinds.map((k) => (
              <option key={k} value={k}>{k}</option>
            ))}
          </select>
        </div>

        {error && (
          <div className="pet-error" style={{ padding: "0 var(--space-4) var(--space-2)" }}>
            {error}
          </div>
        )}

        {/* 网格 */}
        <div className="pet-store-body">
          {catalogLoading && catalog.length === 0 ? (
            <div className="pet-store-empty">正在拉取宠物清单…</div>
          ) : filtered.length === 0 ? (
            <div className="pet-store-empty">没有匹配的宠物</div>
          ) : (
            <div className="pet-store-grid">
              {pageItems.map((p) => {
                const installed = installedSlugs.has(p.slug);
                const busy = installing === p.slug;
                return (
                  <div key={p.slug} className="pet-card">
                    <PetThumb url={p.spritesheetUrl} />
                    <div
                      className="pet-card-name"
                      title={`${p.displayName}（${p.slug}）${p.submittedBy ? " · by " + p.submittedBy : ""}`}
                    >
                      {p.displayName}
                    </div>
                    <div className="pet-card-kind">{p.kind || "—"}</div>
                    <button
                      className={`btn btn-sm ${installed ? "btn-ghost" : "btn-primary"}`}
                      style={{ width: "100%" }}
                      onClick={() => handleInstall(p)}
                      disabled={installed || !!installing}
                    >
                      {installed ? "已安装" : busy ? "安装中…" : "安装"}
                    </button>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* 分页 */}
        {pageCount > 1 && (
          <div className="pet-store-footer">
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              disabled={page === 0}
            >
              ‹ 上一页
            </button>
            <span className="pet-store-page-info">{page + 1} / {pageCount}</span>
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
              disabled={page >= pageCount - 1}
            >
              下一页 ›
            </button>
          </div>
        )}
    </Modal>
  );
}

/** 首帧缩略图：把完整 sprite 缩放为「单帧 = THUMB」，容器裁出左上第 0 帧。 */
function PetThumb({ url }: { url: string }) {
  const h = Math.round((THUMB * FRAME_H) / FRAME_W);
  return (
    <div className="pet-card-thumb" style={{ width: THUMB, height: h }}>
      <div
        style={{
          width: THUMB * 8, // 8 列
          height: h * 9, // 9 行
          backgroundImage: `url("${url}")`,
          backgroundSize: "100% 100%",
          backgroundRepeat: "no-repeat",
          imageRendering: "pixelated",
        }}
      />
    </div>
  );
}
