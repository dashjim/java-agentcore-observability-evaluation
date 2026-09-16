#!/usr/bin/env bash
# 运维入口：Maven 构建 Java 应用；ADOT 复用全局 provider 并通过 SigV4 导出遥测。
# 应用的 instrumentation scope 在 Java 中声明，不能用 resource/span 属性代替。
set -euo pipefail
umask 077
export PYTHONDONTWRITEBYTECODE=1

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
WORK_DIR="${WORK_DIR:-$PROJECT_DIR/.local}"
STATE_PATH="${STATE_PATH:-}"
mode=run
download_adot=false

usage() {
  # 帮助不建目录、不下载依赖、不初始化任何 AWS 客户端。
  cat <<'HELP'
用法：scripts/run.sh [选项] [-- Java 应用任务文本]
  --help                  显示帮助，不构建或访问 AWS
  --build-only            只构建和执行 Java 单元测试，不加载 ADOT 或调用模型
  --schema                构建后输出应用工具 schema，不加载 ADOT 或调用模型
  --state PATH            从 setup state 读取 Region、service 和来源日志组
  --work-dir PATH         工作目录；环境变量 WORK_DIR，默认项目 .local
  --download-adot         缺失时下载固定 ADOT v2.30.0，并检查证据中的 SHA-256

环境变量：JAVA_BUILD_DIR（默认 WORK_DIR/build）、MAVEN_HOME、MAVEN_OFFLINE=true、
ADOT_JAVA_AGENT_JAR、LOG_DIR（默认 WORK_DIR/logs）、STATE_PATH、AWS_REGION、
OTEL_SERVICE_NAME、LOG_GROUP、OTEL_RESOURCE_ATTRIBUTES、OTEL_EXPORTER_OTLP_TRACES_ENDPOINT。
操作者自行提供 ADOT JAR 时仍须匹配 v2.30.0 的固定 SHA。Maven 优先复用现有安装与缓存。
本入口从不调用 setup 或 cleanup；每次运行使用新日志文件并保留资源、state 和结果。
HELP
}

while (($#)); do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --build-only|--schema)
      [[ "$mode" == run ]] || { echo '只能选择一种运行模式' >&2; exit 2; }
      mode="${1#--}"; shift ;;
    --download-adot) download_adot=true; shift ;;
    --state|--work-dir)
      (($# >= 2)) || { echo "$1 缺少路径" >&2; exit 2; }
      if [[ "$1" == --state ]]; then STATE_PATH="$2"; else WORK_DIR="$2"; fi
      shift 2 ;;
    --) shift; break ;;
    --*) echo "未知选项：$1" >&2; exit 2 ;;
    *) break ;;
  esac
done
if [[ "$mode" != run && $# -gt 0 ]]; then echo '本地验证模式不接受任务文本' >&2; exit 2; fi

checked_path() {
  # Python 只做路径解析，应用拒绝使用系统临时目录；不执行用户输入为 shell 代码。
  python3 - "$SCRIPT_DIR" "$1" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from common import local_path
print(local_path(sys.argv[2]))
PY
}

WORK_DIR="$(checked_path "$WORK_DIR")"
JAVA_BUILD_DIR="$(checked_path "${JAVA_BUILD_DIR:-$WORK_DIR/build}")"
LOG_DIR="$(checked_path "${LOG_DIR:-$WORK_DIR/logs}")"
ADOT_JAVA_AGENT_JAR="$(checked_path "${ADOT_JAVA_AGENT_JAR:-$WORK_DIR/aws-opentelemetry-agent.jar}")"
mkdir -p "$JAVA_BUILD_DIR/jvm-tmp" "$JAVA_BUILD_DIR/m2" "$LOG_DIR"
export WORK_DIR JAVA_BUILD_DIR TMPDIR="$JAVA_BUILD_DIR/jvm-tmp"
# Maven、测试 JVM 和应用 JVM 均在调用方选择的目录存放临时文件。
export JAVA_TOOL_OPTIONS="${JAVA_TOOL_OPTIONS:-} -Djava.io.tmpdir=\"$TMPDIR\" -XX:-UsePerfData"
export PYTHONDONTWRITEBYTECODE=1

verify_sha() {
  # 运维脚本验证固定摘要，拒绝仅凭下载成功或文件名信任二进制文件。
  python3 - "$1" "$2" "$3" <<'PY'
import hashlib
from pathlib import Path
import sys
digest = hashlib.new(sys.argv[3])
with Path(sys.argv[1]).open('rb') as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b''):
        digest.update(chunk)
if digest.hexdigest() != sys.argv[2]:
    raise SystemExit('文件校验失败：' + sys.argv[1])
PY
}

build() {
  # Maven 安装只在调用方没有提供 Maven 且工作缓存缺失时下载，固定版本与 SHA-512。
  local version=3.9.11
  local mvn_bin archive
  if [[ -n "${MAVEN_HOME:-}" ]]; then
    mvn_bin="$MAVEN_HOME/bin/mvn"
  elif command -v mvn >/dev/null 2>&1; then
    mvn_bin="$(command -v mvn)"
  else
    mvn_bin="$JAVA_BUILD_DIR/apache-maven-$version/bin/mvn"
    if [[ ! -x "$mvn_bin" ]]; then
      archive="$JAVA_BUILD_DIR/apache-maven-$version-bin.tar.gz"
      if [[ ! -f "$archive" ]]; then
        curl --fail --location --silent --show-error --retry 2 --connect-timeout 15 --max-time 300 \
          "https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/$version/apache-maven-$version-bin.tar.gz" \
          -o "$archive.partial"
        mv -- "$archive.partial" "$archive"
      fi
      verify_sha "$archive" 'bcfe4fe305c962ace56ac7b5fc7a08b87d5abd8b7e89027ab251069faebee516b0ded8961445d6d91ec1985dfe30f8153268843c89aa392733d1a3ec956c9978' sha512
      tar -xzf "$archive" -C "$JAVA_BUILD_DIR"
    fi
  fi
  [[ -x "$mvn_bin" ]] || { echo "Maven 不可执行：$mvn_bin" >&2; return 2; }
  local offline=()
  [[ "${MAVEN_OFFLINE:-false}" != true ]] || offline=(--offline)
  "$mvn_bin" --batch-mode --no-transfer-progress -q "${offline[@]}" -f "$PROJECT_DIR/agent/pom.xml" \
    "-Dmaven.repo.local=$JAVA_BUILD_DIR/m2" "-Dagent.build.directory=$JAVA_BUILD_DIR/target" package >&2
}

if [[ "$mode" == build-only ]]; then build; exit 0; fi
if [[ "$mode" == schema ]]; then
  build
  # 应用 --schema 分支在创建 Bedrock 客户端之前返回；这里不加载 ADOT。
  exec java -jar "$JAVA_BUILD_DIR/target/java-evaluation-agent.jar" --schema
fi

if [[ -n "$STATE_PATH" ]]; then
  # 应用读取 JSON 字段，不 source/eval state；换行字段会被拒绝，避免 shell 注入。
  state_values="$(python3 - "$SCRIPT_DIR" "$STATE_PATH" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from common import read_state
state = read_state(sys.argv[2])
for key in ('region', 'service', 'log_group'):
    value = state[key]
    if not isinstance(value, str) or not value or '\n' in value or '\r' in value:
        raise SystemExit('state 字段无效：' + key)
    print(value)
PY
)"
  # Bash 3 也支持逐行 read，避免依赖仅 Bash 4 提供的 mapfile。
  { read -r AWS_REGION; read -r OTEL_SERVICE_NAME; read -r LOG_GROUP; } <<< "$state_values"
fi
export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:?请通过 --state 或 OTEL_SERVICE_NAME 指定 setup 创建的服务名}"
export LOG_GROUP="${LOG_GROUP:-/aws/bedrock-agentcore/runtimes/$OTEL_SERVICE_NAME}"

# 应用合并调用方的其他资源属性，同时强制保留 Online 会话发现必需的三个字段。
export OTEL_RESOURCE_ATTRIBUTES
OTEL_RESOURCE_ATTRIBUTES="$(python3 - <<'PY'
import os
attrs = {}
for pair in os.environ.get('OTEL_RESOURCE_ATTRIBUTES', '').split(','):
    if pair.strip():
        key, sep, value = pair.partition('=')
        if not sep:
            raise SystemExit('OTEL_RESOURCE_ATTRIBUTES 必须为 key=value 列表')
        attrs[key.strip()] = value.strip()
for key, value in {'service.name': os.environ['OTEL_SERVICE_NAME'], 'aws.service.type': 'gen_ai_agent',
                   'aws.log.group.names': os.environ['LOG_GROUP']}.items():
    if key in attrs and attrs[key] != value:
        raise SystemExit('资源属性冲突，请与 setup 输出保持一致：' + key)
    attrs[key] = value
print(','.join(key + '=' + value for key, value in attrs.items()))
PY
)"
export OTEL_TRACES_EXPORTER=otlp
export OTEL_METRICS_EXPORTER=none OTEL_LOGS_EXPORTER=none
export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="${OTEL_EXPORTER_OTLP_TRACES_ENDPOINT:-https://xray.$AWS_REGION.amazonaws.com/v1/traces}"
export OTEL_EXPORTER_OTLP_TRACES_HEADERS="${OTEL_EXPORTER_OTLP_TRACES_HEADERS:-x-aws-log-group=$LOG_GROUP,x-aws-log-stream=spans}"
# ADOT 定向头必须与 resource 中的来源日志组一致，避免复用旧环境时写入另一组。
python3 - <<'PY'
import os
from urllib.parse import unquote
headers = dict(pair.strip().split('=', 1) for pair in os.environ['OTEL_EXPORTER_OTLP_TRACES_HEADERS'].split(','))
if unquote(headers.get('x-aws-log-group', '')) != os.environ['LOG_GROUP']:
    raise SystemExit('ADOT x-aws-log-group 与 setup/LOG_GROUP 不一致')
if not headers.get('x-aws-log-stream'):
    raise SystemExit('ADOT 定向头必须包含 x-aws-log-stream')
PY
export OTEL_TRACES_SAMPLER=always_on OTEL_AWS_APPLICATION_SIGNALS_ENABLED=false
# 这两个跨语言提示与 Java 2.30.0 一同实测；没有独立证明 Java 会读取它们。
export AGENT_OBSERVABILITY_ENABLED="${AGENT_OBSERVABILITY_ENABLED:-true}"
export AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT="${AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT:-true}"
export OTEL_BSP_SCHEDULE_DELAY="${OTEL_BSP_SCHEDULE_DELAY:-1000}"
export OTEL_BSP_EXPORT_TIMEOUT="${OTEL_BSP_EXPORT_TIMEOUT:-30000}"
export OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT="${OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT:-131072}"
export CAPTURE_CONTENT="${CAPTURE_CONTENT:-true}"

# 固定摘要来自旧实测 evidence/runtime-versions.json；禁止使用 latest 或接受任意摘要。
if [[ ! -f "$ADOT_JAVA_AGENT_JAR" ]]; then
  if [[ "$download_adot" != true ]]; then
    echo "ADOT JAR 缺失：$ADOT_JAVA_AGENT_JAR；请自行下载 v2.30.0 或显式添加 --download-adot" >&2
    exit 2
  fi
  mkdir -p "$(dirname -- "$ADOT_JAVA_AGENT_JAR")"
  curl --fail --location --silent --show-error --retry 2 --connect-timeout 15 --max-time 300 \
    'https://github.com/aws-observability/aws-otel-java-instrumentation/releases/download/v2.30.0/aws-opentelemetry-agent.jar' \
    -o "$ADOT_JAVA_AGENT_JAR.partial"
  verify_sha "$ADOT_JAVA_AGENT_JAR.partial" '3eaf21615c658e567f40428fd582aa67ba112b416f9f856407a22264f27cd5db' sha256
  mv -- "$ADOT_JAVA_AGENT_JAR.partial" "$ADOT_JAVA_AGENT_JAR"
fi
verify_sha "$ADOT_JAVA_AGENT_JAR" '3eaf21615c658e567f40428fd582aa67ba112b416f9f856407a22264f27cd5db' sha256
build
# 应用日志含演示对话；操作者应保留在工作目录而非提交 Git。每次运行使用唯一文件。
run_id="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
echo "Java 运行日志：$LOG_DIR/java-$run_id.stdout.jsonl 和 $LOG_DIR/java-$run_id.stderr.log" >&2
java -javaagent:"$ADOT_JAVA_AGENT_JAR" -jar "$JAVA_BUILD_DIR/target/java-evaluation-agent.jar" "$@" \
  > >(tee "$LOG_DIR/java-$run_id.stdout.jsonl") 2> >(tee "$LOG_DIR/java-$run_id.stderr.log" >&2)
