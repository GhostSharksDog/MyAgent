# RAG 召回契约与证据阶段诊断

2026-10-06。本轮补强检索的可解释性与可复现性，不调用真实模型，不改用户 `.env`，不加载私人资料。公开语料、查询及 gold 均未改动。

## 已完成的修复

- **零匹配不再凑满结果**：TF-IDF 与 BM25 仅将有限且严格正分的匹配交给融合。关闭相似度闸门仍可能返回弱关联片段，正分不能证明资料足以回答。
- **章节名参与召回**：section 切分后的标题存于元数据，旧索引只读正文，标题中的关键词无法可靠召回。两路现在使用完整章节名与正文一起建索引；原正文、块 ID、引用和切分保持不变。
- **同分稳定**：截断边界同分时按语料顺序取，结果也按该顺序打破平局。保留部分选择与候选排序，不把全语料每次全排序。稳定性以同一语料顺序为前提。
- **改写遵守检索模式**：SPARSE 只使用 BM25，DENSE 只使用向量，HYBRID 使用两路。旧多查询融合在 SPARSE 下也加入向量结果，导致消融对照失真。启用相似度闸门时仍需向量计算作为独立过滤步骤。
- **中文词元守住边界**：标点、空白、换行、表情等分隔符结束连续汉字段；`验收，发布` 不再生成不存在的 `收发` 双字。连续文本与技术串仍保持既有行为。

服务默认仍是空语料；`RAG_MIN_SCORE=0`、`RAG_QUERY_REWRITE=none`、`RAG_MODE=hybrid`、`RAG_RERANKER=lexical`、`RAG_TOP_K=4` 均未改动。本页评测明确使用 **k=5**。已有经过标定的非零阈值需要在新索引上复测，因为加入标题会改变余弦分数。重启 API 后加载新代码。

## 如何诊断一条证据

```powershell
& .venv\Scripts\python.exe -X utf8 scripts/eval_rag.py --run --dataset general --diagnostics --json-out data/rag-diagnostics.json
& .venv\Scripts\python.exe -X utf8 scripts/eval_rag.py --compare --dataset general --diagnostics --json-out data/rag-general.json
& .venv\Scripts\python.exe -X utf8 scripts/eval_rag.py --compare --sample --diagnostics --json-out data/rag-legacy.json
```

`--diagnostics` 使用同一次检索的轨迹，**不再执行一次检索或模型调用**。每项 gold 条件分别标注：

| stage | 含义 | 排查方向 |
|---|---|---|
| `returned` | 匹配该条件的至少一个块出现在返回结果 | 检查内容与引用是否足以作答 |
| `not_recalled` | 语料有证据，但没有进入本次召回并集 | 切分、索引、查询表达与每路召回宽度 |
| `filtered_by_gate` | 已召回，但所有匹配块被原查询的相似度闸门过滤 | 阈值标定与查询表达 |
| `outside_top_k` | 通过过滤，但最终返回没有匹配块 | 排序及 k 的容量；不能仅据此归因于重排器 |
| `invalid_gold` | 条件没有匹配任何语料块 | 标注错误；CLI 在执行评测前拒绝这种输入 |

`candidate_rank` 是**融合后、重排前**的第一项匹配位置，不能冒充最终重排排名；`returned_rank` 是实际返回位置。一个条件可对应多个块，任一返回即为该条件命中。`candidate_count` 是真实去重并集大小，`gated_count` 是过滤后大小。

`recall_k=20` 限制的是**每路、每个查询**，不是混合并集的总宽度。两路并集可能超过20；多查询进一步扩大并集。报告保留参数、实际数量、查询/语料摘要、索引输入摘要与三项实现标识：`positive-stable-v1`、`cjk-boundary-v2`、`section-and-body-v1`。块摘要不变也不代表索引输入相同。

`RetrievalTrace` 由调用方为每次调用创建，只含块 ID，不放在共享 Retriever 内。并发查询的轨迹互不覆盖；复用轨迹前清空旧值。该诊断只出现在显式开启的评测报告，没有增加聊天接口字段或日志原文。

## 同参数前后复测

修改前提交为 `82e54f3`，实现提交为 `07c49b8`。四份原始报告在 [`evidence/rag-retrieval-v1/`](evidence/rag-retrieval-v1/)，保留逐条结果，不覆盖原有基准证据。前后均使用 section、size=500、overlap=80、min_size=120、k=5、min_score=0、RRF=60、两路等权、每路候选 `max(k*4,20)`，没有模型重排或改写。

通用集仍为16份原创虚构文档、48块、60查询（48正例、12无答案）；查询摘要与块摘要和[原通用基准](08-general-rag-benchmark.md)一致。下表均为“修改前 → 修改后”，完整证据率的分母为48条正例。

| 管线 | Recall@5 | MRR | NDCG@5 | 完整证据率 |
|---|---|---|---|---|
| TF-IDF | .885 → .917 | .797 → .810 | .802 → .821 | .854 → .875 |
| BM25 | .927 → .917 | .873 → .884 | .870 → .875 | .896 → .875 |
| 混合 RRF | .906 → .917 | .845 → .877 | .843 → .866 | .875 → .875 |
| TF-IDF + lexical | .938 → .917 | .897 → .892 | .887 → .879 | .896 → .875 |
| 混合 + lexical | .917 → .917 | .891 → .891 | .878 → .878 | .875 → .875 |

来源：`general-before.json` 与 `general-after.json`。默认算法组合在本集上的结果不变；部分对照改善，部分退化，不能宣称所有管线都提升。探索的加权词元、分句和覆盖排序没有呈现一致收益，因此没有替换默认重排规则。

旧公开求职集为14查询，不能与通用集混用分母：

| 管线 | Recall@5 | MRR | NDCG@5 |
|---|---|---|---|
| TF-IDF | .821 → .821 | .601 → .643 | .652 → .687 |
| BM25 | .798 → .798 | .685 → .732 | .690 → .725 |
| 混合 RRF | .869 → .798 | .657 → .702 | .692 → .710 |
| TF-IDF + lexical | .821 → .821 | .685 → .685 | .689 → .689 |
| 混合 + lexical | .821 → .821 | .685 → .685 | .689 → .689 |

来源：`legacy-before.json` 与 `legacy-after.json`。不带重排的混合管线新增遗漏“我的联系方式是什么”：相关个人信息块原在第4位，新召回并集内第8位，未进入最终 top-5。它属于 `outside_top_k`，不是证据不存在；保留该退化，不降低回归门槛掩盖问题。默认带重排组合的旧集指标不变。

## 尚未解决的质量问题

默认混合+重排仍只找齐 **8/12** 条多证据问题。新轨迹确认4条缺失证据都已召回并通过过滤，最终没有进入 top-5：

| 查询 ID | 缺失证据 | 召回并集中的位置 |
|---|---|---|
| multi_evidence-01 | 发布需项目评审与运维值班确认 | 7 |
| multi_evidence-02 | 文档内容摘要与查询集摘要 | 10 |
| multi_evidence-05 | 导出所需文件格式 | 7 |
| multi_evidence-08 | 已验收交付的保留期限 | 11 |

两个改写表达的正例 `paraphrase-03/05` 同样在召回并集中，仍被排在最终 top-5 之外。下一次排序实验可针对这些阶段提出通用策略，但不能使用 gold、查询 ID 或样本文案作为在线排序规则。

**12/12 无答案查询仍返回片段，48正例均非空**。本次删除零分只修掉完全没有词项/字符特征匹配时的补位；这些无答案问题本来就与文档存在弱关联。相关性无法单独判断资料能否回答，部分文档还明确说明未提供哪些信息。这里没有评测生成答案、引用忠实性或模型拒答率，也没有修改服务阈值。

数据由同一作者编写，规模小，没有独立标注或隐藏测试集；只作为公开离线基准与回归证据，不推断真实用户表现。

## 验证记录

修改前：1132 后端通过、1 live 跳过；166 前端通过，类型检查通过。

新增50条离线回归，覆盖零匹配与已知正例对照、标题专属词、同分截断、中文边界、改写路由、原查询闸门、各证据阶段、并发轨迹与诊断无额外调用。旧测试中“无匹配也必须有结果”“改写前后候选数量必相同”的断言改为真实匹配对照。知识工具在闸门关闭且没有词项匹配时返回可操作提示。通用CLI隔离测试同时覆盖 `--diagnostics`，禁止配置、私人语料和网络访问。

最终门禁：**1182 后端通过 + 1 live 跳过**（70.85s）、**166 前端通过**；只读格式检查（145文件）、lint、依赖锁（34包）、类型与前端构建通过。可按以下命令复现：

```powershell
& .venv\Scripts\python.exe -X utf8 -m pytest services/api/tests -o addopts="" -q --no-header
& .venv\Scripts\python.exe -X utf8 -m ruff format services/api scripts/eval_agent.py scripts/eval_rag.py --check
& .venv\Scripts\python.exe -X utf8 -m ruff check services/api scripts/eval_agent.py scripts/eval_rag.py scripts/smoke_ui.py
& .venv\Scripts\python.exe -X utf8 scripts/lock_deps.py --check
cd apps/web
pnpm test
pnpm run typecheck
pnpm build
```

本轮没有界面变更，未重新执行 Edge；之前运行记录界面的245项合成验收仍作为历史证据。GitHub Actions 的通用评测产物已启用诊断，远端执行仍需用户推送后确认。本轮真实模型请求 **0**；用户 `.env` 前后 SHA-256 相同。
