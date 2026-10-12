# Java Trace 封装与环境变量说明

本项目的 Java 代码使用 **OpenTelemetry Java 标准 API**。运行时由外置 **AWS Distro for OpenTelemetry（ADOT）Java agent** 提供 SDK、共享 provider、自动埋点、OTLP 导出器及 AWS 签名能力。应用没有把 AWS 遥测服务调用散落在业务循环中：`AgentTelemetry` 统一处理 Trace，`MinimalAgent` 负责模型调用顺序和工具执行。

## Java 应用、ADOT 与评估服务各自负责什么

| 组件 | 该组件负责的工作 | 该组件的代码或运行位置 |
|---|---|---|
| `MinimalAgent` | Java 应用调用 Bedrock Converse，维护本次请求的消息历史，执行工具，并把业务输入、输出和用量交给遥测封装。 | `agent/src/main/java/example/agentcore/MinimalAgent.java` |
| `AgentTelemetry` | Java 封装通过 OTel 标准 API 创建 Agent、模型、工具 span，管理当前上下文、session baggage、属性、异常和结束时机。 | `agent/src/main/java/example/agentcore/AgentTelemetry.java` |
| OpenTelemetry Java API 1.65.0 | 标准 API 为 Java 封装提供 `Tracer`、`Span`、`Scope`、`Baggage` 等接口；API 本身不向 AWS 发送数据。 | Maven `io.opentelemetry:opentelemetry-api`，及其传递 API 依赖。 |
| ADOT Java agent 2.31.0 | ADOT 在 JVM 启动时注册全局 provider，为应用手工 span 与依赖库自动埋点提供共享 SDK，并批量签名导出遥测。 | 独立 `aws-opentelemetry-agent.jar`，由 `-javaagent` 加载。 |
| AWS SDK for Java 2.x | AWS SDK 从默认凭证链取凭证，为 Java 应用发出的 Bedrock Converse 请求签名。 | Maven `software.amazon.awssdk:bedrockruntime`。 |
| AgentCore Runtime | Runtime 校验调用者的 IAM SigV4 请求，托管容器，将会话 ID 转发给 Java HTTP 入口。 | AWS 托管服务。 |
| X-Ray 与 CloudWatch Logs | AWS 遥测服务接收 ADOT 提交的 spans，并按日志定向配置保存记录。 | 指定 Region 的遥测服务和日志组。 |
| AgentCore Evaluations | Evaluations 根据 Online 配置读取指定 service 的会话，调用 evaluator，并写入评分与错误记录。 | AWS 托管评估服务；Java 应用不自行计算这些评分。 |

`io.opentelemetry.*` 是开源标准包名，不是本项目自行定义的 SDK，也不是 AWS SDK 的包名。应用的生产 JAR 包含 OTel API，**不包含应用自行构建的 `SdkTracerProvider` 或 OTLP exporter**。测试使用的 `opentelemetry-sdk-trace:1.65.0` 设为 Maven `test` scope，只供单元测试建立本地 provider、验证 span 内容；Maven Shade 不把测试 SDK 打入生产 JAR。

容器入口实际使用以下结构启动 Java；ADOT JAR 和应用 JAR 是两个文件：

```bash
# 容器入口让 JVM 先挂载 ADOT，再启动应用 HTTP 服务。
java -javaagent:/app/aws-opentelemetry-agent.jar \
  -cp /app/app.jar example.agentcore.RuntimeServer
```

仅运行 `java -cp ... RuntimeServer` 不等于启用 ADOT。本项目在开始业务根 span 时检查 provider 是否真正记录 span；如果没有有效 provider，Java 应用会拒绝继续，避免模型已经执行却没有可评估的 Trace。

## 启动器如何传递配置

部署脚本 `runtime/deploy.py` 从新建 Eval state 读取 service、来源日志组和 Region，并将环境变量提交给 Runtime。Runtime 启动容器时提供这些变量；`runtime/entrypoint.sh` 随后合并资源属性、校验冲突、固定关键导出设置，最后启动 Java。ADOT 和 Java 应用分别读取各自支持的变量。

本地或 EC2 入口 `scripts/run.sh` 采用相同的 trace 导出结构。操作者可以用 `--state` 让启动器读取 Eval state；操作者也可以显式提供变量。`.env.example` 只是配置样例，脚本不会自动把它加载为环境。

下面的“必须”指本项目当前功能要求，不代表每个变量都是 OpenTelemetry 协议的强制字段。启动器已经提供默认值的项目通常无需操作者手工填写。

### 应用与会话变量

| 环境变量或输入 | 实际读取者 | 读取者的用途 | 当前值来源或默认值 | 本项目是否必须 |
|---|---|---|---|---|
| `AWS_REGION` | 启动器、Java 应用、AWS SDK/ADOT 的 AWS 配置层 | 启动器构造 Region 内的 OTLP 地址；Java 应用显式选择 Bedrock Region；AWS 组件使用对应 Region 完成服务调用和签名。 | 部署脚本写入 `us-east-1`；启动器回退到 `AWS_DEFAULT_REGION`，再回退 `us-east-1`。 | 必须有一致的有效 Region，默认已提供。 |
| `AWS_DEFAULT_REGION` | 本项目启动器、Java 应用，以及支持该变量的 AWS 工具 | 启动器和 Java 应用把它作为 `AWS_REGION` 的回退值。Java AWS SDK 不需要依赖这个变量自行推断，因为应用会显式传入 Region。 | 部署脚本与 `AWS_REGION` 设置相同值。 | 可选兼容项。 |
| `BEDROCK_MODEL_ID` | Java 应用 | Java 应用选择 Converse 调用的模型，并把模型 ID 交给遥测封装记录。 | `amazon.nova-lite-v1:0`。 | 应用有默认值；更换模型时还需调整 Runtime 角色权限。 |
| `MAX_MODEL_CALLS` | Java 应用 | Java 应用限制一次任务中调用模型的次数，避免工具循环无限继续。 | `5`，应用接受 `1..10`。 | 可选；这是业务调用预算，不是 trace 采样率。 |
| `CAPTURE_CONTENT` | Java 应用读取变量，`AgentTelemetry` 接收布尔配置 | 封装控制应用自行写入的 prompt、回答、模型消息、工具参数/结果及异常详情；应用仍向 HTTP 调用者返回回答。 | `true`；质量评估演示需要这些内容。 | 内容评估需要相应内容；关闭时会减少可供 evaluator 判断的证据。 |
| `SESSION_ID` | Java CLI 构造器 | CLI 为单次进程任务选择 session；CLI 未设置时生成新 ID。 | CLI 默认 `java-eval-<UUID>`。 | CLI 可选；**Runtime 不用它决定请求会话**。 |
| `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` | Java `RuntimeServer` | Java HTTP 入口校验 Runtime 转发的请求头，为每次请求创建独立 Agent，并将该 ID 传给遥测封装。 | 调用应用向 `InvokeAgentRuntime` 传入的 `runtimeSessionId`。 | Runtime 必须；它是 HTTP 请求头，不是环境变量。 |

`CAPTURE_CONTENT=false` **不是全局脱敏开关**。它只影响本项目应用明确控制的内容字段，不保证第三方自动埋点、AWS 服务日志、调用客户端保存的响应或其他库日志一并停止记录内容。运维人员需要分别检查这些组件的内容策略。

### 服务名与资源关联变量

| 环境变量 | 实际读取者 | 读取者的用途 | 当前值来源或默认值 | 本项目是否必须 |
|---|---|---|---|---|
| `OTEL_SERVICE_NAME` | ADOT，同时也由 Java 应用与启动器读取 | ADOT 用它标识 OTel `service.name`；Java 应用用它标识 Agent 名称及 `gen_ai.agent.name`；启动器让它与 Eval service 保持一致。 | 部署脚本读取 Eval state 的 `service`；两个启动器均要求有效值。 | 必须。这是**同一个变量的两种用途**，不是 Java 类名。 |
| `LOG_GROUP` | `runtime/entrypoint.sh` 或 `scripts/run.sh` | 启动器把这个项目自定义变量翻译为 `aws.log.group.names` 资源属性，以及 `x-aws-log-group` 导出请求头。ADOT 不是直接读取该变量来选择日志组。 | 部署脚本读取 Eval 来源日志组；否则启动器默认 `/aws/bedrock-agentcore/runtimes/<service>`。 | 需要有效目标；默认可生成，但必须与 Eval data source 一致。 |
| `OTEL_RESOURCE_ATTRIBUTES` | 启动器校验/合并，随后 ADOT 读取 | ADOT 把属性附加在 spans 的 resource 上，供 CloudWatch 与 Evaluations 识别应用和 Runtime。 | 部署脚本写入 `service.name`、`aws.service.type=gen_ai_agent`、`aws.log.group.names`；取得 endpoint 后补充 `cloud.resource_id=<Runtime endpoint ARN>`。 | 本项目会话发现和 Runtime 关联需要这些属性；启动器校验前三项一致性。 |
| `AGENTCORE_RUNTIME_ARN` / `BEDROCK_AGENTCORE_RUNTIME_ARN` | 容器入口 | 若运行环境提供其中之一，入口把值补入资源属性 `aws.resource.arn`。Java 业务逻辑不直接读取它们。 | 入口优先使用前者；未提供时不补该属性。部署脚本已另以 `cloud.resource_id` 写入准确 endpoint ARN。 | 可选兼容输入；不依赖二者一定由托管环境注入。 |

`service.name` 是资源属性；`gen_ai.agent.name` 是 span 属性；instrumentation scope 是 `AgentTelemetry` 创建 tracer 时给定的 scope 名称。三者职责不同。操作者不能仅设置 `OTEL_SERVICE_NAME` 就替代正确的 instrumentation scope 或 GenAI 操作类型。

### ADOT provider 与导出变量

| 环境变量 | 实际读取者 | 读取者的用途 | 当前值来源或默认值 | 本项目是否必须 |
|---|---|---|---|---|
| `OTEL_TRACES_EXPORTER` | ADOT 的 OTel SDK 自动配置 | ADOT 选择 trace 导出器。 | 启动器固定 `otlp`。 | 必须导出 traces。 |
| `OTEL_METRICS_EXPORTER` | ADOT 的 OTel SDK 自动配置 | ADOT 选择应用 SDK 的 metrics 导出器。 | 启动器固定 `none`。 | 非 trace 必需；不关闭 Runtime 自身服务指标，也不关闭 Eval 评分指标。 |
| `OTEL_LOGS_EXPORTER` | ADOT 的 OTel SDK 自动配置 | ADOT 选择应用 SDK 的 logs 导出器。 | 启动器固定 `none`。 | 非 trace 必需；**不关闭 Java stdout/stderr，也不关闭 Runtime 容器日志**。 |
| `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` | ADOT OTLP exporter | ADOT 决定 OTLP traces 的传输编码。 | 启动器固定 `http/protobuf`。 | 与本项目目标 endpoint 配套。 |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | ADOT OTLP exporter 与 AWS 签名扩展 | ADOT 把 traces 发送到指定 AWS 接收地址。 | 部署脚本提供 `https://xray.<region>.amazonaws.com/v1/traces`；启动器也提供相同回退值。 | 必须有有效出口。更换地址时还要验证鉴权和协议。 |
| `OTEL_EXPORTER_OTLP_TRACES_HEADERS` | ADOT OTLP exporter；X-Ray/CloudWatch 接收链路解释 AWS 日志定向头 | ADOT 随导出请求发送 `x-aws-log-group=<group>,x-aws-log-stream=spans`，AWS 服务按目标日志组保存 spans。 | 容器入口根据 `LOG_GROUP` 强制生成；本地入口允许传入但校验日志组一致和 stream 非空。 | 本项目直接向独立 Eval 来源日志组投递时需要；日志组还必须授权 X-Ray 写入。 |
| `OTEL_TRACES_SAMPLER` | ADOT 的 OTel SDK 自动配置 | ADOT 决定应用 span 是否记录。 | 启动器固定 `always_on`。 | 本项目要求有效且被记录的根 span，以便验收和评分。 |
| `OTEL_AWS_APPLICATION_SIGNALS_ENABLED` | ADOT Java AWS Application Signals 扩展 | ADOT 控制 Application Signals 功能。 | 启动器固定 `false`。 | 当前方案关闭此功能；手工 GenAI span 的 OTLP 导出仍然启用。 |
| `OTEL_BSP_SCHEDULE_DELAY` | ADOT SDK 的 BatchSpanProcessor | ADOT 控制后台批量导出间隔，单位毫秒。 | `1000`；操作者可覆盖。 | 可选性能设置；不是 Eval 评分等待时间。 |
| `OTEL_BSP_EXPORT_TIMEOUT` | ADOT SDK 的 BatchSpanProcessor | ADOT 限制单次导出等待时间，单位毫秒。 | `30000`；操作者可覆盖。 | 可选超时设置；不控制 Bedrock 推理超时。 |
| `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT` | ADOT 的 OTel SDK 自动配置 | ADOT 限制字符串属性保留长度，过长属性可能被截断。 | `131072`；操作者可覆盖。 | 可选容量设置；截断模型或工具内容可能降低评估证据完整性。 |

ADOT 导出成功还依赖 Runtime 执行角色的 X-Ray 权限、目标日志组的资源策略和账号内 Transaction Search 配置；这些环境变量不会创建 IAM 权限或自动证明数据已到达。部署执行者需要用实际 Session/Trace 与评分记录完成验证。

### 保留的跨语言提示：不能当作 Java 功能保证

| 环境变量 | 当前设置者 | 项目当前值 | 本次证据能说明什么 | 本项目是否依赖其生效 |
|---|---|---|---|---|
| `AGENT_OBSERVABILITY_ENABLED` | 部署脚本与启动器 | 默认 `true`。 | 这是沿用原流程的跨语言配置提示；本项目没有独立证据证明 ADOT Java 2.31.0 会读取它。 | 不把它作为 Java provider、GenAI span 或 exporter 已启用的依据。 |
| `AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT` | 部署脚本与启动器 | 默认 `true`。 | 官方 Python ADOT 指南出现过此变量，但本项目没有独立证据证明 Java agent 对它实现相同语义。 | 不把它作为“保留内容”或“已脱敏”的有效保证；应用手工内容字段由 `CAPTURE_CONTENT` 控制。 |

### 启动、构建与凭证变量

| 环境变量 | 实际读取者 | 用途与当前值来源 | 本项目是否必须 |
|---|---|---|---|
| `ADOT_JAVA_AGENT_JAR` | 本地 `scripts/run.sh`、镜像 `runtime/build.sh` | 构建执行者提供官方 ADOT JAR 路径；脚本检查固定 2.31.0 SHA-256。容器内固定使用 `/app/aws-opentelemetry-agent.jar`，容器入口不读取此路径变量。 | 本地/镜像构建需要；容器运行时无需另填。 |
| `WORK_DIR`、`JAVA_BUILD_DIR` | 构建/运维脚本 | 脚本把依赖缓存、构建产物与证据保存在指定工作目录；`JAVA_BUILD_DIR` 默认由 WORK_DIR 派生。 | 构建需要有效目录；不是 ADOT exporter 配置。 |
| `TMPDIR`、`JAVA_TOOL_OPTIONS` | 启动器/JVM | 容器入口设置 `/app/tmp` 并传 `-Djava.io.tmpdir`；本地入口把 JVM 临时目录设置到工作目录。 | 用于临时文件位置；不是会话上下文。 |
| `AWS_PROFILE` | 本地 Python 管理脚本、支持 profile 的 AWS 默认凭证链 | 本地操作者可选择授权的 AWS profile；容器业务使用 Runtime 执行角色提供的短期凭证。 | 本地可选；Runtime 无需复制操作者 profile。 |
| AWS 容器凭证相关变量 | AWS SDK/ADOT 所用 AWS 凭证提供器 | Runtime 向容器提供角色凭证入口，默认凭证链据此取得和刷新短期凭证；应用不把 Access Key 写入源码或部署 state。 | 需要可用凭证链，但操作者不应在此示例中手工塞入长期密钥。 |

## 应用封装的使用边界

Java 业务代码通过 `AgentTelemetry` 的三个入口描述 Agent、模型与工具操作。`MinimalAgent` 不再导入 `io.opentelemetry.*`，也不直接拼写 `gen_ai.*` 属性。

| 封装入口 | Java 业务层负责什么 | 遥测封装负责什么 |
|---|---|---|
| `telemetry.agent(task, action)` | 业务回调创建并关闭 Bedrock 客户端、运行对话、检查工具要求，通过 `run.outcome(...)` 提交结果。 | 封装创建 `invoke_agent` 根 span、设置 session baggage、记录结果；只有业务回调正常返回，封装才标记成功。 |
| `telemetry.chat(request, action)` | 业务层构造真实 `ConverseRequest`；回调执行 `client.converse(request)` 并更新业务计数。 | 封装从真实 request/response 读取配置、消息、用量和请求 ID，记录 `chat` span，返回原模型响应。 |
| `telemetry.tool(call, action)` | 业务回调执行本地工具，返回 `ToolOutcome.success(...)` 或可恢复的 `ToolOutcome.failure(...)`。 | 封装记录 `execute_tool` span，把参数/结果关联到模型生成的 tool-call ID，并转换为 Bedrock `toolResult`。 |

例如，业务层在已构造 `request`、已创建 `client` 后，仅需这样包装一次模型调用：

```java
// Java 业务回调负责真实 Bedrock 调用与业务计数；封装负责 span 的上下文、属性和结束。
ConverseResponse response = telemetry.chat(request, () -> {
    modelCalls++;
    return client.converse(request);
});
```

业务层在构造器中建立本次请求专属封装：

```java
// Java 业务层读取环境并显式传入会话、模型、Region、Agent 名称和内容采集策略。
this.telemetry = new AgentTelemetry(
    sessionId, modelId, region, agentName,
    Boolean.parseBoolean(env("CAPTURE_CONTENT", "true")));
```

这些入口目前供 `example.agentcore` 包内业务代码使用，尚不是独立发布的通用遥测 SDK；封装仍理解本项目的 Bedrock Converse、工具 schema 和 GenAI 内容结构。封装负责 OTel span 生命周期，ADOT 负责 provider 生命周期和批量导出；Java 业务代码不创建或关闭第二个 SDK，也不在每个 HTTP 请求结束时关闭全局 provider。

Java HTTP 入口每次创建独立业务 Agent，遥测封装随该实例接收 Runtime session ID。封装把同一 ID 写入 `session.id`、`gen_ai.conversation.id` 与 baggage，调用方可以用该 ID 对齐 Runtime 请求、业务 spans 和 Evaluations 记录。封装并不保存跨请求对话历史，也不执行质量评分。
