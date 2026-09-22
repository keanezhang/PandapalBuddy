/**
 * treeUtils.test.ts — 知识库文件树纯函数（A 组用例 1-8，unit，零 mock）
 *
 * 覆盖：排序不变式 / 递归计数 / 按 path 定位 / 父目录推导 / 子串命中 / 搜索过滤。
 * Golden 值来源：设计文档 §6 A 组（可按 POSIX 路径与集合语义独立推导）。
 */
import { describe, it, expect } from "vitest";
import {
  sortNodes,
  countFiles,
  countFolders,
  findNode,
  findParentPath,
  subtreeHasMatch,
  filterTree,
} from "../treeUtils";
import type { KBDocNode } from "../../../types/api";

function n(partial: Partial<KBDocNode> & Pick<KBDocNode, "path" | "name" | "is_dir">): KBDocNode {
  return { size: 0, suffix: "", mtime: 0, children: null, ...partial };
}

function file(path: string, name = path): KBDocNode {
  return n({ path, name, is_dir: false });
}

function dir(path: string, name = path, children: KBDocNode[] = []): KBDocNode {
  return n({ path, name, is_dir: true, children });
}

describe("treeUtils · sortNodes", () => {
  // inv-1 目录优先 + 组内 name.localeCompare(name,"zh") 升序 + 返回新数组
  it("用例1 目录优先、组内 zh 升序，返回新数组且不改原数组", () => {
    const tree = [
      file("b.md", "b.md"),
      dir("beta", "beta"),
      dir("Alpha", "Alpha"),
      file("a.md", "a.md"),
    ];

    const out = sortNodes(tree);

    expect(out.map((x) => x.name)).toEqual(["Alpha", "beta", "a.md", "b.md"]);
    expect(out).not.toBe(tree);
    // 原数组顺序未被就地排序
    expect(tree.map((x) => x.name)).toEqual(["b.md", "beta", "Alpha", "a.md"]);
  });
});

describe("treeUtils · countFiles / countFolders", () => {
  // inv-2 只计文件 / 只计目录（根不计入自身）+ null/undefined → 0
  it("用例2 递归计数正确，null / undefined / 空数组 → 0", () => {
    const tree = [
      dir("docs", "docs", [
        file("docs/a.md", "a.md"),
        dir("docs/sub", "sub", [file("docs/sub/b.md", "b.md")]),
      ]),
      file("c.md", "c.md"),
    ];

    expect(countFiles(tree)).toBe(3);
    expect(countFolders(tree)).toBe(2);

    expect(countFiles(null)).toBe(0);
    expect(countFolders(undefined)).toBe(0);
    expect(countFiles([])).toBe(0);
    expect(countFolders([])).toBe(0);
  });
});

describe("treeUtils · findNode", () => {
  // inv-3 按完整 path 精确命中，未命中 null
  it("用例3 同名 basename 由完整 path 区分，根 basename 查不到", () => {
    const tree = [
      dir("x", "x", [file("x/notes.md", "notes.md")]),
      dir("y", "y", [file("y/notes.md", "notes.md")]),
    ];

    expect(findNode(tree, "y/notes.md")!.path).toBe("y/notes.md");
    expect(findNode(tree, "notes.md")).toBeNull();
    expect(findNode(tree, "ghost.md")).toBeNull();
  });
});

describe("treeUtils · findParentPath", () => {
  // inv-4 根项 "" / 多层返回父相对路径 / 未命中 null
  it("用例4 根项返回空串、多层返回父相对路径、未命中 null", () => {
    const tree = [
      dir("docs", "docs", [
        file("docs/a.md", "a.md"),
        dir("docs/sub", "sub", [file("docs/sub/b.md", "b.md")]),
      ]),
    ];

    expect(findParentPath(tree, "docs")).toBe("");
    expect(findParentPath(tree, "docs/a.md")).toBe("docs");
    expect(findParentPath(tree, "docs/sub/b.md")).toBe("docs/sub");
    expect(findParentPath(tree, "ghost")).toBeNull();
  });
});

describe("treeUtils · subtreeHasMatch", () => {
  // inv-5 自身或后代 name 子串命中，大小写不敏感（调用方已 lowercase 查询串）
  it("用例5 自身命中、后代命中、均未命中，文件不递归 children", () => {
    const d = dir("Docs", "Docs", [file("Docs/ReadMe.md", "ReadMe.md")]);
    const f = file("a.md", "a.md");

    expect(subtreeHasMatch(d, "docs")).toBe(true);
    expect(subtreeHasMatch(d, "readme")).toBe(true);
    expect(subtreeHasMatch(d, "zzz")).toBe(false);
    expect(subtreeHasMatch(f, "a")).toBe(true);
    expect(subtreeHasMatch(f, "docs")).toBe(false);
  });
});

/** 用例 6 / 7 共用的多层树（4 个顶级节点：docs、photos、archive、notes.md） */
function searchTree(): KBDocNode[] {
  return [
    dir("docs", "docs", [
      dir("docs/sub", "sub", [file("docs/sub/report.md", "report.md")]),
      file("docs/other.md", "other.md"),
    ]),
    dir("photos", "photos", [file("photos/pic.png", "pic.png")]),
    dir("archive", "archive", []),
    file("notes.md", "notes.md"),
  ];
}

describe("treeUtils · filterTree", () => {
  // inv-6 + R4 + R12 边界：空/纯空白查询 → 全量已排序 + 空 expandPaths
  it("用例6 空查询与纯空白查询均返回全量（已排序）且 expandPaths 为空", () => {
    for (const q of ["", "   "]) {
      const res = filterTree(searchTree(), q);

      expect(res.nodes).toHaveLength(4);
      expect(res.nodes.map((x) => x.name)).toEqual(["archive", "docs", "photos", "notes.md"]);
      expect(res.expandPaths.size).toBe(0);
      expect(res.nodes).not.toBe(searchTree);
    }
  });

  // inv-6 + R4 + R3：命中保留祖先链 / 大小写不敏感 / 无命中空数组
  it("用例7 命中深层文件保留祖先链（大小写不敏感），命中目录只留该分支，无命中为空", () => {
    const r1 = filterTree(searchTree(), "REPORT");

    expect(r1.nodes).toHaveLength(1);
    expect(r1.nodes[0].path).toBe("docs");
    expect(r1.nodes[0].children!.map((c) => c.path)).toEqual(["docs/sub"]);
    expect(r1.nodes[0].children![0].children!.map((c) => c.path)).toEqual(["docs/sub/report.md"]);
    // expandPaths 为命中目录的完整相对 path 集合（Given 中 sub 的 path 为 "docs/sub"）
    expect(r1.expandPaths).toEqual(new Set(["docs", "docs/sub"]));

    const r2 = filterTree(searchTree(), "photos");
    expect(r2.nodes.map((c) => c.path)).toEqual(["photos"]);
    expect(r2.expandPaths).toEqual(new Set(["photos"]));

    const r3 = filterTree(searchTree(), "zzz");
    expect(r3.nodes).toEqual([]);
    expect(r3.expandPaths.size).toBe(0);
  });

  // inv-6 + R4：目录自身命中但子项未命中 → children 被裁剪为空
  it("用例8 目录自身命中、子项未命中时 children 为空数组", () => {
    const tree = [dir("reports", "reports", [file("reports/a.md", "a.md")])];

    const res = filterTree(tree, "reports");

    expect(res.nodes).toHaveLength(1);
    expect(res.nodes[0].path).toBe("reports");
    expect(res.nodes[0].children).toEqual([]);
    expect(res.expandPaths).toEqual(new Set(["reports"]));
  });
});
