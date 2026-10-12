#!/usr/bin/env bash
# 构建执行者在工作区缓存目录运行 Maven，并仅把白名单产物发送给 ARM64 Docker 构建器。
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"

if [[ "${1:-}" == --help ]]; then
  cat <<'HELP'
用法：WORK_DIR=/绝对/工作目录 IMAGE_TAG=仓库:版本 ADOT_JAVA_AGENT_JAR=/绝对/adot.jar runtime/build.sh
构建执行者使用 Java 21、Maven、Docker；脚本运行测试并构建 linux/arm64 镜像，不推送或部署。
WORK_DIR 必须位于工作区且不能在系统 /tmp；JAVA_BUILD_DIR 默认 WORK_DIR/java-build。
可选 JAVA_BASE_IMAGE 覆盖 Dockerfile 固定基底，DOCKER_CONFIG 指定 Docker 凭证配置目录。
ADOT 固定为 2.31.0；脚本校验 SHA-256，不下载或接受未知二进制文件。
HELP
  exit 0
fi
[[ $# == 0 ]] || { echo 'build.sh 不接受位置参数，请使用环境变量或 --help' >&2; exit 2; }
: "${WORK_DIR:?请指定工作区内的绝对 WORK_DIR}"
: "${IMAGE_TAG:?请指定本次不可变版本的 IMAGE_TAG}"
: "${ADOT_JAVA_AGENT_JAR:?请指定已下载的 ADOT 2.31.0 JAR}"
# 构建执行者先规范化路径，拒绝系统临时目录及相对路径；现有 run.sh 再次校验工作目录。
for path in "$WORK_DIR" "${JAVA_BUILD_DIR:-$WORK_DIR/java-build}" "$ADOT_JAVA_AGENT_JAR"; do
  [[ "$path" == /* ]] || { echo '构建路径必须为绝对路径' >&2; exit 2; }
  resolved="$(realpath -m -- "$path")"
  [[ "$resolved" != /tmp && "$resolved" != /tmp/* ]] || { echo '禁止使用系统 /tmp' >&2; exit 2; }
done
export JAVA_BUILD_DIR="${JAVA_BUILD_DIR:-$WORK_DIR/java-build}"
expected_sha=4e0f171c6d935f0b9ae5f66b28b0b2dae5e2ac1261fc3e3ffc396ef1e500f4fc
actual_sha="$(sha256sum -- "$ADOT_JAVA_AGENT_JAR")"
[[ "${actual_sha%% *}" == "$expected_sha" ]] || { echo 'ADOT 2.31.0 SHA-256 不匹配' >&2; exit 2; }
"$PROJECT_DIR/scripts/run.sh" --build-only --work-dir "$WORK_DIR"
# 构建执行者为每次构建创建新 context，不复用可能混入 state、凭证或原始日志的目录。
mkdir -p "$WORK_DIR/docker-contexts"
context="$(mktemp -d "$WORK_DIR/docker-contexts/context-XXXXXXXX")"
cp -- "$JAVA_BUILD_DIR/target/java-evaluation-agent.jar" "$context/app.jar"
cp -- "$ADOT_JAVA_AGENT_JAR" "$context/aws-opentelemetry-agent.jar"
cp -- "$SCRIPT_DIR/entrypoint.sh" "$SCRIPT_DIR/Dockerfile" "$context/"
options=()
if [[ -n "${JAVA_BASE_IMAGE:-}" ]]; then options+=(--build-arg "JAVA_BASE_IMAGE=$JAVA_BASE_IMAGE"); fi
# 构建器输出单一 ARM64 manifest，避免附带 unknown 架构的 provenance manifest 混入 Runtime 镜像。
docker build --platform linux/arm64 --provenance=false "${options[@]}" --tag "$IMAGE_TAG" "$context"
echo "ARM64 镜像：$IMAGE_TAG"
echo "可审查构建上下文：$context"
