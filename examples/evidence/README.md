# 匿名评分样例

`online-results.example.json` 保留 AgentCore Online 输出的字段结构、评分及解释，但实验执行者替换了 AWS 账号、资源 ID、服务名、session／trace／span 标识。账号 `000000000000` 与这些标识仅为占位，不对应可查询的 AWS 资源。

样例来自本地演示订单。Java 应用实际执行工具，业务模型生成回答，AgentCore judge 对回答和工具目标评价；这不是客户生产质量报告。样例包含两个会话、10 条评分事件：每个会话的 Correctness、Helpfulness、custom judge 各一条，两个工具的 ToolSelectionAccuracy 各一条。

客户要检查本次保留的实际云端资源，应使用实验执行者单独提供的账户检查指南。Git 仓库不保存真实 AWS state、运行 stdout/stderr 或原始主机信息。
