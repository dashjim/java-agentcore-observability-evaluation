# 演示者调用已部署的 Java Runtime

日期：2026-10-12。本流程使用本次已部署的 `us-east-1` 实例；演示者无需重新创建资源。演示者可使用本仓库当前源码或交付的 `code.zip`。CLI/SDK 是业务入口，CloudWatch Dashboard 是 AWS 控制台中的结果视图；Java 服务不提供 WebUI。

## 演示者准备本地路径

```bash
# 演示者在同一 shell 中指定已有交付与工作目录，不将 state 内容写入共享源码。
set -euo pipefail
export PROJECT_DIR=/home/ubuntu/g-repo/codex-doc/java-agentcore-observability-evaluation
export DELIVERY_DIR=/home/ubuntu/g-repo/codex-doc/java-agentcore-runtime-20261012
export WORK_DIR=/home/ubuntu/g-repo/tmp/java-agentcore-runtime-20261012
export PYTHON="$WORK_DIR/venv/bin/python"
export PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$WORK_DIR/tmp"
mkdir -p "$TMPDIR"

# 演示者如尚无 Python 环境，则先安装当前交付固定依赖。
if [[ ! -x "$PYTHON" ]]; then
  python3 -m venv "$WORK_DIR/venv"
  "$WORK_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"
fi
```

调用者的 AWS 默认凭证链必须指向 state 记录的账户，并具备本次 Runtime 调用和归属检查权限。调用脚本会核对账户和 Owner 标签，凭证不写入命令参数或源码。

## 演示者展示实际部署与原始会话

```bash
# 演示脚本从本地证据读取资源和历史响应；此步骤不调用模型，也不修改云端资源。
"$PYTHON" - "$DELIVERY_DIR" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
state = json.loads((root / 'runtime-aws-state-r02.json').read_text())
print(json.dumps({key: state[key] for key in
    ('region', 'runtime_arn', 'runtime_version', 'endpoint_status', 'endpoint_arn')}, indent=2))
for name in ('invoke-r02-001.json', 'invoke-r02-002.json'):
    row = json.loads((root / name).read_text())
    answer = row['response']
    print(json.dumps({'evidence': name, 'completed_at': row['completed_at'],
        'session_id': row['session_id'], 'trace_id': row['trace_id'],
        'answer': answer['answer'], 'successful_tools': answer['successful_tools'],
        'model_calls': answer['model_calls']}, ensure_ascii=False, indent=2))
PY
```

验证人员保存的两个会话分别于 `2026-10-12 06:06:50 UTC` 和 `06:06:54 UTC` 完成；Java 应用每次调用 Bedrock Nova Lite 3 次，执行 `order_lookup` 与 `calculator`。Bedrock 模型都回答：订单 `ORD-1001` 为 `SHIPPED`，商品总价为 `73.5 USD`，数据来自本地演示 fixture。ADOT 导出的每个会话包含 1 个 invoke_agent、3 个 chat、2 个 execute_tool 业务 span。演示者应从上述私有证据读取实际 Session ID / Trace ID，不在共享文档中复制账号或完整 ARN。

## 调用者创建并保存一次新调用

```bash
# 调用者生成新文件名；invoke.py 自动生成 UUID Session ID，拒绝覆盖历史输出。
export INVOCATION_KEY="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
"$PYTHON" "$PROJECT_DIR/runtime/invoke.py" \
  --state "$DELIVERY_DIR/runtime-aws-state-r02.json" \
  --output "$DELIVERY_DIR/invoke-$INVOCATION_KEY.json" \
  --prompt '查询订单 ORD-1001，用 calculator 计算单价乘数量，回答订单状态与商品总价（USD）。'
```

演示者可按以下顺序讲解组件动作：调用脚本用 SigV4 签名；AgentCore Runtime 验证请求并托管容器；Runtime 把 Session ID 放入 `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` 请求头；Java 服务校验 JSON 并创建新的 `MinimalAgent`；Bedrock 模型选择工具；Java 应用执行工具并把真实结果交回模型；模型生成最终回答；ADOT 把 Java 业务 span 导出；CloudWatch 保存遥测；AgentCore Evaluations 随后读取会话并调用 evaluator。

Java 服务每次请求独立创建 Agent，Java 应用仅保留本次请求内的模型历史。调用者即使显式复用 `--session-id`，Java 应用也不会自动接续上次对话。响应中的 Session ID 应与请求一致，Trace ID 用于分析人员定位本次调用。

## 分析人员对照评分与 Dashboard

```bash
# 分析人员采集历史两次调用和本轮新增调用；每次使用新目录保存结果。
export EVIDENCE_KEY="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
"$PYTHON" "$PROJECT_DIR/scripts/collect_evidence.py" \
  --state "$DELIVERY_DIR/evaluation-aws-state.json" --work-dir "$WORK_DIR" \
  --since 2026-10-12T05:50:00Z --output "$DELIVERY_DIR/evidence-demo-$EVIDENCE_KEY"
```

分析人员打开已创建的 [CloudWatch Runtime Dashboard](https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#dashboards:name=java-agentcore-runtime-evaluation-20261012)。原演示时间窗口为 `2026-10-12 05:50–06:20 UTC`（北京时间 `13:50–14:20`）；分析人员查看较晚的新调用时，应在控制台调整时间范围。

CloudWatch 展示日志和同一 evaluator 的指标汇总；AgentCore Evaluations 负责产生数值评分及理由。分析人员先用 Session ID / Trace ID 对照业务 span，再核对 `onlineScoreRows` 和 `evaluationErrors`。Online 评分尚未出现时，分析人员保留当前采集结果并稍后复查，不能把响应成功等同于评分成功。本文编写时，Supervisor 尚在完成本次 Online 评分的最终核验。

分析人员如需创建新的独立 Dashboard，可使用以下命令；脚本通过新的名称与文件路径保留原 Dashboard 和证据。

```bash
"$PYTHON" "$PROJECT_DIR/scripts/dashboard.py" \
  --state "$DELIVERY_DIR/evaluation-aws-state.json" --work-dir "$WORK_DIR" \
  --name "java-runtime-demo-$EVIDENCE_KEY" \
  --output "$DELIVERY_DIR/dashboard-demo-$EVIDENCE_KEY.json" --apply
```

## 演示者说明当前边界并保留现场

验证人员已确认 Runtime / DEFAULT READY、两次真实订单回答成功、工具执行和每会话 6 个业务 span；评分状态以最终采集证据为准。审查人员仍在官方 ADOT 2.31.0 中发现 4 项 HIGH 依赖公告，详细限制见本地报告：[Runtime 安全审查](security-review-runtime.md)。演示者不应把功能成功描述为安全审查全部通过。

演示者保留所有资源和成功／失败证据，不执行 cleanup；管理员只有在用户明确要求删除后，才可调整 `retain_until_user_requests_deletion=true` 对应的保留策略并删除相应资源。管理员如需创建新的独立部署，按[部署指南](deployment-guide-runtime.md)依次执行 `runtime/build.sh`、`scripts/setup_evaluation.py`、`runtime/deploy.py`，然后使用本文的调用与验证流程。
