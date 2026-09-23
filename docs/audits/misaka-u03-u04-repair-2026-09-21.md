# U03 源码修复 / U04 上游对齐候选 · 2026-09-21

## 当前交付状态

- **U03 已合入主工作树**：1 个产品文件、4 个测试文件；仅宿主工具状态分类改变。
- **U04 仅在隔离副本实现和验证，未合入**：删除旧 `argumentsError` 字段后，旧会话中即使该字段为 null，也会被现有严格模型校验拒绝。已向用户询问“只读历史适配”与“接受旧历史恢复受限”两种选择，目前没有收到选择。因此没有把这一已知恢复回归带入主目录。
- 没有重启、恢复、热更新运行中的 MISAKA；没有读取或修改真实会话、Board、用户成果或凭证。本轮数据复现均为合成夹具。
- 没有提交、暂存、推送或发布；没有改写先前修复及并行脏树改动。

## U03：宿主状态转换，不改 Hermes 抽取算法

`misaka/core/web/extract.py:register` 中：

1. 验证顶层及 `results` 的结构，提取字符串类型的 `saved_path` 并去重。
2. 无逐页错误的非空正文，或者已保存材料路径，表示有可用成果；预览为空不代表已保存材料失效。
3. 无可用成果才将整批标为失败；部分成功保持普通结果，逐页错误原样保留。
4. 保留既有顶层 `success:false` / `error` 优先语义。原始结果正文及不可信数据围栏不改。
5. 沿用原 WebPart → Pi 抛异常的错误通道；不改公共 Agent loop、Research 状态机或子代理钩子协议。

全失败的错误回执仍遵循 Pi 的 `details={}`，因此必须保证部分成功不走该通道。新增集成测试不仅检查布尔值，还验证：

`WebPart → SDK → agent_loop → saved_paths → Todo artifact_written → Research consulted_in_session`

实际生成的成功页面仍可登记和引用，卡保持 running，工具批次不因错误标记本身终止。保存路径信任内部抽取契约；未增加磁盘存在性探测或外部数据访问。

### 旧回归夹具适配

完整组合最初 4 failed / 3541 passed / 1 skipped，均由新的真实失败通道触发：

- Parallel 成功夹具没有提供后端读取的 `full_content/excerpts`，实际上返回空正文；改为正确响应并验证落盘正文。
- Keenable keyless 提取实际是 GET `/v1/fetch/public`，旧夹具把空请求正文按 MCP JSON 解码；改为真实接口形状并保留调用计数/隐私断言。
- 递归无效参数、撤销私网许可，本来就是失败场景；改为断言具体错误回执，保留限长、零联网、缓存不重取及材料字节不变断言。
- 原 `test_web_hermes_origin` 的正文 error/failed 关键词测试，成功样本由空 results 改为真实成功页，继续验证不会按关键词误判。

没有跳过这 4 个失败，也没有弱化错误状态来迁就旧测试。

## U04：已验证、尚未应用的候选

固定 Pi：`c7cdb460aa8a0cebef3446c4166729b8a0d97ead`；其 `json-parse.ts` SHA256 为 `828440b19a64773f696ced9461166913ddaddd8479697fbde32d3f298872b153`。

候选内容：

- 以 `partial-json@0.1.7` 默认 Allow.ALL 的忠实 Python 移植替换原 prefix 重建算法，移除严格 `finish_into` 分叉及 `argumentsError` 执行拦截；无生产 Node 依赖，MIT 许可证随补丁保留。
- 5 个供应商终结调用使用宽松 finish；两桥接尊重后端最终值，不再以原始片段覆盖终值。
- 未经 schema 校验的工具参数可为非对象；执行前仍由原有 schema、权限及工具范围检查把关。
- 保留 `stopReason=length` 整批不执行保护；最新 Pi 也有该保护。
- 保留流式解析节流：40,015 字符、2,001 片段仅解析 116 次，样本耗时约 0.070 秒。
- 补齐由非对象值暴露的桥接 null 必填字段，以及 Anthropic/Google 出站、Anthropic 初始 input、Mistral 入出站和 proxy 的 JS nullish/truthiness 语义。

这不是所有 provider 全部行为的升级。Google 入站既有 dict-only 差异未改；极深递归/资源耗尽不在跨语言完全等价声明内。

### 旧会话边界是合入门槛，不是被隐藏的通过项

合成旧工具调用 `{..., "argumentsError": null}` 在删除字段后的 ToolCall 校验中明确报 `extra_forbidden`；证明见 `old-field-compatibility-proof.json`。

新协议会话保存/打开、非对象参数往返已有测试；**这些通过不证明旧历史可以恢复**。
用户若选择保留旧历史，须先实现经确认的只读历史接入适配并补测，再应用 U04；当前 `u04-pending.patch` 不含该适配。没有迁移、删除或重写用户历史。

## 验证证据

- 原始基线组合：**2324 passed, 1 skipped**。
- U03 新用例：**26 failed / 28 passed → 54 passed**；覆盖直接适配、真实宿主执行和资料链。
- U03 独立候选（保留原 U04 源码）：**2378 passed, 1 skipped，-W error**。
- U04 固定 Pi 559 个样本，解析及终结分别验证；加上节流/整数索引测试：**1121 passed**。
- U04 额外确定性畸形/随机输入 **13,778 个，与固定 Pi 零差异**；不是无限输入空间证明。
- U04 调用链/schema/桥接/截断及 Anthropic 相邻：**225 passed**；新增合约在基线 **84 failed / 32 passed**。
- U03+U04 完整候选：**3580 passed, 1 skipped，-W error**。
- 主目录 U03 复验：**2378 passed, 1 skipped，-W error**。
- Scoped Ruff 通过；补丁 apply-check 通过。完整 suite 指明确列出的 Research/Sessions/模型/卡片相邻测试与全部 `tests/web`，不是全仓库测试。
- 唯一 skip 来自已有 opt-in 浏览器实测；没有真实供应商 API 或当前研究运行态验收。

测试使用独立 HOME/MISAKA/XDG/临时目录，清除真实 API 环境变量。保留原样测试日志、运行命令、目标哈希和两个独立补丁。

## 文件与补丁

- [U03 已应用补丁](/Users/makiko/Projects/misaka/docs/audits/misaka-u03-u04-repair-2026-09-21/u03-applied.patch)
- [U04 待历史策略确认的候选补丁](/Users/makiko/Projects/misaka/docs/audits/misaka-u03-u04-repair-2026-09-21/u04-pending.patch)
- [U03 目标哈希](/Users/makiko/Projects/misaka/docs/audits/misaka-u03-u04-repair-2026-09-21/u03-manifest.json)
- [U04 目标哈希](/Users/makiko/Projects/misaka/docs/audits/misaka-u03-u04-repair-2026-09-21/u04-manifest.json)
- [机器状态](/Users/makiko/Projects/misaka/docs/audits/misaka-u03-u04-repair-2026-09-21/status.json)
- [合入证明](/Users/makiko/Projects/misaka/docs/audits/misaka-u03-u04-repair-2026-09-21/source-apply-proof.json)

当前总账：**18 项已确认产品问题 = 16 项有源码修复记录 + 2 项待修（U04、U07）**；其他跟踪 7 项，合计仍 25 条。U04 候选未合入，不提前计作已修。
