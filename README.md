# Java Agent 接入 AgentCore Observability 与 Evaluations

这个示例让 Java 应用执行真实 Bedrock 工具调用，通过 ADOT Java Agent 直接把 GenAI spans 发送到 AWS，再由 AgentCore Online Evaluations 自动评分。Java 应用和所有运维脚本均包含中文注释。

最终仓库脚本实测：两个 Java 会话自动产生 10 条 Online 数值评分、0 条评估错误；CloudWatch 汇总与原始评分一致。实验执行者保留了 AWS 资源和全部成功／失败记录。

Java 应用负责调用顺序、工具执行和业务埋点；Bedrock 模型负责选择工具和生成回答；ADOT Java 负责使用默认 AWS 凭证链签名并导出；AgentCore Evaluations 负责读取会话、调用 judge 和写出评分；CloudWatch 负责存储与同指标统计。

AgentCore Evaluations 的数值评分日志自带 **EMF（Embedded Metric Format）**，即带有“哪个数值属于哪个监控指标”说明的 JSON 日志。AgentCore Evaluations 写入 Helpfulness 的 `0.83`、评分理由和 EMF 后，CloudWatch 自动把数值提取为 `Builtin.Helpfulness`，放入默认 namespace（分类目录）`Bedrock-AgentCore/Evaluations`，并计算多次评分的 `Average` 和趋势。分析人员用 Logs 查逐条理由和错误，用 Metrics 看均值与趋势。

客户可在[客户说明](docs/customer-guide.md#online-结果与汇总由谁提供)中查看指标字段、时间口径和总分边界，并从 [CloudWatch Dashboard](docs/dashboard.md) 查看创建命令与统计口径。项目已包含 [Dashboard 创建脚本](scripts/dashboard.py)、[JSON 定义示例](examples/dashboard/dashboard.example.json)和[匿名指标预览](examples/dashboard/README.md)。

## 指标可视化预览

CloudWatch 根据本次真实评分生成以下匿名静态预览；AWS Dashboard 同时提供数字卡片、趋势和日志查询。内置指标和 custom 量表分别显示。

![内置指标均值：0.83、1.0、1.0](examples/dashboard/builtin-averages.png)

![自定义指标均值：5.0](examples/dashboard/custom-average.png)

## 这个示例回答什么问题

| 问题 | 已验证的结论 |
|---|---|
| 第三方 Java Agent 是否可以接入？ | 可以，不要求迁移到 AgentCore Runtime。ADOT Java Agent 可以直接向 X-Ray OTLP endpoint 发送 spans。 |
| 是否需要 ADOT Collector？ | 本方案不使用 Collector。AgentCore 官方文档明确不支持以 ADOT Collector 作为外部 Agent Observability 接入方式。 |
| 普通日志就能用于 Eval 吗？ | Java 应用必须提供可识别的 scope、Agent／chat／tool spans、`session.id` 及完整业务内容。普通 stdout 不是完整评估输入。 |
| Online Eval 是否有汇总？ | CloudWatch 提供每个 evaluator 的 Average、SampleCount、Sum 等统计。服务不会自动生成多个 evaluator 的业务总分。 |
| 自定义评分必须是 0–1 吗？ | Custom LLM judge 不限于 0–1。本示例采用 1／3／5 量表，服务实际返回 5.0。 |

**Java 接入必查项：**运维团队应确保最终 `resource.attributes` 包含 `aws.service.type=gen_ai_agent`。本次实验从 AgentCore 的实际 Online 发现查询中确认该过滤条件；缺少此属性时，按需评分可以成功，Online 却不会选中这些会话。它不能只作为普通 span attribute 写入。

## 目录

```text
agent/                  Java Agent、工具、业务埋点与单元测试
scripts/                AWS 配置、Java 启动、证据采集与显式清理脚本
docs/customer-guide.md  Java/Spring 接入与评分语义
docs/dashboard.md       Dashboard 创建、可视化与统计口径
docs/validation.md      实验方法、结果与适用边界
docs/security-review.md 发布前安全检查与依赖修复
examples/evidence/      用于说明格式的匿名评分事件
examples/dashboard/     匿名 Dashboard JSON 与真实指标预览
```

## 快速开始

管理员需要准备 Linux／macOS 环境、JDK 21、Python 3.10+、可用的 AWS 默认凭证链，以及已经启用 Transaction Search 的 Region。Java 程序使用 EC2 instance role、AWS profile 等标准凭证来源，代码不保存 Access Key。

以下命令从仓库根目录执行。`WORK_DIR` 是调用方选择的本地目录，运行状态和原始日志不会被 Git 跟踪。在当前工作区操作时，应将它改成工作区 `tmp/` 下的绝对路径。

```bash
# 开发者把依赖、AWS 状态和构建缓存放在明确的本地目录。
export WORK_DIR="$PWD/.local"
mkdir -p "$WORK_DIR/tmp"
export TMPDIR="$WORK_DIR/tmp"
python3 -m venv "$WORK_DIR/venv"
"$WORK_DIR/venv/bin/pip" install --upgrade "pip>=26.2"
"$WORK_DIR/venv/bin/pip" install -r requirements.txt

# 管理员使用唯一名称创建实验资源；脚本不会更改共享 Transaction Search 设置。
"$WORK_DIR/venv/bin/python" scripts/setup_evaluation.py \
  --region us-east-1 \
  --name customer_java_eval \
  --state "$WORK_DIR/aws-state.json"
```

管理员从脚本输出读取 service、日志组和 Online config ID，再设置 Java 的导出目标。下面的名称与 `customer_java_eval` 对应：

```bash
# 运维团队配置 AgentCore Online 会话发现所需的 resource 属性。
export AWS_REGION=us-east-1
export OTEL_SERVICE_NAME=customer-java-eval
export OTEL_RESOURCE_ATTRIBUTES='service.name=customer-java-eval,aws.service.type=gen_ai_agent,aws.log.group.names=/aws/bedrock-agentcore/runtimes/customer-java-eval'
export OTEL_EXPORTER_OTLP_TRACES_HEADERS='x-aws-log-group=/aws/bedrock-agentcore/runtimes/customer-java-eval,x-aws-log-stream=spans'
export JAVA_BUILD_DIR="$WORK_DIR/java-build"
export ADOT_JAVA_AGENT_JAR="$WORK_DIR/aws-opentelemetry-agent.jar"

# 启动脚本构建 Java 应用，ADOT 提供共享 OTel provider 和 AWS OTLP exporter。
bash scripts/run.sh --download-adot
```

Java 模型会先请求 `order_lookup`，Java 应用读取本地演示订单，再按模型请求执行 `calculator`。订单字段为 `24.50 USD × 3`，计算器使用 `BigDecimal` 返回 `73.5`，模型据此报告状态 `SHIPPED` 和总价。示例不是生产订单系统，也不是通用聊天客户端。

Online 服务异步发现并评价会话。分析人员先检查来源日志中的 spans，随后采集结果：

```bash
# 分析人员读取云端评分和同指标统计；这一步不触发模型或删除资源。
"$WORK_DIR/venv/bin/python" scripts/collect_evidence.py \
  --state "$WORK_DIR/aws-state.json" \
  --output "$WORK_DIR/evidence"
```

采集脚本每次需要一个新输出目录，已有目录会被拒绝覆盖；分析人员复查时应更换 `--output` 或省略它，使用自动生成的目录。

管理员默认保留实验资源，日志不设置自动过期时间，状态文件含 `retain_until_user_requests_deletion=true`。仓库没有自动 cleanup 流程；显式清理脚本也会拒绝删除受到该标记保护的资源。管理员只有在用户明确要求删除之后，才能调整这一保留策略并执行清理。

## 开发者构建与检查

```bash
# 开发者仅构建和运行本地测试，不调用 Bedrock。
JAVA_BUILD_DIR="$WORK_DIR/java-build" bash scripts/run.sh --build-only

# 开发者查看应用实际发送给 Bedrock 的工具定义，不执行模型推理。
JAVA_BUILD_DIR="$WORK_DIR/java-build" bash scripts/run.sh --schema
```

测试覆盖十进制计算、错误参数和工具消息关联。发布前还应运行秘密扫描、检查完整解析的依赖、核对 IAM 资源范围，以及确认 state／凭证／原始遥测不在提交清单。具体结果见 [安全检查记录](docs/security-review.md)。

运行时会输出业务内容，示例只应使用本地演示数据。`CAPTURE_CONTENT=false` 仅控制本应用的内容采集，不是对 ADOT、错误输出或进程参数的全局脱敏保证。客户应按实际数据策略决定内容采集方式。

详细接入步骤见 [客户说明](docs/customer-guide.md)，本次实测记录见 [验证报告](docs/validation.md)。GitHub 中的匿名事件不包含可直接查询真实 AWS 资源的标识；账户内检查信息由实验执行者单独提供。
