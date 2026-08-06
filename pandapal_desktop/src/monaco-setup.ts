/**
 * Monaco Editor 本地加载（从 public/monaco-editor/min/vs，不依赖 CDN）
 * 必须在 Editor/DiffEditor import 之前执行
 */
import { loader } from "@monaco-editor/react";
loader.config({ paths: { vs: "/monaco-editor/min/vs" } });
