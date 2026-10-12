# 操作者部署 Java AgentCore Runtime

更新日期：2026-10-12。本文补充原 EC2 实验；部署执行者保留旧资源与历史结果。操作者可使用本仓库的当前源码或交付源码包 `code.zip`；本工作区源码位于 `/home/ubuntu/g-repo/codex-doc/java-agentcore-observability-evaluation`。操作者应确认检出的版本包含 `runtime/` 入口。

## 用户任务与组件职责

用户要求部署执行者在现有账户的 `us-east-1` 增加 AgentCore Runtime 实例。部署执行者使用自定义 Linux ARM64、Java 21 容器、PUBLIC 网络与 IAM SigV4 入站认证。调用者通过 CLI/SDK 发送任务；本示例不提供 WebUI。

| 组件或角色 | 该主体负责的动作 |
|---|---|
| 调用者与 `runtime/invoke.py` | 调用者选择任务和 Session ID；调用脚本读取 state，核对账户与资源归属，AWS SDK 对请求执行 SigV4 签名，并保存响应证据。 |
| AgentCore Runtime | Runtime 托管 ARM64 容器、执行入站认证，并将请求与 Session ID 请求头转发给 Java HTTP 服务。 |
| Java RuntimeServer / MinimalAgent | Java 服务校验 HTTP、JSON 与会话；Java 应用编排 Bedrock 请求、执行本地订单工具和计算器，并记录业务 span。 |
| Bedrock Nova Lite | 模型选择工具及参数，读取 Java 应用返回的工具结果，生成最终订单回答。 |
| ADOT Java 2.31.0 | ADOT 提供共享 OTel provider，使用 AWS 凭证链签名并导出 Java 业务遥测。 |
| AgentCore Evaluations | Evaluations 异步发现会话、调用 evaluator，并写入评分及评估错误。 |
| CloudWatch | CloudWatch 存储 spans、运行日志与评分日志，提取 EMF 指标，并向查看者提供 Dashboard。 |
| 部署执行者 / 验证人员 | 部署执行者创建独立镜像、角色和配置；验证人员核对 READY、真实响应、遥测和数值评分。 |

## 部署执行者准备独立工作目录

部署执行者需要 Linux、JDK 21、Maven、Python 3.10+、Docker Buildx 和有效 AWS 默认凭证链。Docker 主机需能够构建 `linux/arm64`；本次实际构建在 ARM64 主机完成。管理员需要事先启用该 Region 的 Transaction Search；配置脚本不会修改共享设置。部署身份需要创建本次 ECR、IAM、Runtime、Evaluations 和 CloudWatch 资源所需的权限；调用身份需要本次 Runtime 的调用权限，以及脚本核对归属所需的控制面读权限。

以下命令为**创建一套新的独立资源**。操作者若只想调用已部署实例，应直接阅读[演示流程](demo-flow-runtime.md)，无需重新部署。

```bash
# 操作者使用当前交付源码；从 code.zip 解包时，将 PROJECT_DIR 改为实际绝对路径。
set -euo pipefail
export PROJECT_DIR=/home/ubuntu/g-repo/codex-doc/java-agentcore-observability-evaluation
cd "$PROJECT_DIR"
umask 077
export RUN_KEY="$(date -u +%Y%m%d_%H%M%S)_$(python3 -c 'import uuid; print(uuid.uuid4().hex[:8])')"
export WORK_DIR="/home/ubuntu/g-repo/tmp/java-agentcore-runtime-$RUN_KEY"
export DELIVERY_DIR="/home/ubuntu/g-repo/codex-doc/java-agentcore-runtime-$RUN_KEY"
export TMPDIR="$WORK_DIR/tmp"
export DOCKER_CONFIG="$WORK_DIR/docker-config"
export JAVA_BUILD_DIR="$WORK_DIR/java-build"
export IMAGE_TAG="java-agentcore-runtime:$RUN_KEY"
mkdir -p "$TMPDIR" "$DOCKER_CONFIG"
mkdir "$DELIVERY_DIR"
python3 -m venv "$WORK_DIR/venv"
"$WORK_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"
export PYTHON="$WORK_DIR/venv/bin/python"
export PYTHONDONTWRITEBYTECODE=1
export AWS_REGION=us-east-1
```

操作者在同一 shell 中执行下列步骤。UUID 后缀使部署名称、镜像 tag 和 state 路径独立；操作者应保存本次变量值以便后续复查。所有构建缓存位于工作区 `tmp/`，所有用户交付证据位于工作区 `codex-doc/`。部署执行者不应把 state、凭证、账号标识和原始遥测提交到源码仓库。

## 构建执行者校验 ADOT 并构建镜像

```bash
# 构建执行者下载固定官方版本，并在使用二进制文件前验证固定摘要。
export ADOT_JAVA_AGENT_JAR="$WORK_DIR/aws-opentelemetry-agent-2.31.0.jar"
curl --fail --location --retry 2 \
  https://github.com/aws-observability/aws-otel-java-instrumentation/releases/download/v2.31.0/aws-opentelemetry-agent.jar \
  --output "$ADOT_JAVA_AGENT_JAR.partial"
printf '%s  %s\n' \
  4e0f171c6d935f0b9ae5f66b28b0b2dae5e2ac1261fc3e3ffc396ef1e500f4fc \
  "$ADOT_JAVA_AGENT_JAR.partial" | sha256sum --check -
mv "$ADOT_JAVA_AGENT_JAR.partial" "$ADOT_JAVA_AGENT_JAR"

# build.sh 调用现有 scripts/run.sh --build-only，执行 Maven 测试并构建 ARM64 镜像。
bash "$PROJECT_DIR/runtime/build.sh"
docker image inspect "$IMAGE_TAG" --format '{{.Os}}/{{.Architecture}} {{.Id}}'
```

构建脚本再次检查 ADOT 摘要，仅将应用 JAR、ADOT JAR、Dockerfile、入口脚本放入新的 Docker context。Docker 构建器使用固定 Java 21 基底 digest，以 `--platform linux/arm64 --provenance=false` 输出单 ARM64 manifest；部署脚本在任何云端写入前再次检查镜像架构。容器以 UID 10001 运行，Java 与 ADOT 使用 `/app/tmp` 存放临时文件。

应用使用 Jackson 2.18.11、OTel API 1.65.0，ADOT 使用官方 2.31.0。**审查人员仍在 ADOT 内嵌依赖中发现 4 项 HIGH 公告，不能把本次结果表述为依赖扫描全部通过。**审查人员未发现当前 HTTP 输入到相关触发路径的静态证据，但没有完成全面不可达证明。详细范围与限制见本地交付报告：[Runtime 安全审查](security-review-runtime.md)。公开报告记录匿名结论，原始证据由部署执行者在本地保留。

## 部署执行者创建评估配置与 Runtime

```bash
# 配置脚本为本次 Runtime 创建独立日志组、评估角色、custom evaluator 与 Online 配置。
"$PYTHON" "$PROJECT_DIR/scripts/setup_evaluation.py" \
  --region "$AWS_REGION" --name "java_eval_$RUN_KEY" \
  --work-dir "$WORK_DIR" --state "$DELIVERY_DIR/evaluation-aws-state.json"

# 部署脚本创建独立 ECR 仓库与执行角色，推送不可变镜像，再创建并等待 Runtime/DEFAULT READY。
"$PYTHON" "$PROJECT_DIR/runtime/deploy.py" \
  --region "$AWS_REGION" --name "java_runtime_$RUN_KEY" \
  --image "$IMAGE_TAG" --work-dir "$WORK_DIR" \
  --evaluation-state "$DELIVERY_DIR/evaluation-aws-state.json" \
  --state "$DELIVERY_DIR/runtime-aws-state.json"
```

部署脚本先创建 Runtime，再把服务返回的 DEFAULT Endpoint ARN 写入遥测资源属性 `cloud.resource_id`，因此正常首次部署可能产生版本 2。部署脚本检查 DEFAULT 指向的版本与 Runtime 版本一致；验证人员以 state 和 AWS 实际响应为准，不把版本号固定为 2。

部署脚本不接管同名陌生资源，也不覆盖已有 state。若同一部署中断，操作者须保持原名称、镜像、评估 state 和 Runtime state 不变，并在原 `runtime/deploy.py` 命令中显式增加 `--resume`。部署脚本核对账号、Owner 标签、镜像 ID、资源配置和保留标记后恢复操作。操作者不能用 `--resume` 隐式替换镜像。

## 调用者执行新会话，验证人员读取结果

```bash
# 调用脚本默认生成 UUID Session ID；调用者为每次响应选择新文件，脚本拒绝覆盖旧证据。
export INVOCATION_KEY="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
"$PYTHON" "$PROJECT_DIR/runtime/invoke.py" \
  --state "$DELIVERY_DIR/runtime-aws-state.json" \
  --output "$DELIVERY_DIR/invoke-$INVOCATION_KEY.json"

# 验证人员从本地 state 读取实际资源信息，不把账号或 ARN 硬编码到共享源码。
"$PYTHON" - "$DELIVERY_DIR/runtime-aws-state.json" <<'PY'
import json, sys
state = json.load(open(sys.argv[1]))
print(json.dumps({key: state[key] for key in
    ('region', 'runtime_arn', 'runtime_version', 'endpoint_arn', 'endpoint_status', 'image_digest')}, indent=2))
PY
```

调用者也可传入 `--prompt '查询订单 ORD-1001，计算商品总价并回答状态。'` 和 `--session-id <33至256位字母数字下划线连字符>`。首字符必须为字母或数字。SDK 把 Session ID 交给 Runtime；Runtime 通过 `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` 请求头传给 Java 服务；Java 应用把同一标识写入响应和 `session.id` / `gen_ai.conversation.id`。

Java 服务每次请求创建新的 `MinimalAgent`，Java 应用仅在本次请求内保存模型消息历史、工具调用及计数。调用者重复使用同一 Session ID **不会让 Java 应用保留上一次请求的对话历史**；Session ID 负责 Runtime 会话与遥测关联。Java 服务提供 `GET /ping` 和 `POST /invocations`，其中健康请求在业务调用阻塞期间仍可响应。

验证人员应看到模型回答 `ORD-1001`、`SHIPPED`、`73.5 USD`，以及 `successful_tools` 中的 `order_lookup` 与 `calculator`。调用脚本保存原始响应、Session ID、Trace ID 和耗时，并校验响应会话与请求会话相等；验证人员仍须核对工具与回答内容。

## 分析人员采集遥测与评分，创建 Dashboard

```bash
# collect_evidence.py 仅读取日志、评分和指标；分析人员每次选择新的输出目录。
export EVIDENCE_KEY="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
"$PYTHON" "$PROJECT_DIR/scripts/collect_evidence.py" \
  --state "$DELIVERY_DIR/evaluation-aws-state.json" --work-dir "$WORK_DIR" \
  --hours 2 --output "$DELIVERY_DIR/evidence-$EVIDENCE_KEY"

# 管理员显式创建本次独立 Dashboard；CloudWatch 提供可视化，Java 服务不提供 WebUI。
"$PYTHON" "$PROJECT_DIR/scripts/dashboard.py" \
  --state "$DELIVERY_DIR/evaluation-aws-state.json" --work-dir "$WORK_DIR" \
  --name "java-runtime-$RUN_KEY" --output "$DELIVERY_DIR/dashboard-$EVIDENCE_KEY.json" \
  --apply
```

AgentCore Evaluations 异步处理会话。分析人员应分别核对 `summary.json` 的 source 会话、`onlineScoreRows`、`evaluationErrors` 和 CloudWatch metrics；没有评分时，分析人员应保留本次证据，稍后用新目录重新采集，不能把“0 条错误”当作评分成功。Dashboard 脚本在 receipt 中记录控制台链接；CloudWatch 按 evaluator 分别汇总数值，不自动生成跨 evaluator 的业务总分。分析人员查看历史会话时，应同时为采集脚本指定 `--since` / `--until`，为 Dashboard 指定对应 `--start` / `--end`，并使用含时区的 ISO 8601 时间。

## 验证人员记录当前结果，操作者保留资源

截至本文更新，验证人员已确认 `us-east-1` Runtime 与 DEFAULT 为 READY；两个真实会话均成功执行两个工具并返回 `SHIPPED` 和 `73.5 USD`，Java 应用各调用模型 3 次，ADOT 向 CloudWatch 导出的每会话 6 个业务 span 与请求会话对应。验证人员已执行 14 项 Java 测试和 9 项 Runtime 部署脚本测试且全部通过。Supervisor 已核验 AgentCore Online Evaluations 对本次两个会话写出 9 条数值评分、1 条评估错误，详见 [Runtime 验证报告](e2e-test-report-runtime.md)。

部署与调用脚本都保留 `retain_until_user_requests_deletion=true`。部署执行者保留原 EC2 资源和本次 Runtime、ECR、角色、评估配置、Dashboard、日志及本地证据，不运行自动 cleanup。只有用户随后明确要求删除，管理员才可调整保留策略并执行与本次资源对应的删除流程；既有 EC2 清理脚本不能代替 Runtime 资源清理。

## 部署执行者更新已有 Runtime 镜像

部署执行者需要保留旧状态及镜像，重新构建新 tag，然后通过独立输出状态文件更新原 Runtime。更新脚本核对账号、Owner 标签、旧版本、镜像、IAM 角色和全部运行配置，仅替换镜像并同步 DEFAULT Endpoint；脚本拒绝覆盖已有新状态文件，也不会自动回滚或删除历史资源。

部署执行者第二次及以后升级时，还必须把 `--state` 改为上一成功版本的状态文件，不能只修改 `--output-state` 编号；脚本会拒绝与云端当前版本不一致的旧状态。

```bash
# 操作者先为当前修改后的源码构建一个新的本地镜像 tag。
export IMAGE_TAG="java-agentcore-runtime:revision-$(date -u +%Y%m%dT%H%M%SZ)"
bash "$PROJECT_DIR/runtime/build.sh"

# 操作者选择上一版本 state 与全新的下一版本 state；已创建 r02 时应另选新编号。
"$PYTHON" "$PROJECT_DIR/runtime/update.py" \
  --state "$DELIVERY_DIR/runtime-aws-state.json" \
  --output-state "$DELIVERY_DIR/runtime-aws-state-r02.json" \
  --image "$IMAGE_TAG" --work-dir "$WORK_DIR"

# 调用者使用新状态和全新 Session；旧 Session 可能继续使用原容器版本。
"$PYTHON" "$PROJECT_DIR/runtime/invoke.py" \
  --state "$DELIVERY_DIR/runtime-aws-state-r02.json" \
  --output "$DELIVERY_DIR/invoke-new-revision-$(date -u +%Y%m%dT%H%M%SZ).json"
```
