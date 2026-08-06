/**
 * demo/app.tsx — InlineDiffEditor / CodeRenderer 浏览器测试台架
 *
 * URL 参数：
 *   ?case=<name>        选择 InlineDiffEditor 场景（见 SCENARIOS，默认 multi_modify）
 *   ?case=renderer      CodeRenderer 模式（CR-1~4）
 *   ?noCallbacks=1      不传 onPartialSave/onAllResolved（CMP-29）
 *   ?appliedKeys=k1,k2  initialAppliedKeys（CMP-23）
 *
 * 全局桥（供 e2e 调用）：
 *   window.__diffEvents      事件数组 [{type:"partialSave"|"allResolved"|"change", ...}]
 *   window.__setProps(p)     动态修改 original/current（props 变化用例）
 *   window.__getValue()      读取 model 当前值
 *   window.__editorReady()   编辑器是否就绪
 *   window.__setRenderer(p)  CodeRenderer 模式动态修改 content/original/readOnly/fileId
 *   window.__clearEvents()   清空事件数组
 */
import { loader } from "@monaco-editor/react";
// 与生产环境一致：Monaco 从本地 min/vs 加载（不走 CDN），由 vite.demo.config.ts 映射
loader.config({ paths: { vs: "/vs" } });

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { InlineDiffEditor } from "../../src/editor/InlineDiffEditor";
import { CodeRenderer } from "../../src/CodeRenderer";
import { computeDiff } from "../../src/engine/diff";
import { groupHunks } from "../../src/engine/hunk";

/* ── 场景目录（数据与 tests/docs/test-design.md 对齐）── */

interface Scenario {
  original: string;
  current: string;
  language?: string;
  /** 需要动态计算（如大文件）时提供 */
  build?: () => { original: string; current: string };
}

function buildLarge(): { original: string; current: string } {
  const orig: string[] = [];
  for (let i = 1; i <= 1000; i++) orig.push(`line ${i}`);
  const cur = [...orig];
  for (let i = 0; i < 100; i++) {
    const ln = (i * 10) % 1000;
    cur[ln] = `line ${ln + 1} modified`;
  }
  return { original: orig.join("\n"), current: cur.join("\n") };
}

const SCENARIOS: Record<string, Scenario> = {
  /* ── ★ 默认：真实 PR 级重构（~45 行 → ~55 行，12+ 个 hunk）────────── */
  multi_modify: { original: [
    // 简单的 Django REST 视图 —— 重构前
    "from rest_framework import status",
    "from rest_framework.decorators import api_view",
    "from rest_framework.response import Response",
    "",
    "",
    "def validate_email(email):",
    '    return "@" in email and "." in email',
    "",
    "",
    "@api_view(['GET', 'POST'])",
    "def user_list(request):",
    '    """List all users or create a new one."""',
    "    if request.method == 'GET':",
    "        users = User.objects.all()",
    "        data = [{'id': u.id, 'name': u.name, 'email': u.email} for u in users]",
    "        return Response(data)",
    "",
    "    elif request.method == 'POST':",
    "        body = request.data",
    "        name = body.get('name', '')",
    "        email = body.get('email', '')",
    "        if not name or not email:",
    "            return Response(",
    "                {'error': 'name and email are required'},",
    "                status=status.HTTP_400_BAD_REQUEST",
    "            )",
    "        if not validate_email(email):",
    "            return Response({'error': 'invalid email'}, status=status.HTTP_400_BAD_REQUEST)",
    "        if User.objects.filter(email=email).exists():",
    "            return Response({'error': 'email already registered'}, status=status.HTTP_409_CONFLICT)",
    "",
    "        user = User.objects.create(name=name, email=email)",
    "        return Response({'id': user.id, 'name': user.name, 'email': user.email}, status=status.HTTP_201_CREATED)",
  ].join("\n"),
    current: [
    // 重构后：类型注解、提取校验/序列化、统一错误格式
    "from __future__ import annotations",
    "",
    "import logging",
    "from typing import Any, TypedDict",
    "",
    "from rest_framework import status",
    "from rest_framework.decorators import api_view",
    "from rest_framework.request import Request",
    "from rest_framework.response import Response",
    "",
    "from .models import User",
    "from .serializers import UserSerializer",
    "",
    "logger = logging.getLogger(__name__)",
    "",
    "",
    "class UserPayload(TypedDict):",
    "    id: int",
    "    name: str",
    "    email: str",
    "",
    "",
    "class CreateUserError(TypedDict):",
    "    error: str",
    "    detail: str | None",
    "",
    "",
    "def validate_email(email: str) -> bool:",
    '    """Validate email format using standard regex."""',
    "    import re",
    "    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}$'",
    "    return re.match(pattern, email) is not None",
    "",
    "",
    "def _validate_create_body(body: dict[str, Any]) -> CreateUserError | None:",
    "    name = body.get('name', '')",
    "    email = body.get('email', '')",
    "    if not name or not email:",
    "        return {'error': 'name and email are required', 'detail': None}",
    "    if not validate_email(email):",
    "        return {'error': 'invalid email', 'detail': 'Malformed address'}",
    "    if User.objects.filter(email=email).exists():",
    "        return {'error': 'email already registered', 'detail': email}",
    "    return None",
    "",
    "",
    "@api_view(['GET', 'POST'])",
    "def user_list(request: Request) -> Response:",
    '    """List all users or create a new one."""',
    "    if request.method == 'GET':",
    "        users = User.objects.select_related('profile').all()",
    "        serializer = UserSerializer(users, many=True)",
    "        return Response(serializer.data)",
    "",
    "    elif request.method == 'POST':",
    "        validation_error = _validate_create_body(request.data)",
    "        if validation_error is not None:",
    "            return Response(validation_error, status=status.HTTP_400_BAD_REQUEST)",
    "",
    "        user = User.objects.create(",
    "            name=request.data['name'],",
    "            email=request.data['email'],",
    "        )",
    "        logger.info('user.created', extra={'user_id': user.id})",
    "        return Response(",
    "            UserSerializer(user).data,",
    "            status=status.HTTP_201_CREATED",
    "        )",
  ].join("\n") },

  /* ── 经典演示：TypeScript 接口重构 ──────────────────────────────────── */
  greet: { language: "typescript", original: [
    "/** User profile as returned by GET /api/users/:id */",
    "export interface UserProfile {",
    "  id: number;",
    "  name: string;",
    "  email: string;",
    "  createdAt: string;",
    "  avatarUrl: string | null;",
    "}",
    "",
    "/** Response envelope — all API responses follow this shape */",
    "export interface ApiResponse<T> {",
    "  ok: boolean;",
    "  data: T | null;",
    "  error?: string;",
    "}",
    "",
    "export async function fetchUser(id: number): Promise<ApiResponse<UserProfile>> {",
    "  const res = await fetch(`/api/users/${id}`);",
    "  if (!res.ok) {",
    "    return { ok: false, data: null, error: `HTTP ${res.status}` };",
    "  }",
    "  const json = await res.json();",
    "  return { ok: true, data: json.data };",
    "}",
  ].join("\n"),
    current: [
    "/**",
    " * User profile as returned by GET /api/users/:id.",
    " * Note: `avatarUrl` is always resolved (no longer nullable).",
    " */",
    "export interface UserProfile {",
    "  id: number;",
    "  name: string;",
    "  email: string;",
    "  /** ISO-8601, e.g. '2025-01-15T08:30:00Z' */",
    "  createdAt: string;",
    "  /** Fully qualified URL, never null. Falls back to default avatar. */",
    "  avatarUrl: string;",
    "}",
    "",
    "/** Union type: successful response or error (discriminated). */",
    "export type ApiResult<T> =",
    "  | { ok: true; data: T }",
    "  | { ok: false; error: string; code?: number };",
    "",
    "const API_BASE = import.meta.env.VITE_API_BASE ?? '/api';",
    "",
    "export async function fetchUser(id: number): Promise<ApiResult<UserProfile>> {",
    "  try {",
    "    const res = await fetch(`${API_BASE}/users/${id}`);",
    "    if (!res.ok) {",
    "      const body = await res.json().catch(() => null);",
    "      return { ok: false, error: body?.error ?? `HTTP ${res.status}`, code: res.status };",
    "    }",
    "    const json = await res.json();",
    "    return { ok: true, data: json.data };",
    "  } catch (err) {",
    '    return { ok: false, error: "Network error", code: 0 };',
    "  }",
    "}",
  ].join("\n") },
  // 旧版默认：3 个简单 modify（供 e2e 测试用，CMP-1~18 等依赖精确 hunk 数）
  three_funcs: {
    original: ["def alpha():", "    return 1", "", "def beta():", "    return 2", "", "def gamma():", "    return 3"].join("\n"),
    current: ["def alpha():", "    return 100", "", "def beta():", "    return 200", "", "def gamma():", "    return 300"].join("\n"),
  },
  no_diff: { original: "a\nb", current: "a\nb" },
  // 单类型
  add_simple: { original: "a\nb", current: "a\nx\nb" },
  del_simple: { original: "a\nb\nc", current: "a\nc" },
  modify_simple: { original: "a\nb\nc", current: "a\nx\nc" },
  // 多 hunk 同类型（ctx 行隔开，保证独立）
  add_multi: { original: "a\nb", current: "x\na\ny\nb" },                    // A3：2 add
  add_three: { original: "a\nb\nc", current: "x\na\ny\nb\nz\nc" },            // CMP-56：3 add
  del_multi: { original: "a\nb\nc\nd\ne", current: "a\nc\ne" },                // D5：2 del（ctx c 隔开；相邻 del 会被合并）
  modify_multi: { original: "a\nb\nc\nd\ne\nf", current: "a\nx\nc\ny\ne\nz" }, // 3 modify
  // 混合类型
  mixed_add_del: { original: "a\nb", current: "x\na\nc" },                    // H1：add + modify(b→c 相邻合并)
  add_then_del: { original: "a\nb\nc", current: "x\na\nc" },                  // S6：add(x) 在前、del(b) 在后（ctx a 隔开）
  del_then_add: { original: "a\nb\nc", current: "a\nc\nx" },                  // S7 修正版：del(b) 在前、add(x) 在后（ctx c 隔开）
  mixed_three: { original: "a\nb\nc\nd\ne\nf", current: "x\na\nb\nd\ny\nf" }, // H2 修正版：add(x) + del(c) + modify(e→y)，ctx 行隔开
  modify_then_add: { original: "a\nb\nc", current: "a\nx\nc\nn" },            // S9：modify(b→x) + add(n)
  two_modify: { original: "a\nb\nc\nd\ne", current: "a\nx\nc\ny\ne" },        // X3：2 个 modify（ctx c 隔开）
  adjacent_modify: { original: "a\nb\nc\nd", current: "a\nx\ny\nd" },         // X5 修正：相邻 modify 实际合并为 1 个 hunk
  // 边界
  empty_original: { original: "", current: "a\nb" },                          // E2：全 add
  empty_current: { original: "a\nb", current: "" },                           // E3：全 del
  empty_both: { original: "", current: "" },                                  // E4
  single_line: { original: "single line", current: "modified line" },         // E5
  modify_two: { original: "line1\nline2\nline3\nline4\nline5", current: "line1\nnew2\nline3\nnew4\nline5" }, // E6
  empty_lines: { original: "a\n\nb\n\nc", current: "a\n\nx\n\nc" },           // E7
  unicode: { original: "名前\n🎉", current: "名前\n🚀" },                     // E9
  no_trailing_nl: { original: "a\nb", current: "a\nx" },                      // E10（行尾无换行歧义）
  large: { original: "", current: "", build: buildLarge },                    // E8
  /* ── 真实文件场景复现：大文件 + 首 hunk 在初始视口外 + 多 hunk ── */
  // CRLF 行尾（Windows 真实文件），100 行，首 hunk 在 line 61（视口外），共 3 hunk
  crlf_first_hunk: {
    original: Array.from({ length: 100 }, (_, i) => `ctx line ${i + 1}`).join("\r\n"),
    current: (() => {
      const lines = Array.from({ length: 100 }, (_, i) => `ctx line ${i + 1}`);
      lines[59] = "LLM MODIFIED (first hunk, outside initial viewport)";
      lines[79] = "SECOND HUNK MODIFIED";
      lines[89] = "THIRD HUNK MODIFIED v2";
      lines.splice(90, 0, "THIRD HUNK EXTRA");
      return lines.join("\r\n");
    })(),
  },
  // LF 对照组：同结构，LF 行尾
  lf_first_hunk: {
    original: Array.from({ length: 100 }, (_, i) => `ctx line ${i + 1}`).join("\n"),
    current: (() => {
      const lines = Array.from({ length: 100 }, (_, i) => `ctx line ${i + 1}`);
      lines[59] = "LLM MODIFIED (first hunk, outside initial viewport)";
      lines[79] = "SECOND HUNK MODIFIED";
      lines[89] = "THIRD HUNK MODIFIED v2";
      lines.splice(90, 0, "THIRD HUNK EXTRA");
      return lines.join("\n");
    })(),
  },
};

/* ── 全局事件桥 ── */

interface DiffEvent {
  type: "partialSave" | "allResolved" | "change";
  content?: string;
  hunkKey?: string;
  at: number;
}

declare global {
  interface Window {
    __diffEvents: DiffEvent[];
    __setProps?: (p: { original?: string; current?: string }) => void;
    __setRenderer?: (p: { content?: string; original?: string | null; readOnly?: boolean; fileId?: string }) => void;
    __clearEvents?: () => void;
    monaco?: typeof import("monaco-editor");
  }
}

window.__diffEvents = [];
window.__clearEvents = () => {
  window.__diffEvents.length = 0;
};
/** 读取当前 model 值（editor 模式与 renderer 模式通用） */
(window as any).__getValue = () => window.monaco?.editor.getModels()[0]?.getValue() ?? null;
/** 编辑器是否已渲染就绪 */
(window as any).__editorReady = () => !!document.querySelector(".view-lines");
/** 计算一组 original/current 的所有 hunk contentKey（CMP-23 预备数据） */
(window as any).__computeKeys = (o: string, c: string) =>
  groupHunks(computeDiff(o, c)).map((h) => h.contentKey);

function pushEvent(e: Omit<DiffEvent, "at">) {
  window.__diffEvents.push({ ...e, at: Date.now() });
  const el = document.getElementById("evt-count");
  if (el) el.textContent = String(window.__diffEvents.length);
}

/* ── InlineDiffEditor 台架 ── */

function DiffApp({ caseName, noCallbacks, appliedKeys }: { caseName: string; noCallbacks: boolean; appliedKeys: string[] | null }) {
  const sc = SCENARIOS[caseName] ?? SCENARIOS.multi_modify;
  const initial = useMemo(() => (sc.build ? sc.build() : { original: sc.original, current: sc.current }), [sc]);
  const [original, setOriginal] = useState(initial.original);
  const [current, setCurrent] = useState(initial.current);
  const [mounted, setMounted] = useState(true);

  useEffect(() => {
    window.__setProps = (p) => {
      if (p.original !== undefined) setOriginal(p.original);
      if (p.current !== undefined) setCurrent(p.current);
    };
    return () => {
      delete window.__setProps;
    };
  }, []);

  const statusRef = useRef<HTMLSpanElement>(null);

  const onPartialSave = useCallback((content: string, hunkKey: string) => {
    pushEvent({ type: "partialSave", content, hunkKey });
  }, []);
  const onAllResolved = useCallback((content: string) => {
    pushEvent({ type: "allResolved", content });
    if (statusRef.current) statusRef.current.textContent = "resolved";
  }, []);

  return (
    <div style={{ height: "100vh", display: "flex", flexDirection: "column", fontFamily: "system-ui, sans-serif" }}>
      <div id="hud" style={{ padding: "4px 10px", background: "#1e1e2e", color: "#cdd6f4", fontSize: 12, display: "flex", gap: 16 }}>
        <span>
          case: <b id="case-name">{caseName}</b>
        </span>
        <span>
          status: <b ref={statusRef} id="status">pending</b>
        </span>
        <span>
          events: <b id="evt-count">0</b>
        </span>
        <button id="toggle-mount" onClick={() => setMounted((m) => !m)} style={{ fontSize: 11 }}>
          {mounted ? "unmount" : "mount"}
        </button>
      </div>
      <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
        {mounted && (
          <InlineDiffEditor
            original={original}
            current={current}
            language={sc.language ?? "python"}
            initialAppliedKeys={appliedKeys ?? undefined}
            onPartialSave={noCallbacks ? undefined : onPartialSave}
            onAllResolved={noCallbacks ? undefined : onAllResolved}
          />
        )}
      </div>
    </div>
  );
}

/* ── CodeRenderer 台架（CR-1~4）── */

function RendererApp() {
  const [content, setContent] = useState("a\nx\nc");
  const [original, setOriginal] = useState<string | null>("a\nb\nc");
  const [readOnly, setReadOnly] = useState(true);
  const [fileId, setFileId] = useState("f1");

  useEffect(() => {
    window.__setRenderer = (p) => {
      if (p.content !== undefined) setContent(p.content);
      if (p.original !== undefined) setOriginal(p.original);
      if (p.readOnly !== undefined) setReadOnly(p.readOnly);
      if (p.fileId !== undefined) setFileId(p.fileId);
    };
    return () => {
      delete window.__setRenderer;
    };
  }, []);

  return (
    <div style={{ height: "100vh", display: "flex", flexDirection: "column" }}>
      <div id="hud" style={{ padding: "4px 10px", background: "#1e1e2e", color: "#cdd6f4", fontSize: 12, display: "flex", gap: 16 }}>
        <span>
          mode: <b id="mode">{readOnly && original != null ? "suggestion" : "edit"}</b>
        </span>
        <span>
          fileId: <b id="file-id">{fileId}</b>
        </span>
        <span>
          events: <b id="evt-count">0</b>
        </span>
      </div>
      <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
        <CodeRenderer
          content={content}
          language="python"
          original={original ?? undefined}
          readOnly={readOnly}
          fileId={fileId}
          onChange={(v) => pushEvent({ type: "change", content: v })}
          onPartialSave={(c, k) => pushEvent({ type: "partialSave", content: c, hunkKey: k })}
          onAllResolved={(c) => pushEvent({ type: "allResolved", content: c })}
        />
      </div>
    </div>
  );
}

/* ── 入口 ── */

const params = new URLSearchParams(location.search);
const caseName = params.get("case") ?? "multi_modify";
const noCallbacks = params.get("noCallbacks") === "1";
const appliedKeysParam = params.get("appliedKeys");
const appliedKeys = appliedKeysParam ? appliedKeysParam.split(",").filter(Boolean) : null;

const rootEl = document.getElementById("root")!;
createRoot(rootEl).render(
  caseName === "renderer" ? (
    <RendererApp />
  ) : (
    <DiffApp caseName={caseName} noCallbacks={noCallbacks} appliedKeys={appliedKeys} />
  ),
);
