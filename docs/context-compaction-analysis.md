# 搜索结果导致上下文超限：v0.12.4 源码分析

检查日期：2026-10-27。基线：上游 v0.12.4，提交
`33c1978d5bad52a31df75ea55143025dfad18d16`。
用户报告普通聊天、内置搜索，错误示例为 114025 tokens > 100000，
压缩阈值为 80000。尚未取得实际请求、服务端日志、API 模型标识或压缩模型配置，
所以以下区分已验证代码路径与具体事件的可能原因。100000 是报错端点给出的限制，
不能据此确认某个模型官方规格或限制究竟来自模型、供应商还是代理。

## 已确认的行为

1. 80K 是压缩触发阈值，不是请求上限。`context_compaction.py` 的
   `_resolve_token_threshold()` 取全局阈值／个人阈值和 token_cap 的较小值；
   token_cap 仅限制阈值，不能把请求裁剪到该长度。
2. 在 `middleware.py` 的请求预处理中，历史压缩发生在搜索／文件来源加入前。
   `function_calling == 'legacy'` 时，`chat_web_search_handler()` 搜索网页，
   `chat_completion_files_handler()` 检索内容，随后
   `apply_source_context_to_messages()` 注入结果。返回前没有再次调用压缩。
   因而“历史低于 80K，加入网页后高于 100K”可以直接到达提供商。
3. Native 工具循环在返回结果后、再次请求前会调用压缩，并非完全没有检查。
   但 `_find_compaction_boundary()` 为保持调用／结果配对，只在第二个及以后
   assistant tool-call 批次前切分。仅一个批次时返回 0，即使超过阈值也不压缩。
   并行多个调用属于同一批次，不等于多个可切分批次。
4. 新增内容估算使用 `len(text) // 4`，图片固定估算值，并非模型 tokenizer；
   有 usage 时使用最近 usage 加后续消息估算。中文、代码、结构化数据和工具 schema
   可能产生偏差。单纯切换界面显示的 token 数并不能提供请求长度保证。
5. `_generate_summary()` 默认摘要提示包含之前摘要、待压缩消息、保留消息。
   未配置可用压缩模型时使用当前模型；未作分块摘要。因此已经超限的上下文，
   发给同一受限模型做摘要时也可能被拒绝。这里只确认请求构造，没有复现真实提供商拒绝。
6. 历史压缩异常日志为 `Context compaction failed; continuing with full chat history`；
   工具循环为 `Tool loop context compaction failed; keeping the current context`。
   两处都继续原上下文，而不是停止超限请求。若摘要接口返回错误对象而不是抛异常，
   `_response_text()` 为空时还有简易摘要回退；不能把所有摘要失败都归为抛异常路径。
7. 保留比例按消息数量计算，不是按 token。压缩成功后也没有严格复查最终负载大小；
   保留一条很长的工具结果或当前输入，仍可能超限。
8. 开启有状态 Responses API 且有 previous_response_id 时，工具循环走单独分支，
   不经过这里的无状态 messages 压缩。需要按连接实际配置排查，不能以此认定用户走了该路径。

## 本次最小实验

使用 Python 标准库提取并执行当前源码中实际的阈值、边界、压缩入口函数，
仅对配置读取、摘要生成和持久化依赖做隔离，不编译前端，也不访问模型 API：

- 76020 个已报告 tokens + 约 38004 个新增工具 tokens：阈值检查为真，
  首批工具的 boundary=0，摘要函数未调用，完整消息原样返回。
- 加入第二个工具批次后产生有效边界，摘要函数被调用。
- v0.12.4 对三条普通对话消息、存在下一个 user 边界的超阈值历史，能够压缩；
  验证了此次上游移除短对话门槛的改进。
- 40000 个中文字符的回退估算值为 10000；这里只说明估算公式，
  没有把它当成模型真实 token 数。

这些是算法级验证，不代表复现了用户的实际 114025-token 请求。

## 现在可采取的配置措施

- 对错误端点实际 100K 的限制，先把阈值调到 50K–60K 作为起始值观察；
  这只是预留搜索、工具定义和输出空间的缓解措施，不能保证不超限。
- 在管理员设置 → 界面 → 上下文压缩中，选择实际 API 上下文更大的可用压缩模型，
  并确认已保存的配置生效，而非仍使用 Current Model。
- Native 内置 `search_web` 返回标题、链接和 snippet；大量正文主要关注 `fetch_url`。
  `web.fetch.max_content_length` / `WEB_FETCH_MAX_CONTENT_LENGTH` 可以限制
  内置 fetch_url 单页返回的**字符数**，默认未配置时不限长。可从每页 10000–20000
  字符、搜索 3–5 条起步，再按实际工具批次数和语言调整；不是 token 硬上限。
- Legacy RAG 搜索需另查搜索条数、检索 top-k、chunk 大小、全量上下文，以及
  `BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL`。不要把 native fetch_url 的设置
  当作对 legacy 注入结果也有相同上限的保证。
- 已超限且持续失败的对话，可先用更大窗口模型压缩或开启带简短任务摘要的新对话；
  原聊天记录仍保留。变更模型后是否可用以该 API 的实际限制为准。

## 若要从代码上可靠处理

需要在最终完整负载和每次工具续请求发送前统一执行预算检查：
历史、当前输入、system、工具定义、搜索结果、摘要及图片等都算入；
若提供商定义的是输入加输出总窗口，还应扣除输出／推理预留空间。
优先使用适配提供商的计数接口，无法精确计数时明确采用保守估算。

超过预算时先压缩可压缩历史，并重新计算；当超长内容主要来自当前工具结果，
按预算提取、分页或对结果分块摘要，保留引用和完整原始资料的可访问位置。
不能拆坏 tool-call/result 配对，或悄悄截掉用户要求和执行状态。
压缩模型也必须有独立的输入预算，不能把超限请求原封不动交给同一个短窗口模型。
若仍不能满足预算，停止并给出明确的可操作错误，而不是继续发送已知超限内容。
可识别的提供商长度错误可作一次有界压缩重试，避免无限重试及重复执行工具。

本次只同步上游并分析，没有擅自改变工具输出、裁剪策略、压缩语义或用户配置。

## 上游证据

- [v0.12.4 发布记录](https://github.com/open-webui/open-webui/releases/tag/v0.12.4)
- [压缩阈值、边界、摘要和估算实现](https://github.com/open-webui/open-webui/blob/33c1978d5bad52a31df75ea55143025dfad18d16/backend/open_webui/utils/context_compaction.py)
- [预处理搜索与工具循环的压缩调用及异常策略](https://github.com/open-webui/open-webui/blob/33c1978d5bad52a31df75ea55143025dfad18d16/backend/open_webui/utils/middleware.py)
- [内置搜索与网页正文工具](https://github.com/open-webui/open-webui/blob/33c1978d5bad52a31df75ea55143025dfad18d16/backend/open_webui/tools/builtin.py)
