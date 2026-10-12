#!/usr/bin/env bash
# 容器入口配置 ADOT 共享 provider；Java RuntimeServer 负责 Runtime HTTP 协议和业务调用。
set -euo pipefail
umask 077
export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:?部署执行者必须设置 OTEL_SERVICE_NAME}"
export LOG_GROUP="${LOG_GROUP:-/aws/bedrock-agentcore/runtimes/$OTEL_SERVICE_NAME}"
export TMPDIR=/app/tmp
mkdir -p "$TMPDIR"

# Bash 仅解析数据，不执行环境变量内容；入口拒绝遥测资源中的冲突或分隔符注入。
declare -A attributes=()
IFS=',' read -r -a pairs <<< "${OTEL_RESOURCE_ATTRIBUTES:-}"
for pair in "${pairs[@]}"; do
  [[ -n "$pair" ]] || continue
  [[ "$pair" == *=* && "$pair" != *$'\n'* && "$pair" != *$'\r'* ]] || { echo '无效 OTEL_RESOURCE_ATTRIBUTES' >&2; exit 2; }
  key="${pair%%=*}"; value="${pair#*=}"
  [[ "$key" =~ ^[a-zA-Z0-9_.-]+$ ]] || { echo '无效遥测属性名' >&2; exit 2; }
  attributes["$key"]="$value"
done
set_attribute() {
  # 容器入口保留调用方自定义属性，并强制校验会话发现和 Runtime 关联需要的属性。
  local key="$1" value="$2"
  [[ "$value" != *','* && "$value" != *$'\n'* && "$value" != *$'\r'* ]] || { echo '无效遥测属性值' >&2; exit 2; }
  [[ -z "${attributes[$key]+present}" || "${attributes[$key]}" == "$value" ]] || { echo "遥测资源属性冲突：$key" >&2; exit 2; }
  attributes["$key"]="$value"
}
set_attribute service.name "$OTEL_SERVICE_NAME"
set_attribute aws.service.type gen_ai_agent
set_attribute aws.log.group.names "$LOG_GROUP"
# AgentCore Runtime 注入自身 ARN；部署执行者也可以显式传入同一 ARN，供 ADOT 关联托管资源。
runtime_arn="${AGENTCORE_RUNTIME_ARN:-${BEDROCK_AGENTCORE_RUNTIME_ARN:-}}"
if [[ -n "$runtime_arn" ]]; then set_attribute aws.resource.arn "$runtime_arn"; fi
export OTEL_RESOURCE_ATTRIBUTES=""
for key in "${!attributes[@]}"; do
  OTEL_RESOURCE_ATTRIBUTES+="${OTEL_RESOURCE_ATTRIBUTES:+,}$key=${attributes[$key]}"
done

export OTEL_TRACES_EXPORTER=otlp OTEL_METRICS_EXPORTER=none OTEL_LOGS_EXPORTER=none
export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="${OTEL_EXPORTER_OTLP_TRACES_ENDPOINT:-https://xray.$AWS_REGION.amazonaws.com/v1/traces}"
# 容器入口固定日志定向头与 LOG_GROUP 一致，避免复用 EC2 环境时把新会话发送到旧日志组。
export OTEL_EXPORTER_OTLP_TRACES_HEADERS="x-aws-log-group=$LOG_GROUP,x-aws-log-stream=spans"
export OTEL_TRACES_SAMPLER=always_on OTEL_AWS_APPLICATION_SIGNALS_ENABLED=false
export AGENT_OBSERVABILITY_ENABLED="${AGENT_OBSERVABILITY_ENABLED:-true}"
export AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT="${AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT:-true}"
export OTEL_BSP_SCHEDULE_DELAY="${OTEL_BSP_SCHEDULE_DELAY:-1000}"
export OTEL_BSP_EXPORT_TIMEOUT="${OTEL_BSP_EXPORT_TIMEOUT:-30000}"
export OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT="${OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT:-131072}"
export CAPTURE_CONTENT="${CAPTURE_CONTENT:-true}"
# Java 进程以 PID 1 接收 SIGTERM；应用与 ADOT 的 shutdown hook 分别停止 HTTP 服务、刷出遥测。
exec java -Djava.io.tmpdir="$TMPDIR" -XX:-UsePerfData \
  -javaagent:/app/aws-opentelemetry-agent.jar -cp /app/app.jar example.agentcore.RuntimeServer
