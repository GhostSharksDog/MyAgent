# 通用公开 RAG 基准与失败分析

2026-10-06。新增 `legacy-general-rag-v1`，不替换历史 14 查询基准。
本轮真实模型请求 0 次；没有修改用户 `.env`、知识库或工作区。
检索器、服务配置和默认相关性门槛保持原状。

## 1. 数据范围与复现

16 份本仓库原创的虚构“青禾协作组”Markdown，涵盖团队制度、项目交付、运维记录、知识库说明。
不是第三方材料或真实组织政策。60 条人工查询分为五类，各12条：直接查询、同义改述、
相似内容干扰、多处证据、无答案。前四类共48条有答案；12条无答案的事实没有在资料中提供。

默认切分生成48块，共7026字符。数据规模仍小，问题和材料由同一轮编写，
没有独立标注者、外部审阅或隐藏测试集；查询风格也偏向制度事实，不代表所有通用助手任务。
人工 gold 定义被评测的证据锚点，可能遗漏另一处语义等价来源，不能当作完美标注。

在仓库根目录运行：

```powershell
& .venv\Scripts\python.exe -X utf8 scripts/eval_rag.py --inspect --dataset general
& .venv\Scripts\python.exe -X utf8 scripts/eval_rag.py --validate --dataset general
& .venv\Scripts\python.exe -X utf8 scripts/eval_rag.py --compare --dataset general --json-out data/rag-general.json
# 闸门对照；仅改变本次评测，不写服务设置
& .venv\Scripts\python.exe -X utf8 scripts/eval_rag.py --compare --dataset general --min-score 0.1 --json-out data/rag-general-gate01.json
& .venv\Scripts\python.exe -X utf8 scripts/eval_rag.py --compare --dataset general --min-score 0.2 --json-out data/rag-general-gate02.json
# 保留旧基准，单独复测
& .venv\Scripts\python.exe -X utf8 scripts/eval_rag.py --compare --sample --json-out data/rag-legacy.json
```

`--dataset general` 只读取显式 manifest 中列出的文件，不读取用户配置、notes、简历或本机语料。
它拒绝 `--with-llm`、`--with-rewrite`、模型重排和 Query 改写，在加载之前报错。
缺文档、摘要漂移、重复清单或越界路径明确失败，不能偷偷少读几份后继续评分。
服务默认仍为空；用户若要演示这份知识库，需显式将知识库路径指向
`services/api/seed/rag_general/documents`，脚本不替用户配置。

参数：section、size500、overlap80、min_size120、k5、RRF平滑常数60、两路等权、
候选数 `max(k*4,20)=20`；TF-IDF 字词特征与 BM25，不使用预训练语义 embedding 或模型重排。
报告保存全部参数、检索器 stats、manifest、语料和查询内容摘要、分类与逐条结果。

本次摘要：

- 查询：`2f6ea61c51723ac453be9070ccb69b002e44519942f4f9e9b3e4c60d8e074aa4`
- 切分语料：`0fc0e3ef1d5c79addd2fe5b7c77976c7a4da1c3353e357cb3527614f613bd9bf`
- 清单：`3a179c9ad19c60c365b9d5e5b73db038af12a050dd2dad4878fbfe4ae024be85`

manifest 中另有每个原文件的字节摘要；以上查询／切分摘要针对评分时解析后的模型。
参数变化会改变切分摘要，不能拿不同版本直接作提升百分比。

## 2. 分清指标含义

Recall、Precision、MRR和NDCG只在48条有答案查询上取宏平均；NDCG仍为 `ndcg-corpus-v2`。
相关块判定为命中任意 gold 条件（OR）；新的 gold_coverage 则记录标注条件召回比例，
complete_evidence_rate 要求该查询的全部条件都被命中。单个块可以满足多个条件，
也可以需要同一文档中的不同章节，不能只数文档数量。
旧基准的新增条件覆盖字段仅用于记录标注覆盖，不重新定义旧任务的答案成功条件。

无答案查询单独报告 `negative_return_rate`（返回至少一块的比例）及 `abstention_rate`（空返回比例）。
这不是模型幻觉率或最终拒答率：有些片段明确写了“资料未提供”，生成模型可能据此正确说明缺失。
本轮没有调用生成模型；不评价它能否识别这些说明。
同时记录有答案查询的空返回率，防止把“全部拒检”写成优秀的拒答系统。
没有无答案样本时该指标为null，而不是0。

标注校验逐个检查每个 gold 条件，不能让一条有效条件掩盖另一条不存在的条件。
无答案查询不能同时标正例。校验只证明标注可匹配，语义需要人工核对。
报告的失败明细包含完全未命中、只找到部分证据、无答案非空返回与缺失条件。

## 3. 五条管线的同题对照

来源：[原始报告](evidence/rag-general-v1/baseline.json)。门槛0，48条有答案查询。

| 管线 | Recall@5 | MRR | NDCG@5 | 完整证据率 |
|---|---:|---:|---:|---:|
| TF-IDF | 0.885 | 0.797 | 0.802 | 0.854 |
| BM25 | 0.927 | 0.873 | 0.870 | 0.896 |
| 混合RRF | 0.906 | 0.845 | 0.843 | 0.875 |
| TF-IDF + 词法重排 | 0.938 | 0.897 | 0.887 | 0.896 |
| 混合RRF + 词法重排 | 0.917 | 0.891 | 0.878 | 0.875 |

此样本中 TF-IDF+重排的整体指标高于混合+重排，不能宣称“混合检索总会更好”。
该结论只适用于此固定语料与参数，没有据此修改线上默认检索配置。

混合+重排命中46/48条有答案查询，但完整证据仅42/48；多证据类只找齐8/12。
`multi_evidence-01` 命中了阿特拉斯验收日期，但没有召回“项目评审+运维值班双重确认”的发布规则。
`multi_evidence-02` 找到了灯塔冻结日，却遗漏文档与查询摘要的版本规则。
`paraphrase-03` 和 `paraphrase-05` 完全未命中，保留在报告中，不删除困难问题提高分数。
这些失败说明高命中率仍可能缺少足够证据，后续可针对多意图召回与同义表达研究改进。

## 4. 闸门对照的代价

来源：[0.1报告](evidence/rag-general-v1/gate_01.json)、[0.2报告](evidence/rag-general-v1/gate_02.json)。
固定混合+重排管线；门槛使用原查询与片段的TF-IDF余弦相似度，其他参数不变。

| min_score | 有答案Recall@5 | 完整证据率 | 有答案空返回 | 无答案非空返回 |
|---|---:|---:|---:|---:|
| 0 | 0.917 | 0.875 | 0/48 | 12/12 |
| 0.1 | 0.677 | 0.604 | 7/48 | 11/12 |
| 0.2 | 0.292 | 0.271 | 31/48 | 1/12 |

门槛0.2虽然让11/12无答案查询空返回，也让31/48有答案查询变空。
三个取值是固定的离线敏感性对照，不是独立验证后的最优阈值，不用来标定生产配置。
没有把拒检与最终回答质量混为一谈，也没有开启模型改写来弥补检索损失。

## 5. 验证与交付

新增45项回归：部分证据、同块多条件、正负样本分母、未知负例指标、精确文档名、
死 gold 分支、三种切分标注、清单漂移／缺文件／重复／越界、配置与网络隔离、质量下界和空结果反例。
默认混合+重排下界：Recall0.83、MRR0.80、NDCG0.79、完整证据率0.78。
将检索替换成空结果时四项下界都失败；同时无答案空返回率为1，证明单看拒检率会失真。

顺手修正旧回归夹具默认读取 `data/notes` 的问题，显式排除notes及额外路径。
旧基准五管线的Recall/MRR/NDCG与上一轮完全一致，RRF仍为0.869/0.657/0.692。
旧夹具的NDCG注释也改为校正值，不再使用历史0.717。

最终后端 **1103通过、1 live跳过**（60.54s）；本轮新增45项。
前端159项、类型检查和构建通过，前端未修改；格式、lint和运行时锁通过。
GitHub Actions增加通用公开校验、消融报告与artifact；远端首跑待用户推送确认。
三份原始JSON与公开样本进仓库，用户 `.env` 字节摘要保持不变。
真实模型效果、独立测试集及生产相关性阈值仍未验证。
