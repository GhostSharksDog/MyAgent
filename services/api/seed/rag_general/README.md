# 通用公开检索基准 v1

本仓库原创的虚构“青禾协作组”资料。16 份 Markdown，四个领域各四份：
团队制度、项目交付、运维记录、知识库说明。它们不描述真实组织、人员或生产规则。
没有私人文档、第三方网页原文或需要模型生成的材料。

60 条人工编写的查询分为直接查询、同义改述、相似内容干扰、多处证据和无答案五类，各 12 条。
前四类共 48 条有答案查询；最后 12 条不提供问题要求的事实，reference_answer 记录缺失说明。
这些说明和 gold 仅供评测核对，**不进入检索索引**。

`manifest.json` 显式列出文档及 SHA-256；清单外文件不加载。
`eval_set.json` 使用精确文档名与支持句锚点，逐条件校验，避免同名材料串标或一个有效条件掩盖另一个失效条件。
同义问题和答案都由同一轮编写，没有独立外部审阅或隐藏测试集，不能据此推断生产泛化。

```powershell
& .venv\Scripts\python.exe -X utf8 scripts/eval_rag.py --validate --dataset general
& .venv\Scripts\python.exe -X utf8 scripts/eval_rag.py --compare --dataset general --json-out data/rag-general.json
```

参数默认沿用旧评测入口：section、size500、overlap80、min_size120、k5；默认门槛0，不联网。
`--dataset general` 拒绝模型重排、改写及相关付费参数，不读取 `.env` 或用户数据源。
旧 `--sample` 的 14 查询求职基准保留，两份基准的分数不能直接作改进比较。

若想在界面中演示这些文档，需由用户显式将知识库路径设置为 `services/api/seed/rag_general/documents`。
服务默认仍为空语料；评测脚本不会修改设置或替用户加载文档。

五管线、阈值对照、逐条失败和限制见 [评测记录](../../../../docs/08-general-rag-benchmark.md)。
