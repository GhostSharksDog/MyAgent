# 多证据排序、冻结留出与答案核验

2026-10-07。交付固定五方案排序实验、公开冻结的合成留出集，以及不调用模型的答案核验入口。**服务默认仍为 lexical、top_k=4、空语料；没有把局部收益直接替换成默认策略。**

## 排序实验与取舍

`CoverageReranker` 在原 lexical 排序之上保留第一项，再按新增查询词覆盖与候选文本重复程度选择后续项。候选内 IDF 减弱高频词重复占位，词元 Jaccard 衡量重复；严格原文相等才去重，大小写和缩进不同的代码不会被合并。同分沿用原排序，结果保留原文与出处。

只读取查询及候选，不使用 gold、查询 ID、文件名规则或本基准专用词表。五个固定候选为 lexical、仅去重、增加覆盖权重0.25、增加重复惩罚0.25、两者各0.25；不是第三方预注册实验。`eval_rag_ranking.py` 同时保留全部候选，不根据留出结果再选参数。

首轮开发实验后修正了原文去重和浮点求和的通用安全/稳定性问题，再复测开发集与旧集，指标不变；此时尚未读取留出材料。最终策略文件 SHA-256 为 `748635c893c2f3ade7210106b69201e8676e26f06d33f04d37b30019844f6350`，三个报告中的 `strategy_source_sha256` 一致，首次留出评测后未改实现或参数。

以下是同语料、同 k、同召回预算的 lexical → coverage；完整证据率分母分别为开发集48正例、留出集24正例、旧集14查询。

| 集合 | k | Recall | MRR | NDCG | 完整证据率 | 多证据找齐 |
|---|---:|---|---|---|---|---|
| 开发集 | 4 | .917 → .885 | .891 → .885 | .878 → .861 | .875 → .833 | 8/12 → 7/12 |
| 开发集 | 5 | .917 → .927 | .891 → .890 | .878 → .879 | .875 → .896 | 8/12 → 9/12 |
| 留出集 | 4 | .958 → .958 | .931 → .927 | .920 → .922 | .917 → .917 | 6/8 → 6/8 |
| 留出集 | 5 | .979 → 1.000 | .931 → .927 | .929 → .942 | .958 → 1.000 | 7/8 → 8/8 |
| 旧公开集 | 4 | .762 → .726 | .685 → .667 | .659 → .640 | .786 → .714 | 未单独分组 |
| 旧公开集 | 5 | .821 → .786 | .685 → .681 | .689 → .670 | .857 → .786 | 未单独分组 |

来源：[general-ranking.json](evidence/rag-quality-v1/general-ranking.json)、[holdout-ranking.json](evidence/rag-quality-v1/holdout-ranking.json)、[legacy-ranking.json](evidence/rag-quality-v1/legacy-ranking.json)。完整报告包含五方案×两种k、参数、语料/索引/查询摘要、逐条证据阶段和增减对照；入库只统一文本换行，不删减结果。`improved`/`regressed` 分别记录任一指标的增加/减少，同一查询可以同时列入两者。

开发集 k5 新找齐 `multi_evidence-02`，但 `paraphrase-11`、`multi_evidence-03/10` 排名退化；k4 还丢失前两者的证据。旧集 `query-14` 丢失证据，k4 另损 `query-10` 的条件覆盖。留出集 k5 找齐 `holdout-multi-03`，同时 `holdout-paraphrase-03` 首命中从第3位退到第4位。

因此保留原默认，仅提供 `--rerank coverage` 的显式离线实验入口；没有新增服务配置或界面开关。通用开发集12/12、留出集8/8无答案查询仍返回弱关联片段，所有正例均非空。排序实验没有判断答案是否受支持，不能宣称解决拒答或幻觉。

参数为 section、size=500、overlap=80、min_size=120，TF-IDF/BM25等权RRF=60，每路候选 `max(k*4,20)`（本轮两种k均20），min_score=0，无模型改写/重排。旧14题与两套通用集不能混用分母。NDCG继续使用完整语料的相关块作为理想排序分母。

## 冻结留出集的实际边界

`services/api/seed/rag_holdout` 为12份原创虚构「折光资料社」文档、36块、32查询，直接、改述、跨文档双证据、无答案各8条。它不读取私人资料，也不会自动加入服务知识库。

先创建材料与标注，只校验原文锚点及三种切分的有效性，再冻结，之后才做检索。冻结时间为 `2026-10-07T03:33:56Z`，freeze.json SHA-256 为 `9c1407c126b6a3113b242dca2cc49b45569341acbd4da37ba353a36dd360fc58`。加载器核对固定冻结摘要、每份文件与开发集版本，并拒绝重写清单+freeze后冒充同一版。

文件名、加载后规范化全文及规范化查询与开发集没有精确交集；这不是语义独立性证明。材料仍由同一 AI 作者编写、全部公开，没有独立人标、盲审或真实用户任务。摘要是一致性证据，不是签名或第三方时间戳。首次实验后不回改这版样本、gold或排序参数，新实验需要显式另建版本。

```powershell
& .venv\Scripts\python.exe -X utf8 scripts/eval_rag.py --validate --dataset holdout
& .venv\Scripts\python.exe -X utf8 scripts/eval_rag_ranking.py --dataset general --json-out data/rag-ranking/general.json
& .venv\Scripts\python.exe -X utf8 scripts/eval_rag_ranking.py --dataset legacy --json-out data/rag-ranking/legacy.json
& .venv\Scripts\python.exe -X utf8 scripts/eval_rag_ranking.py --dataset holdout --json-out data/rag-ranking/holdout.json
& .venv\Scripts\python.exe -X utf8 scripts/eval_rag.py --run --dataset holdout --rerank coverage --k 4 --diagnostics
```

## 答案与引用如何核验

`eval_rag_answers.py` 只有导出、离线评分和受控自检，没有联网生成入口。每份 bundle 绑定数据、原始块、管线参数、实际导出context及其摘要；评分时重建公开检索管线，拒绝自洽但伪造的输入。运行时与导出共用 `assemble_context`，被上下文字符预算排除的hit不会作为可见出处。

**将来生成答案时只发送每题的 `generation_messages`，不能把整个 bundle JSON 发给模型。** 内部 `query_id` 本身透露 direct/multi/unanswerable 等类别，只用于关联记录；生成消息不含ID、gold、参考答案、类别或数据集元信息。导出摘要中含标签版本的hash，不含标签内容。

| 核验层 | 自动检查 | 不能自动推出的结论 |
|---|---|---|
| 引用存在 | 正文独立 `[n]` 是否对应本题导出的出处；忽略代码、转义与inline Markdown链接 | 编号有效不等于引用贴在正确结论旁 |
| 证据覆盖 | 被引用片段逐项匹配gold；检索到但未引用的片段不算引用完整 | gold命中不等于答案包含全部正确结论 |
| 审核结构 | claim的Unicode字符范围、实际引用、精确原文引文及审核摘要 | 引文存在不等于支持结论，否定/条件/推论需人工判断 |
| 语义与拒答 | 汇总显式complete人工审核的正确性/真实拒答声明 | 生成者的declared_abstention、出现“资料不足”都不能证明真的拒答 |

报告分开列缺记录、空答案、正/负例、生成来源、引用错误和未知审核。引用有效性按正文编号出现次数计分；没有引用时为 `null`。完整引用证据率按全部预期正例计分，缺答不会消失。语义正确率只在有完整、无结构错误、明确正确性结论的人工审核子集上计算；同时给审核覆盖率和模型/人工创作来源分组。没有审核或只有受控夹具时，语义正确率为 `null`。

```powershell
& .venv\Scripts\python.exe -X utf8 scripts/eval_rag_answers.py --export --dataset holdout --k 4 --bundle-out data/rag-answers/bundle.json --template-out data/rag-answers/records.json
# records模板为空：留空不会产生答案成功率。填入真实已有答案后才评分。
& .venv\Scripts\python.exe -X utf8 scripts/eval_rag_answers.py --score --bundle data/rag-answers/bundle.json --records data/rag-answers/records.json --json-out data/rag-answers/report.json
& .venv\Scripts\python.exe -X utf8 scripts/eval_rag_answers.py --self-test --dataset general --json-out data/rag-answers/self-test.json
```

答案记录字段为 `query_id`、`bundle_id`、`origin`（model/human/controlled_fixture）、`answer`、`declared_abstention` 和可选 `review`。输入必须是JSON数组；重复题、未知题或另一bundle的答案直接拒绝。评分的参数来自bundle，不接受额外管线参数覆盖；输出不能覆盖输入。

人工审阅后填 `review`：

- `reviewer`、`origin=human`、`query_id`、`bundle_id`、`answer_sha256`。答案摘要使用 `app.rag.evaluate.fingerprint(record['answer'])`，而非原始UTF-8字节摘要；它与bundle一起绑定题目和内容，修改答案或换题后旧审核失效。
- `complete` 表示审阅者明确检查了整份答案；无法完成时填false。算法不凭字符覆盖率猜测审阅是否完整。
- `answer_correct` 为true/false/null，`is_abstention` 表示人工判断是否真正拒答。部分正确、含未经支持内容、只写拒答词后继续猜测等应如实标注。
- `claims` 每项有 `start`/`end`（Python Unicode字符偏移，左闭右开，包括引用标记）、`verdict`（supported/unsupported/uncertain）和 `evidence=[{source,quote}]`。supported必须提供实际引用出处中的精确引文；范围重叠/越界、引文不存在、引用只在代码中、正确声明与unsupported矛盾会使审核失效。

来源与审核人身份都由填写者声明，脚本不认证真人或真实模型。精确quote与结构通过后，supported仍是人工判断，不是程序证明。complete也依赖审阅者诚实检查。受控夹具的审核必须标为controlled_fixture，即使标签全为supported也不进入人工质量分母。

本评测只覆盖**单题、一次检索、完整导出的输入**。原Agent工具还可能执行8000字符头尾截断，模型messages也可能进一步裁剪；未记录实际messages时，不能宣称验证真实Agent看到了完整证据。多次检索的 `[1]` 会重用，暂不混入本评测；运行提示词已要求多次检索同时注明文档与章节，逐项说明已知和缺失事实。

## 验证与未验证项

修改前1232后端通过+1 live跳过、203前端通过，类型检查通过。新增回归覆盖排序语义/稳定性、冻结数据与CLI隔离，以及答案/引用/审核的有牙反例。两套公开材料各8项受控自检通过，报告为 [general-answer-self-test.json](evidence/rag-quality-v1/general-answer-self-test.json)、[holdout-answer-self-test.json](evidence/rag-quality-v1/holdout-answer-self-test.json)。这些fixture借助gold制造对照，只用于检查评分逻辑，不是模型成绩。

本轮真实模型请求0，未读取私人语料或修改用户 `.env`。已耗尽的30次模型预算没有重领。真实答案正确率、引用忠实性、拒答与过度拒答尚未采集；不把检索分数或受控自检当这些指标。

本地门禁结果与更新后的基线见 README/AGENTS。Windows CI新增冻结留出验证、三套公开排序实验和两套受控答案核验报告，仍需用户推送后确认远端首跑。没有界面变更，本轮未重跑Edge；既有309项合成界面检查作为历史证据保留。

最终本地等价门禁：**1361后端通过、1 live跳过**（112.10s，2条既有依赖弃用警告）、**203前端通过**；171文件只读格式检查、lint、34包lock、类型和前端构建通过。新增129条回归（排序28、冻结22、核验54、入口集成25）。公开旧集/开发集消融、留出标注校验、三套固定排序实验、两套各8项受控答案自检及90轮合成任务全部完成。后端JUnit在忽略提交的 `data/rag-answer-quality/backend-tests.xml`，新原始报告已入库。
