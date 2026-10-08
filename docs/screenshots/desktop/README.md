# 普通用户配置截图

2026-10-08，`scripts/smoke_ui.py --visual --target http://127.0.0.1:8097/ --screenshots-dir data/ui-desktop`。
全部API/SSE均为公开合成返回；未读取真实密钥、会话或工作区，未调用模型。
这七张截图逐张检查了内容、焦点和布局；手机设置内容可独立滚动。

| 截图 | 视口 / 内容 |
|---|---|
| [首次配置](desktop-setup-1440.png) | 1440×900，只含供应商、地址、模型名、API Key |
| [首次配置手机](desktop-setup-390.png) | 390×844，字段与主按钮完整可见 |
| [Tavily密钥](mcp-tavily-key-desktop.png) | 1440×900，直接密码输入，高级详情折叠 |
| [Tavily手机](mcp-tavily-key-mobile.png) | 390×844，密钥入口可见，设置独立滚动 |
| [实际存储状态](desktop-memory-1440.png) | 1440×900，SQLite状态、开关和明确新增入口 |
| [记忆管理手机](desktop-memory-fact-390.png) | 390×844，内容与编辑/删除/清空可见 |
| [记忆确认手机](desktop-memory-approval-390.png) | 390×844，保存前展示完整记忆及确认/拒绝 |

全套497项Edge验收另覆盖1024×768、深色主题、工具确认、三模式、停止、键盘与预先安装的错误采集。
更早的聊天、文件和深色截图仍见 [界面重设计截图](../ui-redesign/)。
