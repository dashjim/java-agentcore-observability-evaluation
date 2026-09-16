# Java Agent 接入 AgentCore Observability 与 Evaluations

客户技术说明与验证报告 · 更新日期：2026-09-16 · 验证 Region：us-east-1

> 维护者将本文件作为完整客户说明与验证报告，补入原技术说明的最新修订。本文包含 Java 接入细节、评分解释、实测结果和复现步骤；当前仓库脚本及保留状态以本版为准。公开文档省略真实账号、会话、Trace 和资源 ID，实验执行者通过单独检查指南提供账户内访问入口。

相关入口：[快速接入说明](customer-guide.md)、[Dashboard 与可视化](dashboard.md)、[验证记录](validation.md)、[安全检查](security-review.md)。


## 1. 客户问题与结论

客户在 AgentCore Runtime 之外运行 Java Agent，希望 AWS 接收其运行遥测，AgentCore Evaluations 自动评价 Agent 的回答和工具调用，并提供可监控的评分。

| 客户问题 | 答复 |
|---|---|
| 第三方 Java Agent 能否接入？必须迁移到 AgentCore Runtime 吗？ | 可以接入，无须迁移。Java 应用输出符合约定的 OpenTelemetry GenAI spans，ADOT Java Agent 负责向 AWS 导出，AgentCore Evaluations 从 CloudWatch 读取。 |
| ADOT 能否直接发送？ | **可以。ADOT Java Agent 支持无 Collector 直连 AWS OTLP 端点，并通过 AWS 凭证链执行 SigV4 签名。**本次使用实例角色和 Java Agent 2.30.0 实测成功。CloudWatch 文档给出的 Java 直连功能最低版本为 2.11.2；该版本要求仅说明传输功能，不代表所有新 GenAI 能力都在该版本通过了本次测试。[1] |
| 加一个 Java agent JAR、上传普通日志就能 Eval 吗？ | **还不够。**ADOT 解决采集与传输；Java 应用或框架 instrumentation 必须提供可识别的 Agent、模型、工具 span 及其内容。普通 stdout、Logback 日志、HTTP 耗时不能自动补成可评估的完整对话。[2][3] |
| Online Eval 有没有汇总分数？ | **有每个 evaluator 的统计和趋势。**CloudWatch 可对同一 evaluator 的多次评分计算 Average、SampleCount、Sum、Minimum、Maximum；控制台展示配置概览及各指标明细。[5][6] |
| 不同指标会不会自动加总成一个最终分数？ | **服务分别返回各 evaluator 的分数，没有为所选指标自动生成统一加权总分的公开接口约定。**应用若需要总分，应自行定义指标方向、归一化、权重与缺失值处理。AWS 明确采用多维独立评估，以便定位问题。[7] |
| 自定义评分是否约定为 0–1？ | **Custom LLM-as-a-judge 不限于 0–1。**客户可以定义 1–5 等数值量表，也可以定义分类量表。本次注册 1/3/5 数值锚点，服务实际返回 5.0，没有自动转换为 1.0。[8][9] |

这里的“ADOT Java Agent”是 JVM instrumentation 组件；“业务 Agent”是客户的 Java 应用与模型协作完成任务的系统。两者的职责不同。AgentCore 文档明确说明，**ADOT Collector 不受支持用于 Agent Observability**；不能把 CloudWatch 通用 Collector 接入示例等同于 AgentCore 推荐接入方式。本方案采用 ADOT Java Agent 直接导出。[2]

## 2. Java 应用、ADOT 与 AWS 服务各自负责什么

本方案采用统一遥测：Java 应用把提示词、最终回答和工具输入输出写在 span attributes 内；ADOT 把 spans 发送到指定日志组。AgentCore Evaluations 读取这些 spans，重建会话，再调用 evaluator。Python 旧版本常见的 split 模式把大字段分到 event records；服务也支持这种模式，但本次 Java 实验没有使用独立 payload event records。[4]

| 组件 | 本方案中由该组件完成的动作 |
|---|---|
| Java 应用／编排器 | 应用接收请求、分配并传播 session ID、调用模型、执行工具、维护父子 span、记录实际输入输出。 |
| Bedrock 业务模型 | 模型决定调用哪个工具；模型根据应用返回的工具结果生成回答。 |
| ADOT Java Agent | ADOT 提供 OTel provider、自动采集基础设施调用、补充 AWS 属性、批量导出 spans，并使用实例角色的临时凭证签名。 |
| X-Ray OTLP 接收端与 CloudWatch | AWS 接收 spans；X-Ray 按配置向 CloudWatch 日志组交付；CloudWatch 存储并提供 Transaction Search／Logs Insights 查询。 |
| AgentCore Evaluations | 服务按 Online 配置发现并抽样完成的会话，使用执行角色读取遥测，调用配置的 evaluator，把各评分结果写入 CloudWatch。 |
| LLM judge | 评审模型依据具体 evaluator 的 rubric 对指定 TRACE、SESSION 或 TOOL_CALL 做出判断。 |
| CloudWatch Metrics／客户分析应用 | CloudWatch 按单个指标及时间窗口计算统计；客户分析应用自行计算跨指标业务总分（如需要）。 |

传输顺序：**Java 应用创建 GenAI spans → ADOT Java Agent 签名并导出 → X-Ray 将 spans 交付 CloudWatch → AgentCore Evaluations 读取并调用 evaluator → LLM judge 作出质量判断 → AgentCore Evaluations 写出评分事件 → CloudWatch 保存事件并提取指标。**

## 3. 客户工程团队如何接入 Java／Spring 应用

### 3.1 AWS 管理员准备接收端和权限

1. 管理员在目标 Region 启用 CloudWatch Transaction Search。本次账号原先已经启用，实验没有修改其全局采样或目的地设置。
2. 管理员为该 Java Agent 创建 CloudWatch 日志组，例如 `/aws/bedrock-agentcore/runtimes/customer-java-agent`，并创建 `spans` stream。该命名用于外部 Agent 的遥测组织，**不表示 AWS 已创建 AgentCore Runtime 资源**。
3. 管理员为 Java 应用运行角色授予 X-Ray 写入权限。官方给出的起点是 `AWSXrayWriteOnlyPolicy`；ADOT 使用默认 AWS 凭证链。本机实验使用 EC2 instance role，没有配置长期 Access Key。若应用另行导出 OTLP logs，管理员还需授予对应 CloudWatch Logs 权限并创建日志 stream。[1]
4. 管理员为目标日志组添加资源策略，允许 `xray.amazonaws.com` 执行 `logs:PutLogEvents`，同时限制 SourceAccount、SourceArn 和日志组 ARN。**只有 Java 运行角色的 X-Ray 权限还不够：X-Ray 交付到自定义日志组也需要这条资源策略。**[2]
5. 管理员为 Online Eval 创建单独的执行角色，信任 `bedrock-agentcore.amazonaws.com`。该角色需要读取来源日志、写入评分结果、管理评估所需索引，以及调用自定义 judge 模型的权限。[10]

管理员可使用仓库中的 [`scripts/setup_evaluation.py`](../scripts/setup_evaluation.py) 参考本次通过验证的权限配置。脚本将日志数据的读写范围限制在测试来源、兼容的 `aws/spans` 读取范围和本配置的结果组；`DescribeLogGroups` 单独使用本账号、Region 的日志组目录 ARN。实验曾因把这个目录查询权限限制到单个命名日志组而收到校验拒绝；扩大该目录查询权限后，服务成功创建配置。目录查询权限不等于读取所有日志内容。

### 3.2 Java 应用补齐业务语义

Java 团队优先检查当前框架已经输出的 spans。若框架没有满足下表，Java 团队在应用编排层补充 instrumentation。Spring HTTP 自动埋点可以描述请求，但无法单凭 HTTP span 表达某次模型判断、工具参数和 Agent 最终答案。

| Java 应用写入位置 | 应用需要写入的关键内容 |
|---|---|
| Instrumentation scope | 应用使用 `opentelemetry.instrumentation.<自定义名称>`；本次为 `opentelemetry.instrumentation.java_agentcore_demo`。`scope.name` 不是普通 span attribute，也不是 `service.name`。 |
| OTel resource 属性 | 运维团队通过环境变量配置 `service.name`、`aws.service.type=gen_ai_agent` 和实际日志组对应的 `aws.log.group.names`，ADOT 将这些值写入 resource。Online 会话发现会过滤 `aws.service.type`；仅有 scope 和会话内容还不够。 |
| 每个业务 span | 应用设置相同的 `session.id`；每个用户 turn 对应一个 trace；应用正确建立 parent/child 关系。不同会话使用不同 session ID。 |
| Agent 根 span | 应用设置 `gen_ai.operation.name=invoke_agent`、`gen_ai.task.input`、`gen_ai.task.output`，分别记录用户请求和真实最终回答。 |
| 模型调用 span | 应用设置 `gen_ai.operation.name=chat`、`gen_ai.input.messages`、`gen_ai.output.messages`、模型 ID；应用按实际调用记录 system instructions、工具定义及 token usage。 |
| 工具调用 span | 应用设置 `gen_ai.operation.name=execute_tool`、`gen_ai.tool.name`、`gen_ai.tool.call.id`、`gen_ai.tool.call.arguments`、`gen_ai.tool.call.result`。 |

AgentCore 的 generic parser 根据 scope 前缀和 `gen_ai.operation.name` 分类 span，再从上述字段读取内容；服务对 generic 字段进行字符串化，不会理解客户随意设计的多层 request envelope。[3]

Java 应用使用 ADOT 提供的全局 provider。应用不要再初始化第二套独立 SDK，以免业务 spans 与 Java agent 的导出配置脱节。最小的根 span 代码如下；随附项目还完整实现了模型和工具 spans：

```java
// Java 应用复用 ADOT 注册的 provider，并用可识别的 scope 创建业务 spans。
Tracer tracer = GlobalOpenTelemetry.getTracer(
    "opentelemetry.instrumentation.customer_java", "1.0.0");
Span span = tracer.spanBuilder("invoke_agent customer-java-agent")
    .setAttribute("gen_ai.operation.name", "invoke_agent")
    .setAttribute("session.id", sessionId)
    .setAttribute("gen_ai.task.input", userPrompt)
    .startSpan();
try (Scope current = span.makeCurrent()) {
    // Java 应用编排真实模型调用和工具执行，再记录模型生成的最终回答。
    String answer = application.invokeModelAndExecuteTools(userPrompt);
    span.setAttribute("gen_ai.task.output", answer);
} finally {
    // Java 应用在业务处理完成后结束根 span；ADOT 负责后续批量导出。
    span.end();
}
```

上例中的 `application.invokeModelAndExecuteTools` 是客户业务方法的占位符。Java 团队把根 span 放在 Controller／Service 的 Agent 调用边界，把 chat span 放在模型调用边界，把 tool span 放在工具执行边界。若 Spring 应用使用 Reactor、异步线程池或 `CompletableFuture`，Java 团队还需传播 OTel Context／baggage，并在异步工作完成时结束相应 span。本次实验使用同步 Java 程序，没有验证特定 Spring AI／LangChain4j 版本的自动埋点覆盖。

### 3.3 应用运维团队启动 ADOT Java 直连

运维团队从 AWS 官方发行页面取得 ADOT Java agent JAR。为了复现本次结果，团队可固定本次使用的 2.30.0；正式接入时团队应按自身版本管理流程选择版本。[1]

```bash
# 运维团队配置业务服务标识、GenAI resource 属性和 ADOT 直连端点。
export AWS_REGION=us-east-1
export OTEL_SERVICE_NAME=customer-java-agent
export OTEL_RESOURCE_ATTRIBUTES='service.name=customer-java-agent,aws.service.type=gen_ai_agent,aws.log.group.names=/aws/bedrock-agentcore/runtimes/customer-java-agent'
export OTEL_TRACES_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="https://xray.${AWS_REGION}.amazonaws.com/v1/traces"
export OTEL_EXPORTER_OTLP_TRACES_HEADERS='x-aws-log-group=/aws/bedrock-agentcore/runtimes/customer-java-agent,x-aws-log-stream=spans'
export OTEL_AWS_APPLICATION_SIGNALS_ENABLED=false
export OTEL_METRICS_EXPORTER=none
export OTEL_LOGS_EXPORTER=none
export OTEL_TRACES_SAMPLER=always_on

# 运维团队挂载 ADOT JAR；Java 应用继续运行自己的业务 Agent。
java -javaagent:/opt/adot/aws-opentelemetry-agent.jar -jar customer-agent.jar
```

**Java 必查项：本次 Java ADOT 没有自动写入 `aws.service.type=gen_ai_agent`。**实验执行者从 CloudTrail 看到 AgentCore 的 Online 发现查询明确过滤这个 resource 属性；缺失时，按需评分可以成功，但 Online 不会选中这些会话。Java 团队必须检查最终遥测中该属性出现在 `resource.attributes` 下，而不是仅写成普通 span attribute。

本段示例让 ADOT 导出带内容的 traces；应用无需再上传一份重复的 stdout 才能评价这些 spans。若客户还需要运行日志，运维团队可单独开启 OTLP logs，并配置 `https://logs.<region>.amazonaws.com/v1/logs` 及 logs headers。**OTLP traces 使用 X-Ray endpoint；OTLP logs 使用 Logs endpoint。两者不能互换。**

本实验完整启动脚本另外设置了 `AGENT_OBSERVABILITY_ENABLED=true` 和 `AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true`，并使用完整内容采集。实验验证的是整组配置，没有单独证明 Java 需要或处理这两个跨语言参数。AWS 文档中的 `aws-opentelemetry-distro>=0.18.0` 是 Python 包版本要求，客户不能将其套用成 Java JAR 的版本号。客户应检查 CloudWatch 实际落地的 span 是否保留了所需内容。当前仓库的启动脚本会合并 `OTEL_RESOURCE_ATTRIBUTES`，并在服务名、日志组或 `aws.service.type` 冲突时停止运行。客户在自己的启动流程中也应合并这些属性，避免覆盖必需字段。[2][4]

本次验证使用 100% trace 采样，方便对账。生产环境由客户选择 trace 采样和 Online Eval 抽样；前者决定 AWS 能看到什么，后者决定 Eval 从已收到的会话中评价多少。客户应避免 trace 采样造成会话缺失关键 turn 或工具调用。

### 3.4 评估管理员创建 Online Eval 配置

评估管理员先让少量测试 spans 到达 CloudWatch，再检查 scope、session 和内容。管理员通过按需 Eval 确认格式后，为新流量创建 Online 配置。统一遥测下，管理员填写 spans 实际所在日志组及与 resource `service.name` 完全一致的服务名：

```python
# 评估管理员通过控制面客户端创建配置；AgentCore 服务读取匹配的遥测并执行评分。
control.create_online_evaluation_config(
    onlineEvaluationConfigName="customer_java_online",
    dataSourceConfig={"cloudWatchLogs": {
        "logGroupNames": [
            "/aws/bedrock-agentcore/runtimes/customer-java-agent"
        ],
        "serviceNames": ["customer-java-agent"]
    }},
    rule={
        "samplingConfig": {"samplingPercentage": 100.0},
        "sessionConfig": {"sessionTimeoutMinutes": 1}
    },
    evaluators=[
        {"evaluatorId": "Builtin.Correctness"},
        {"evaluatorId": "Builtin.Helpfulness"},
        {"evaluatorId": "Builtin.ToolSelectionAccuracy"}
    ],
    evaluationExecutionRoleArn=execution_role_arn,
    enableOnCreate=True
)
```

`control` 是 `bedrock-agentcore-control` 客户端；`execution_role_arn` 是管理员准备的服务角色。本次脚本使用 Python 管理 AWS 资源，**业务 Agent、模型调用和工具编排全部由 Java 程序执行**。客户也可以使用控制台或相应 Java SDK 管理 Online 配置。

本段的 1 分钟 session timeout 和 100% 抽样仅用于短会话实验。AgentCore 还需要完成数据交付、会话判定与异步评分，因此 **1 分钟 timeout 不表示服务承诺 1 分钟内返回分数**。生产配置由客户根据真实会话时长和监控需求设定。若客户使用 split 模式，管理员还需检查 `aws/spans` 和 payload event 日志组，确保执行角色可读、traceId/spanId 可关联。[4][10]

## 4. CloudWatch 如何提供 Online Eval 汇总

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


当前接口也支持自定义结果目的地与 metrics namespace；管理员使用相关字段前，应确认当前 SDK 支持。本文的实测使用 boto3 1.43.6 和默认输出配置。分析人员可先用 ListMetrics 检查真实 namespace、metric name 与 dimensions，不应假设存在通用 Score metric 或 EvaluatorName dimension。[5]

客户可以在 CloudWatch 的 GenAI Observability → Bedrock AgentCore → 选择 Agent／endpoint → Evaluations 查看评估视图，也可以直接使用 Metrics 和 Logs Insights。本文的实测结论来自 API 查询、原始事件与指标图像，不将控制台导航说明当作已登录浏览器的验收记录。[5][6]

### 4.1 客户使用项目中的 Dashboard

项目包含 [`scripts/dashboard.py`](../scripts/dashboard.py)，该脚本把已有 EMF 指标与日志查询组织成 CloudWatch Dashboard。CloudWatch 页面分别展示四项均值、内置 0–1 与自定义 1–5 的趋势、各指标样本数与累计分数，以及评分理由和错误明细。脚本不触发业务模型或 judge，也不改变原始评分。

实验执行者已创建包含 11 个组件的 Dashboard，CloudWatch 最终返回 0 条验证消息；两张日志表分别返回 4 项指标汇总和 10 条评分明细。开发者同时在仓库提供[匿名 JSON 定义](../examples/dashboard/dashboard.example.json)与[可视化预览](../examples/dashboard/README.md)。创建步骤、历史时间范围和验证边界见 [Dashboard 说明](dashboard.md)。

## 5. 评估管理员如何设计自定义分数

### 5.1 Custom LLM judge 使用自己的量表

控制面参数为 `evaluatorConfig.llmAsAJudge.ratingScale`。管理员在 `numerical` 与 `categorical` 中选择一种；数值量表的每个定义包含 `value`、`label`、`definition`。API 对注册数值声明非负约束，没有声明 `max=1`。管理员不应把它称为一个只有 min/max 的 `numericScale` 参数。[8][9]

本次验证的量表示意如下：

```json
{
  "ratingScale": {
    "numerical": [
      {"value": 1, "label": "Incorrect", "definition": "回答错误或与工具证据矛盾"},
      {"value": 3, "label": "Partial", "definition": "回答部分有用但遗漏请求结果"},
      {"value": 5, "label": "Correct", "definition": "回答准确且与工具证据一致"}
    ]
  }
}
```

数值锚点不应被当作应用端严格的离散类型校验。官方结果 API 将 value 定义为量表范围内的 decimal；本次只验证返回 5.0，未验证所有中间值、越界值或强制枚举行为。客户若要求只接受指定值，应由消费结果的应用校验；分类量表更适合只需要业务标签的场景。[11]

TRACE 级 custom instructions 必须至少包含一个该层级支持的 placeholder，例如 `{context}` 或 `{assistant_turn}`；本次同时使用两者。Online 流量没有逐条人工提供的 ground truth，因此管理员不能将依赖 `{expected_response}` 等 reference placeholders 的 custom evaluator 直接用于 Online 配置。[12]

### 5.2 客户不要混淆 built-in、custom LLM 与 code-based

本次 built-in 的实际返回为 Correctness 1.0、Helpfulness 0.83、ToolSelectionAccuracy 1.0；自定义 LLM 返回 5.0。**客户应保留 evaluator ID、版本／配置、value、label、level 和 explanation，按该 evaluator 的实际尺度解读分数。**

某些 built-in 的 prompt 标签档位、`GetEvaluator` 元数据与运行时数值表示并不完全相同。例如 Helpfulness 的 prompt／元数据包含七档 0–6，而本次服务的 “Very Helpful” 结果是 0.83；客户不能仅按元数据最高值 6 再把运行时 0.83 除一次。评价“拒绝”等行为指标时，客户也必须先明确分值方向，不能默认所有高分都表示业务效果更好。

对于 Lambda code-based evaluator，AWS 示例采用 0–1；当前开发者指南将 0–1 表述为数值例子。本次没有运行 code-based evaluator，也没有测试其越界校验。客户若选择这种类型，可按 0–1 设计自身契约；该示例不能被推广为 custom LLM 的强制上限。[13]

### 5.3 客户分析应用若需总分，应先定义合成口径

应用可以先把正向 1–5 指标转换为 `(score - 1) / 4`，再与其他明确为 0–1、同为高分更好的指标按权重组合。这是**客户应用定义的规则**，不是 AgentCore 内置行为。客户还需确定：

- 应用先对齐到每条 trace、每个 session，还是按整个统计窗口计算；TOOL_CALL 条数通常多于 TRACE 条数，不能直接混合所有评分事件求均值。
- 应用如何处理缺失 evaluator、评价错误、不适用结果，以及抽样造成的覆盖差异。
- 应用是否对关键失败设置单独门槛，避免其他高分抵消关键缺陷。

客户也可以定义一个独立的 custom evaluator，让 judge 按整体 rubric 给出一项综合判断；该做法仍然是一个自定义指标，不表示服务会读取其他 evaluator 的分数并自动加权。

## 6. 验证执行者的实测结果

实验执行者在 EC2 本机运行 Java 程序；被测系统是 **Java GenAI instrumentation → ADOT Java 直连 → X-Ray／CloudWatch → AgentCore Evaluations**。Bedrock 业务模型 Amazon Nova Lite 决定工具调用；Java 应用从本地固定订单读取 `24.50 USD × 3`，通过十进制计算器实际得到 `73.5`，再把结果交给模型生成回答。演示订单不来自客户生产系统。

当前发布版本使用 OpenJDK 21.0.11、ARM64、ADOT Java 2.30.0-aws、OTel API 1.64.0、AWS SDK for Java 2.54.19、Jackson BOM 2.18.9 和管理脚本 boto3 1.43.6。Java 应用与 ADOT 使用 AWS 默认凭证链和 EC2 instance role，不在代码中保存长期凭证。

### 6.1 实验执行者核实的格式与按需结果

| 验证项 | 实测结果 |
|---|---|
| ADOT Java 直接发送，无 Collector | CloudWatch 指定日志组保存 Java spans；根 span 保留 task input/output，chat/tool spans 保留消息与工具输入输出。 |
| 早期按需 Correctness | 1.0，Perfectly Correct。judge 解释引用订单状态、单价、数量与计算器的 73.5 结果。 |
| 早期按需 Helpfulness | 0.83，Very Helpful。 |
| 早期按需 ToolSelectionAccuracy | 两条工具 span 各为 1.0，Yes。 |
| 按需 custom LLM 1／3／5 量表 | 服务返回 5.0，Correct，没有自动换算为 1.0。 |
| 应用普通 JSON 日志作为 Eval 输入 | 服务以 ValidationException 拒绝，指出缺少标准 span ID／起止时间等字段。 |
| 相同 spans 改成不被识别的 scope | 服务以 ValidationException 拒绝：没有 supported scope。实验执行者没有将该反例写入 CloudWatch。 |

每个完成的业务会话包含 6 个手工 GenAI spans：1 个 Agent 根 span、3 个模型调用 span、2 个工具调用 span；同一个业务 trace 另有 3 个 SDK 自动 spans。进程还会产生其他 trace 的基础设施 spans，分析人员不能把日志组或进程总事件数当作单一业务 trace 的 span 数。

### 6.2 当前发布脚本的两次 Online 验证

实验执行者用仓库的配置和启动脚本完成两次会话，Java 应用分别在 **06:39:20 UTC** 和 **06:39:41 UTC** 完成。实验执行者在 **06:50:16 UTC** 的采集中确认，AgentCore Online 写出 **10 条数值评分、0 条评估错误**；CloudWatch 统计与评分日志逐项一致。

实验执行者使用 `Bedrock-AgentCore/Evaluations` namespace、`service.name + onlineEvaluationConfigId` 维度，查询 06:35 UTC 至采集时刻的指标；Period=3600。该窗口内只有一个非空统计桶：

| evaluator | SampleCount | Average | Sum | Min／Max |
|---|---:|---:|---:|---|
| Builtin.Correctness | 2 | 1.0 | 2.0 | 1.0／1.0 |
| Builtin.Helpfulness | 2 | 0.83 | 1.66 | 0.83／0.83 |
| Builtin.ToolSelectionAccuracy | 4 | 1.0 | 4.0 | 1.0／1.0 |
| 自定义 1／3／5 judge | 2 | 5.0 | 10.0 | 5.0／5.0 |

这组结果直接回答了客户的两项评分问题：**Online 有各指标的汇总；custom 分值保留为 5，且 Sum=10 只是这个 custom 指标两次评分相加。** AgentCore 没有额外输出四个 evaluator 的统一最终总分。这 10 条事件对应 10 个 evaluator／目标组合；一般场景中，分析人员仍应区分数值样本数和唯一目标数。

### 6.3 实验执行者保留的问题与修复证据

- **会话发现条件：**实验执行者从实际查询中确认 `resource.attributes.aws.service.type=gen_ai_agent` 是过滤条件，运维团队为本次 Java 配置显式补齐该值。
- **日志目录权限：**管理员为执行角色提供本账号、Region 的 DescribeLogGroups 目录权限，日志内容读取仍限制到指定来源。
- **基础模型与 inference profile：**早期 custom judge 使用 `us.amazon.nova-lite-v1:0` 时虽能注册，评分却返回 inference profile not found；实验执行者改用 `amazon.nova-lite-v1:0` 后获得成功结果。这个个例不表示所有 inference profile 都不受支持。
- **自定义 judge 波动：**保留的排错组前两个会话出现 `No score found in evaluation result`，同一裁判后续重试和第三个 Online 会话又返回 5.0。排错组共保留 13 条数值评分和 2 条错误；最终发布组另有上述 10 条成功评分。实验执行者明确裁判角色和证据边界并采用 2048 tokens 上限后，最终配置成功完成两次 Online 评价，但现有证据不足以认定早期错误根因或保证所有输入稳定。
- **裁判输出与版本：**AgentCore 自动追加 reason／score 标准化提示，管理员不应另加竞争的输出格式指令。AgentCore 锁定被 Online 配置引用的 evaluator 后拒绝直接修改；实验执行者保留旧版本，另建版本验证。
- **资源级日志策略：**CloudWatch PutResourcePolicy 在资源级策略中只接受 resourceArn，不能同时传 policyName；开发者已修复脚本，并通过同一 state 的 --resume 完成实际创建。
- **异步等待：**新日志组的首次回看查询曾因查询结束时间早于日志组创建时间报错，后续查询恢复。会话结束与评分落地之间约十分钟的等待是本次观测值，不是 SLA，管理员不能把 1 分钟 session timeout 当作评分完成保证。

开发者已经通过 2 项 Java 工具契约测试，以及 14 项脚本安全测试和 8 项 Dashboard 测试。实验执行者保留两组 Online 配置及全部成功／失败记录；日志为 Never expire，Dashboard 固定的历史窗口覆盖业务结束与评分日志写入时间。完整证据口径见 [验证记录](validation.md)。

验证边界：本实验证明指定版本、Region 和遥测格式下的链路可行性；实验没有验证特定客户 Spring 代码、生产吞吐、所有 Java 框架的零代码覆盖，以及所有 built-in 的完整评分映射。客户无需为了本链路把业务 Agent 改写成 Python。

## 7. 交付代码与复现顺序

完整源码、中文注释和启动入口位于仓库根目录。工程团队按以下顺序复现，实际命令见 [README](../README.md)：

1. 工程团队准备 JDK 21、Python、可访问模型的 AWS 角色及已启用 Transaction Search 的 Region；管理员为工作文件设置独立 WORK_DIR。
2. 管理员运行 [`scripts/setup_evaluation.py`](../scripts/setup_evaluation.py)，为独立实验选择新的 --name 和 --state。脚本输出 service、日志组、evaluator 和 Online config ID，并拒绝覆盖已有 state；管理员只在恢复同一实验时使用 --resume。
3. 工程团队通过 [`scripts/run.sh`](../scripts/run.sh) 的 --state 参数读取输出配置，挂载校验过的 ADOT Java JAR，启动 Java 业务 Agent。Java 实现及测试位于 [`agent/`](../agent/)。
4. 分析人员运行 [`scripts/collect_evidence.py`](../scripts/collect_evidence.py)，先确认 CloudWatch 保存的业务 spans，再核对 Online 结果和 Metrics。脚本为评分错误单独保存记录，不把缺失分数当作零分。
5. 管理员按 [Dashboard 说明](dashboard.md) 运行 [`scripts/dashboard.py`](../scripts/dashboard.py)，用 --apply 创建可视化页面；固定历史时段时同时指定 --start 和 --end。项目的[匿名图表和 JSON 示例](../examples/dashboard/README.md)可供审阅。
6. **管理员保留实验资源与结果，直到用户明确要求删除。** 当前 state 包含 retain_until_user_requests_deletion=true；来源与结果日志为 Never expire，Online 配置保持 ACTIVE／ENABLED，Dashboard 和全部错误记录均保留。脚本没有自动清理流程；显式清理入口也会拒绝带有保留标记的 state。

公开仓库仅包含源码、说明、匿名评分事件和图表预览。实验执行者通过单独的账户检查指南提供真实 Console 链接、配置／会话／Trace ID 和原始证据，避免把运行账号与主机信息混入公开源码。客户复用时应按自己的环境生成新资源。

## 8. AWS 官方依据

以下资料核对于 2026-09-16。文中“官方支持”与“本次实测”分别以官方契约和实验原始结果为依据。

1. **ADOT SDK collector-less；Java ≥2.11.2、OTLP、凭证链**：https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-OTLP-UsingADOT.html
2. **外部 Agent Observability、Collector 限制、自定义 span destination 权限**：https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html
3. **Generic framework support：scope、span 分类、字段提取**：https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/supported-frameworks-generic.html
4. **Telemetry setup and delivery：unified／split 与评估内容**：https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/supported-frameworks-telemetry.html
5. **Online 结果日志、指标 namespace、输出配置**：https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/results-and-output.html
6. **CloudWatch Agent Evaluations 视图**：https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/session-traces-evaluations.html
7. **AWS 对独立多维评分的说明**：https://aws.amazon.com/blogs/machine-learning/build-reliable-ai-agents-with-amazon-bedrock-agentcore-evaluations/
8. **RatingScale API**：https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_RatingScale.html
9. **NumericalScaleDefinition API**：https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_NumericalScaleDefinition.html
10. **Evaluations 前提条件与执行角色**：https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/evaluations-prerequisites.html
11. **EvaluationResultContent API**：https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_EvaluationResultContent.html
12. **Custom evaluator prompt、placeholder 和 Online 限制**：https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/create-evaluator.html
13. **Code-based evaluator response schema**：https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-based-evaluators.html#code-based-response-schema
