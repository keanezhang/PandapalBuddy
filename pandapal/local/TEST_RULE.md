# 代码改动收尾：测试闭环检验（写完代码的最后一步）

> 本文件由 `pandapal/local/prompts.py` 在模块导入期加载，拼接注入 **coding 模式** system prompt
> （office 模式不注入）。打包发布时随 PyInstaller `--add-data` 进 sidecar 包
> （见 `pandapal_desktop/build_sidecar_{macos,windows}.py` 的 DATA_FILES）。
>
> 任何代码改动任务，收尾前必须过一遍本清单。缺项要么补齐、要么在收尾说明中写明豁免理由。
> 测试执行由两个系统子 Agent 承担：**test-designer（用例设计）→ test-coder（测试代码）**，
> 方法论同源于 `monaco-inline-diff-review/tests/docs/` 的风险驱动测试设计。

## 触发与豁免

| 情形 | 处理 |
|------|------|
| 新增/修改 ≥1 个源文件的功能性改动 | **必须走闭环** |
| 单行修复、纯文档/配置/重命名、探索性 spike | 可豁免，但收尾需显式说明「豁免测试闭环 + 理由」 |

## 收尾检验清单

1. **不变式/风险已定义**：改动涉及的核心逻辑有对应的不变式或风险条目（存在于测试设计文档中），不是只测 happy path
2. **P0/P1 有用例**：高严重度风险每条至少一个对应用例；无则补齐
3. **分支可追溯**：改动引入的新分支（if/else、错误路径）都能在覆盖矩阵中找到对应用例
4. **测试实际跑过**：相关测试已真实运行并通过——「应该能过」不算数

## 委派路径（主 Agent 不亲自写测试）

- **无设计文档** → 委派 **test-designer**，输入：改动的文件/函数 + 需求背景；产出：设计文档路径 + 摘要
- **已有设计文档** → 委派 **test-coder**，输入：设计文档路径；产出：可执行测试文件路径 + 摘要
- 子 Agent 只回「路径 + ≤10 行摘要」，主 Agent 不回收全文；跑测试、修失败由主 Agent 负责
- 技术栈双轨：Python → pytest；TS → vitest + testing-library + playwright（见 test-coder 的 TS 轨道）
- 具体子 Agent 名以运行时可用列表为准，按 when_to_use 场景匹配

## 已知差距处理

设计期望与实现现状不一致时，**不改断言迁就实现**：用 `pytest.xfail` / vitest `test.fails` /
playwright `test.fail` 显式标记差距并在收尾报告中说明；将来修复后测试「意外通过」即报警。
