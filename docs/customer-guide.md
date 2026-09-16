# Java 接入与评分说明

本页提供快速接入说明。完整代码片段、格式反例、实测结果和复现顺序见 [AgentCore-Evaluation-Java接入与评分说明](AgentCore-Evaluation-Java接入与评分说明.md)。

## 客户现状与接入目标

客户在 AgentCore Runtime 外运行 Java／Spring Agent。客户希望 AWS 接收其遥测，AgentCore Evaluations 评价回答与工具使用，CloudWatch 提供可检查的评分和趋势。客户不需要为了这条链路把业务 Agent 改写成 Python。

## Java 应用和 AWS 服务如何协作

1. Java 应用为每个会话分配 `session.id`，在每次用户 turn 创建 Agent 根 span。
2. Bedrock 业务模型请求工具；Java 应用执行工具并把实际结果返回模型。
3. Java 应用通过 `GlobalOpenTelemetry` 记录 Agent、chat 和 execute_tool spans，复用 ADOT 的 provider。
4. ADOT Java Agent 使用 AWS 默认凭证链向 `https://xray.<region>.amazonaws.com/v1/traces` 执行 SigV4 签名并导出。
5. X-Ray 向指定 CloudWatch 日志组交付 spans；AgentCore Evaluations 发现完成的会话并读取内容。
6. LLM judge 按 evaluator rubric 作出判断；AgentCore Evaluations 写出带理由和 EMF 的评分日志；CloudWatch 从日志自动提取指标并计算同指标统计。

ADOT Java Agent 是 JVM instrumentation 组件，不是业务 Agent。CloudWatch 官方文档说明 Java collector-less 功能要求 ADOT Java ≥2.11.2；本项目实际验证的是 2.30.0。AgentCore 文档明确 ADOT Collector 不受支持用于外部 Agent Observability，不能把通用 CloudWatch Collector 示例当作本链路的推荐配置。

## 管理员配置权限和资源

管理员先在目标 Region 启用 Transaction Search，再创建来源日志组和 `spans` stream。脚本只检查 Transaction Search 是否已经可用，不修改其账号级设置。

Java 运行角色需要 X-Ray 写入和业务模型调用权限。CloudWatch 官方接入文档以 AWS 托管策略 `AWSXrayWriteOnlyPolicy` 作为 trace 写入的配置起点；管理员还需允许应用调用所选 Bedrock 模型。此示例不自动修改客户的 Java 运行角色。X-Ray 将 spans 交付自定义日志组时，还需要日志资源策略允许 `xray.amazonaws.com` 执行 `logs:PutLogEvents`。管理员应限制日志 ARN、SourceAccount 和 X-Ray SourceArn。

Online Eval 使用独立执行角色：AgentCore Evaluations 通过该角色读取来源日志、写评分、管理必要索引并调用 custom judge。`logs:DescribeLogGroups` 是日志组目录查询；本次使用本账号、Region 的目录 ARN，日志内容读取仍限制到指定来源和兼容的 `aws/spans`。角色权限与信任策略在带注释的配置脚本中可检查。

本项目默认保留所有创建的资源，来源和结果日志均不设置自动过期。用户随后明确要求清理之前，管理员不得删除这组测试资源。

## Java 团队补齐三类业务 spans

| 位置 | Java 团队／运维团队提供的内容 |
|---|---|
| OTel resource | 运维人员设置 `service.name`、`aws.service.type=gen_ai_agent`、`aws.log.group.names`；ADOT 将属性写在 resource 上。 |
| Scope | Java 应用调用 `GlobalOpenTelemetry.getTracer("opentelemetry.instrumentation.java_agentcore_demo", "1.0.0")`。generic parser 识别 scope 前缀，不能把自定义 scope 随意命名为 `mycompany.agent.tracing`。 |
| 所有业务 spans | Java 应用写相同的 `session.id`，维护 trace 和 parent/child 关系。 |
| invoke_agent | Java 应用写 `gen_ai.operation.name=invoke_agent`、`gen_ai.task.input`、`gen_ai.task.output`。 |
| chat | Java 应用写 `gen_ai.operation.name=chat`、`gen_ai.input.messages`、`gen_ai.output.messages`、model ID、system instructions、tool definitions 和实际 token usage。 |
| execute_tool | Java 应用写 `gen_ai.operation.name=execute_tool`、工具名称／调用 ID、`gen_ai.tool.call.arguments` 与 `gen_ai.tool.call.result`。 |

本项目采用统一遥测：Java 把业务内容保留在 spans 内，AgentCore 直接从 spans 读取。generic parser 按约定字段提取字符串，不会理解客户自行包装的任意请求 envelope。

`aws.service.type=gen_ai_agent` 的具体 Online 发现条件来自本次 CloudTrail 和查询结果实证，不能把它描述成所有 Java instrumentation 都会自动设置的属性。本次 Java 配置需要运维人员显式补充；客户应检查最终落地内容。如果已有 `OTEL_RESOURCE_ATTRIBUTES`，客户必须合并新增字段，避免环境变量覆盖脚本默认值。

Spring 团队可以把根 span 放在 Agent Service 的调用边界，把 chat／tool spans 放在模型和工具执行边界。HTTP 自动埋点不能替代这些业务语义。Reactor、线程池和 `CompletableFuture` 还需要传播 OTel Context，并在异步工作真正完成时结束 span。本项目未验证某个 Spring AI 或 LangChain4j 版本的零代码覆盖。

## Online 结果与汇总由谁提供

AgentCore Evaluations 默认将评分日志写入 `/aws/bedrock-agentcore/evaluations/results/<config-id>`。AgentCore Evaluations 写出的数值评分日志自带 **EMF（Embedded Metric Format，嵌入式指标格式）**。客户可以把 EMF 理解为：**带有“哪个数值属于哪个监控指标”说明的 JSON 日志**。AgentCore Evaluations 在同一条日志中记录评分、理由和指标说明；CloudWatch 按照这些说明自动提取数值，形成 Metrics 中的监控指标。客户应用无需为了这条评分链路再上传一份指标。

以 Helpfulness 得到 `0.83` 为例，各组件依次完成以下动作：

1. AgentCore Evaluations 调用 Helpfulness evaluator，LLM judge 按评价标准给出判断与理由，AgentCore Evaluations 写出数值为 `0.83` 的评分结果。
2. AgentCore Evaluations 在 JSON 日志中同时记录评分理由、`"Builtin.Helpfulness": 0.83`，以及 `_aws.CloudWatchMetrics` 中的 EMF 指标说明。
3. CloudWatch 读取 EMF，把 `0.83` 记入 `Bedrock-AgentCore/Evaluations` 下的 `Builtin.Helpfulness` 指标，并按日志声明的维度区分服务和评估配置。
4. 分析人员在 CloudWatch Metrics 中选择同一指标、同一组维度和统计周期；CloudWatch 对多次评分计算 `Average`，并按时间展示趋势。例如两次评分均为 `0.83` 时，CloudWatch 返回 `Average=0.83`、`SampleCount=2`、`Sum=1.66`。

客户查看指标时，应区分以下四项：

| 概念 | 本例内容 | CloudWatch 如何使用，客户如何理解 |
|---|---|---|
| Namespace（命名空间） | `Bedrock-AgentCore/Evaluations` | CloudWatch 用它给指标分类；客户可以把它理解为分类目录。AgentCore Evaluations 默认在 EMF 中声明此目录，它本身不是评分项。 |
| Metric name（指标名） | `Builtin.Helpfulness` | CloudWatch 用它识别具体评分项。本例中，AgentCore Evaluations 声明的指标名与 evaluator 名相同。 |
| Dimensions（维度） | `service.name` + `onlineEvaluationConfigId` | CloudWatch 用这两个维度的名称和具体值，区分“哪个服务、哪份 Online 评估配置”的评分序列。分析人员必须同时选择对应服务名和配置 ID。 |
| Value（数值） | `0.83` | CloudWatch 从日志中的 `Builtin.Helpfulness` 字段读取本次数值；分析人员不能把数值当作指标名或维度。 |

AgentCore Evaluations 还会在 EMF 中声明其他维度组合，例如增加 `label` 的组合。CloudWatch 将不同维度组合分别作为指标序列；分析人员对账时应固定同一组维度名称和具体值，不能把这些序列相加，否则可能重复计入同一条评分。在同一账号和 Region 内，CloudWatch 以 namespace、metric name 和完整维度组合共同识别一条指标序列。

分析人员查看 **CloudWatch Logs** 时，可以逐条读取 `gen_ai.evaluation.explanation` 中的评分理由，并根据 session／trace 标识检查低分或排查评估错误。分析人员查看 **CloudWatch Metrics** 时，可以观察同一指标的平均水平和随时间的变化；Metrics 中的数值曲线不包含逐条文字理由。

CloudWatch 对同一指标序列在指定统计周期内计算 `Average`（均值）、`SampleCount`（数值样本数）和 `Sum`（该指标各次评分之和）。CloudWatch 不会自动按 session 去重，也不会自动把不同 evaluator 合成为业务总分。客户分析应用不能直接加总 Helpfulness 的 0–1 分值与 custom judge 的 1–5 分值；客户需要总分时，应按下一节定义统一规则。

分析人员在结果日志组选择 Logs Insights QL，运行以下查询即可得到每 evaluator 的数值评分事件统计：

```sql
# 分析人员统计数值评分事件；count(*) 不进行目标去重。
fields jsonParse(@message) as r
| filter r.name = "gen_ai.evaluation.result"
| filter ispresent(r.attributes.`gen_ai.evaluation.score.value`)
| stats count(*) as score_events,
        avg(r.attributes.`gen_ai.evaluation.score.value`) as average_score,
        min(r.attributes.`gen_ai.evaluation.score.value`) as min_score,
        max(r.attributes.`gen_ai.evaluation.score.value`) as max_score
  by r.attributes.`gen_ai.evaluation.name` as evaluator
```

分析人员应另行统计错误、N/A、没有数值的分类结果与覆盖率，不能一律当作零分。

**分析人员对账时，还需要区分业务时间与评分日志写入时间。** 本次实测中，AgentCore Evaluations 将原 trace 的结束时间写入 EMF 的 `_aws.Timestamp`（Unix 毫秒）；CloudWatch 用这个时间把评分归入 Metrics 的统计周期。AgentCore Evaluations 在稍后完成评分并记录 `observedTimeUnixNano`（Unix 纳秒），CloudWatch 接收的 log event 时间也在稍后；本次这两个时间比原 trace 结束时间晚约 10 分钟。分析人员不能把 `_aws.Timestamp` 当作评分完成或日志写入时间，也不能把约 10 分钟理解为固定延迟或 SLA。

CloudWatch Logs Insights 的界面时间范围按日志事件时间筛选，因此分析人员直接使用同一组起止时间查询 Logs 和 Metrics，可能得到不同数量。分析人员核对 Metrics 时，应先让日志查询范围覆盖稍后写入的评分，再按每条日志的 `_aws.Timestamp` 归入相同业务时间窗口，并固定 evaluator、维度和统计周期；分析人员核对评分处理延迟时，则应比较原 trace 结束时间与 observed／log event 时间。AgentCore Evaluations 还需要异步发现、读取会话和执行 judge，管理员配置的 1 分钟 session timeout 不代表服务会在 1 分钟内返回评分。

## 自定义量表和最终总分

Custom LLM judge 的 `ratingScale` 可选 `numerical` 或 `categorical`。数值定义包含 value、label 和 definition，API 不要求上限为 1。本项目注册 1／3／5 锚点，实测服务保留 5.0。

数值锚点不等于应用端强制枚举约束。结果 API 将 value 描述为量表范围内的 decimal；客户若只接受指定值，应由结果消费应用校验。TRACE instructions 至少包含一个该层级支持的 placeholder，例如 `{context}` 或 `{assistant_turn}`。Online 流量没有逐条 ground truth，因此管理员不能使用依赖 `{expected_response}` 等 reference placeholders 的 custom evaluator。

管理员应在 instructions 中明确裁判角色、评价标准和证据边界。AgentCore 会自动追加要求 reason 与 score 的标准化提示，管理员不应再加入互相竞争的输出格式指令。本次复跑出现过 `No score found in evaluation result`；分析应用必须把这种事件作为评估错误统计，不能记成零分或忽略覆盖率。一次成功调用也不能证明裁判在所有输入上稳定。

Builtin、custom LLM、code-based evaluator 应分别按自身契约解读。Helpfulness 的 prompt 档位编号与运行时分值可能不同；客户不能因为档位编号为 0–6，就把实际返回的 0.83 再除以 6。本项目没有测试 Lambda code-based evaluator 的越界行为。

如果客户需要总分，客户分析应用必须确定归一化、方向、权重、层级对齐、缺失值和关键失败门槛。例如应用可以把正向 1–5 指标转为 `(score - 1) / 4`，再和已经是 0–1 的正向指标组合。该规则属于客户应用，不是 AgentCore 内置加权。

## 官方依据

- [CloudWatch：ADOT collector-less 与 Java 版本](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-OTLP-UsingADOT.html)
- [AgentCore：外部 Observability、Collector 限制与日志策略](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html)
- [Generic framework support](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/supported-frameworks-generic.html)
- [Unified／split telemetry](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/supported-frameworks-telemetry.html)
- [Eval 结果与输出](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/results-and-output.html)
- [RatingScale API](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_RatingScale.html)
- [NumericalScaleDefinition API](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_NumericalScaleDefinition.html)
- [EvaluationResultContent API](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_EvaluationResultContent.html)
- [Custom evaluator 与 placeholders](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/create-evaluator.html)
