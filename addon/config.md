# Attention Is All You Need

- **settingsByProfile**：由看板维护的配置档设置，按 profile 保存牌组 ID 与每日预算。请通过看板修改。
- **managedDecks**：旧版本兼容字段；首次升级不再把 **["ALL"]** 当作用户已确认选择。
- `simulationDays`：FSRS 模拟周期。默认 90；实际生效范围会归一化到 **90–730 天**——配置低于 90 按 90 计（健康判定与新增空间至少看 90 天），高于 730 收敛到 730。
- `dailyBudgetMinutes`：每日 Anki 注意力预算。

新增空间没有固定配额，也不读任何配置：看板按当前牌组、每日预算和有效复习日，用 FSRS 模拟动态估算可持续新增量（见产品文档）。旧版本写入过的 `weeklyNewTarget` 配置已废弃，不再读取。

看板以 Anki 内置 WebView 窗口承载，通过 JS bridge 与 Python 通信；不启动本地 HTTP 服务。容量预测依赖 Anki 的 FSRS 模拟接口，若当前版本不可用则自动降级为排程估算。休息日直接读取牌组配置的 **easyDaysPercentages**（Anki 原生 FSRS Easy Days），按有效天数折算未来 7 天预算。卡片编辑、启停、候选标签和笔记删除均调用 Anki 原生写操作。
