# CloudWatch Dashboard：查看 Java Agent 的评分

## 客户为什么需要这个页面

客户希望在一个页面查看各项评分、样本数、趋势和评分理由。AgentCore Evaluations 把评分写成自带 EMF 信息的日志；CloudWatch 自动提取指标；本项目的 Dashboard 脚本把已有指标和日志查询组织成页面。脚本不调用业务模型或裁判模型。

EMF 可以理解为带有说明的 JSON 日志：“哪个数值应该成为哪个监控指标”。例如 AgentCore 给一次回答的 Helpfulness 打 0.83 分，CloudWatch 根据日志中的 EMF 信息，将它记录到 `Builtin.Helpfulness` 指标。`Bedrock-AgentCore/Evaluations` 是这些指标所在的分类名称，即 namespace。

## 客户如何阅读 Dashboard

| 页面内容 | CloudWatch 展示的含义 |
|---|---|
| 各项平均分 | CloudWatch 对选定时间范围内的同一指标计算 Average。它们各自独立，不是综合分。 |
| 内置指标趋势 | CloudWatch 展示三个已验证的 0–1 内置指标随时间的变化。 |
| 自定义指标趋势 | CloudWatch 单独展示 custom judge 量表；本项目是 1／3／5，满分 5。 |
| 样本数 | CloudWatch 每 5 分钟展示每项指标的 SampleCount。工具级评分可能比 trace 级评分多。 |
| 累计分数 | CloudWatch 每 5 分钟展示每项指标的 Sum；它不是不同指标加起来的业务总分。 |
| 评分／错误统计 | CloudWatch Logs Insights 按 evaluator 统计结果事件、数值样本、均分和评估错误。 |
| 最近评分与理由 | CloudWatch Logs Insights 展示 session、trace、分数、评分解释和错误信息，方便客户追查。 |

脚本支持本项目已验证的 Helpfulness、Correctness、ToolSelectionAccuracy 三项内置指标和一个 1–5 数值型 custom evaluator；其他组合需要开发者调整图表定义与量表校验。脚本用 `service.name + onlineEvaluationConfigId` 精确选择指标，因此 Dashboard 对应一个 Online 配置。日志查询同样限制到该配置的来源／结果组。CloudWatch 没有观测到数据时，页面保留空数据状态；客户不能把空白理解成零分或无错误。

CloudWatch 数字卡片统计整个选定时间窗口，趋势图按 5 分钟分桶。本次两个会话发生在同一个 5 分钟桶内，所以趋势图只有一个数据点；这不是图表故障。EMF 指标时间来自原始 trace 的结束时间；结果日志写入时间通常更晚。客户对账时应选择覆盖这两个时刻的时间范围，并检查日志与指标是否包含相同目标。

## 管理员从项目创建 Dashboard

管理员使用已有 setup 状态文件和 AWS 默认凭证链。管理员身份需要读取 Online 配置、evaluator 和 Dashboard，并允许 `cloudwatch:PutDashboard` 写入所选 Dashboard。脚本不会修改 IAM。客户打开 AWS Console 查看页面时，需要相应 CloudWatch 指标和日志查询权限。

```bash
# 管理员先生成本地 Dashboard JSON；应用核对 AWS 账号并读取 evaluator 名称。
python3 scripts/dashboard.py \
  --state "$WORK_DIR/aws-state.json" \
  --output "$WORK_DIR/dashboard.json"

# 管理员显式创建页面；CloudWatch 保存 Dashboard，应用保存验证结果和访问入口。
python3 scripts/dashboard.py \
  --state "$WORK_DIR/aws-state.json" \
  --output "$WORK_DIR/dashboard.json" \
  --apply
```

管理员可通过 `--start` 和 `--end` 指定带时区的 ISO 8601 历史范围，以便以后打开仍能看到实验数据。管理员若使用默认最近三小时范围，较早的测试记录会随着时间移出页面，但仍保存在日志组中。

CloudWatch Dashboard 是账号级资源；每个图表和日志查询仍使用状态文件里的 AWS Region。脚本遇到同名且内容不同的已有 Dashboard 会拒绝覆盖，管理员应检查已有页面或选择新的 `--name`。内容相同的重复请求不改写页面。

本项目保留 Dashboard、日志、评分、评估配置及已有错误记录。脚本没有清理流程。AWS Console 访问入口由脚本输出；包含真实账号及资源标识的 JSON、回执和检查链接保存在工作目录，不提交到 GitHub。

## 本次部署验证

实验执行者使用已保留的最终 Java 评估配置，选择 2026-09-16 06:35–06:55 UTC。该时间范围覆盖两次 Java 会话的结束时间和 Online 评分日志写入时间。实验执行者已创建并回读包含 11 个组件的 Dashboard，最终 PutDashboard 返回 0 条验证消息，重复运行脚本确认同内容不改写。两张日志表分别返回 4 个 evaluator 汇总行和 10 条评分明细；错误数为 0。CloudWatch 图片 API 已渲染四张指标图，实验执行者另用旧配置确认两条评估错误不会计入数值均分。

实验执行者通过 API 与指标图像完成验证，未将这些检查描述为已登录 AWS Console 的浏览器验收。CloudWatch 数字卡片使用 `setPeriodToTimeRange=true`，开发者测试确认其统计整个窗口。

Dashboard JSON 中的 `SOURCE '日志组'` 是控制台的日志组选择语法。开发者使用 StartQuery API 验证时，应单独传入 logGroupName 并执行其后的 QL；API 的 SOURCE logGroups(...) 语法与 Dashboard 定义不同。

项目包含[匿名 Dashboard JSON](../examples/dashboard/dashboard.example.json)和[指标图片预览](../examples/dashboard/README.md)。这些示例方便客户在 GitHub 检查可视化；真实部署 JSON、API 回执和 Console 链接保存在本地检查指南中。

官方参考：[Dashboard Body Structure](https://docs.aws.amazon.com/AmazonCloudWatch/latest/APIReference/CloudWatch-Dashboard-Body-Structure.html)、[Metric Widget Image](https://docs.aws.amazon.com/AmazonCloudWatch/latest/APIReference/CloudWatch-Metric-Widget-Structure.html)。
