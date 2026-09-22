/**
 * kbStore.test.ts — 知识库 Store 的选中态 / 右栏 Tab / 派生状态回收（H 组用例 44-48）
 *
 * Mock 策略（设计 §2）：真实 store（无 IO）。每例 beforeEach 调 reset() 消除单例残留。
 */
import { describe, it, expect, beforeEach } from "vitest";
import { useKbStore, type KbBuildState, type KbUploadFeedback } from "../../../store/kbStore";
import type { KBDocNode, KBSummary } from "../../../types/api";

function fileOf(path: string): KBDocNode {
  return {
    path,
    name: path.split("/").pop()!,
    is_dir: false,
    size: 0,
    suffix: "",
    mtime: 0,
    children: null,
  };
}

function kbSummary(name: string): KBSummary {
  return {
    name,
    description: "",
    status: "ready",
    document_count: 0,
    enabled_in_chat: false,
    auto_rebuild: false,
    top_k: 5,
    enable_bm25: true,
  };
}

function feedback(): KbUploadFeedback {
  return { uploaded: [{ name: "a.md", suffix: ".md" }], rejected: [] };
}

function buildState(): KbBuildState {
  return { stage: "scan", percent: 10, message: "扫描中" };
}

beforeEach(() => {
  useKbStore.getState().reset();
});

describe("kbStore · selectedPath / rightTab", () => {
  // inv-23 [P1]
  it("用例44 selectedPath 初始 null，setSelectedPath 生效 / 可清空", () => {
    expect(useKbStore.getState().selectedPath).toBeNull();

    useKbStore.getState().setSelectedPath("docs/a.md");
    expect(useKbStore.getState().selectedPath).toBe("docs/a.md");

    useKbStore.getState().setSelectedPath(null);
    expect(useKbStore.getState().selectedPath).toBeNull();
  });

  // inv-24 [P1]
  it("用例45 rightTab 初始 preview，setRightTab 生效", () => {
    expect(useKbStore.getState().rightTab).toBe("preview");

    useKbStore.getState().setRightTab("search");
    expect(useKbStore.getState().rightTab).toBe("search");

    useKbStore.getState().setRightTab("settings");
    expect(useKbStore.getState().rightTab).toBe("settings");
  });

  // inv-23 / inv-24 + R9 [P1]
  it("用例46 reset 清空 selectedPath 并复位 rightTab 回 preview", () => {
    useKbStore.setState({ selectedPath: "x/y.md", rightTab: "search" });

    useKbStore.getState().reset();

    expect(useKbStore.getState().selectedPath).toBeNull();
    expect(useKbStore.getState().rightTab).toBe("preview");
  });
});

describe("kbStore · remove / setTree / setUploadFeedback", () => {
  // inv-25 + R9 [P1]：删库回收该库派生状态，保留其他库
  it("用例47 remove(name) 回收该库 tree/uploadFeedback/build，保留其他库", () => {
    const nodeA = fileOf("A/a.md");
    const nodeB = fileOf("B/b.md");
    const fbA = feedback();
    const fbB = feedback();
    const bA = buildState();
    const bB = buildState();

    useKbStore.setState({
      kbs: [kbSummary("A"), kbSummary("B")],
      treeByName: { A: [nodeA], B: [nodeB] },
      uploadFeedbackByName: { A: fbA, B: fbB },
      buildByName: { A: bA, B: bB },
    });

    useKbStore.getState().remove("A");

    const s = useKbStore.getState();
    expect(s.treeByName.A).toBeUndefined();
    expect(s.treeByName.B).toEqual([nodeB]);
    expect(s.uploadFeedbackByName.A).toBeUndefined();
    expect(s.uploadFeedbackByName.B).toEqual(fbB);
    expect(s.buildByName.A).toBeUndefined();
    expect(s.buildByName.B).toEqual(bB);
    expect(s.kbs.map((k) => k.name)).toEqual(["B"]);
  });

  // inv-25 邻接 [P1]：setTree 写入并复位 treeLoading；setUploadFeedback 写入
  it("用例48 setTree 写入并复位 treeLoading；setUploadFeedback 写入", () => {
    const nodeA = fileOf("A/a.md");
    const fbA = feedback();

    useKbStore.setState({ treeLoading: true });

    useKbStore.getState().setTree("A", [nodeA]);
    expect(useKbStore.getState().treeByName.A).toEqual([nodeA]);
    expect(useKbStore.getState().treeLoading).toBe(false);

    useKbStore.getState().setUploadFeedback("A", fbA);
    expect(useKbStore.getState().uploadFeedbackByName.A).toEqual(fbA);
  });
});
