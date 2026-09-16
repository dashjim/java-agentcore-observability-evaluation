#!/usr/bin/env python3
"""应用只读采集本次运行的 spans、Online 评分和 CloudWatch 指标，保留原始记录。

AgentCore Evaluations 产生评分；本脚本只整理证据，不把未出现的结果解释为评分成功。
每次采集使用新目录，应用拒绝覆盖已有证据；访问错误写入 manifest 并以非零退出。
"""

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
import uuid

from common import (aws_session, entrypoint, local_path, now, parser, read_state,
                    state_path, write_json)


def utc_time(value):
    """应用接受带时区的 ISO 8601 时间，避免把本地时间误当 UTC。"""
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('时间必须包含 UTC Z 或明确时区偏移')
    return result.astimezone(timezone.utc)


def read_events(logs, group, start, end):
    """应用分页读取指定日志组和时间范围；AWS 仍保留原始数据。"""
    rows = []
    for page in logs.get_paginator('filter_log_events').paginate(
            logGroupName=group, startTime=int(start.timestamp() * 1000), endTime=int(end.timestamp() * 1000)):
        rows.extend(page.get('events', []))
    return rows


def summarize(source, results):
    """应用分别整理 span、评分和评估错误；普通文本或其他日志另行计数。"""
    sessions, scores, errors = {}, [], []
    skipped = Counter()

    def object_field(item, key):
        """应用容忍遥测可选对象缺失或为 null，原始异常字段仍保存在 events 文件。"""
        value = item.get(key)
        return value if isinstance(value, dict) else {}

    for label, records in (('source', source), ('results', results)):
        for event in records:
            try:
                item = json.loads(event.get('message', ''))
            except (ValueError, TypeError):
                skipped[label] += 1
                continue
            if not isinstance(item, dict) or not isinstance(item.get('attributes'), dict):
                skipped[label] += 1
                continue
            attrs = item['attributes']
            if label == 'results':
                # AgentCore 的评分失败事件携带 error/error.type/error.message；应用不把它算作分数或未解析日志。
                if attrs.get('error') == 1 or attrs.get('error.type') or attrs.get('error.message'):
                    errors.append({'evaluator': attrs.get('gen_ai.evaluation.name'),
                        'sessionId': attrs.get('session.id'), 'traceId': item.get('traceId'), 'spanId': item.get('spanId'),
                        'level': attrs.get('aws.bedrock_agentcore.evaluation_level'),
                        'logTimestamp': event.get('timestamp'), 'errorType': attrs.get('error.type'),
                        'errorMessage': attrs.get('error.message'), 'attributes': attrs, 'rawEvent': event})
                    continue
                if 'gen_ai.evaluation.score.value' not in attrs:
                    skipped[label] += 1
                    continue
                scores.append({'evaluator': attrs.get('gen_ai.evaluation.name'),
                    'value': attrs['gen_ai.evaluation.score.value'], 'label': attrs.get('gen_ai.evaluation.score.label'),
                    'sessionId': attrs.get('session.id'), 'traceId': item.get('traceId'), 'spanId': item.get('spanId'),
                    'level': attrs.get('aws.bedrock_agentcore.evaluation_level'),
                    'logTimestamp': event.get('timestamp'), 'metricTimestamp': object_field(item, '_aws').get('Timestamp'),
                    'attributes': attrs})
            elif attrs.get('session.id'):
                row = sessions.setdefault(str(attrs['session.id']), {'spans': 0, 'operations': Counter(),
                    'traceIds': set(), 'serviceTypes': set(), 'scopeNames': set()})
                row['spans'] += 1
                row['operations'][str(attrs.get('gen_ai.operation.name', 'other'))] += 1
                row['traceIds'].add(str(item.get('traceId', '<missing>')))
                row['serviceTypes'].add(str(object_field(object_field(item, 'resource'), 'attributes').get('aws.service.type', '<missing>')))
                row['scopeNames'].add(str(object_field(item, 'scope').get('name', '<missing>')))
    for row in sessions.values():
        row['operations'] = dict(row['operations'])
        for key in ('traceIds', 'serviceTypes', 'scopeNames'):
            row[key] = sorted(row[key])
    return sessions, scores, errors, dict(skipped)


def main():
    """操作者选择时间范围；应用先核对账号，再采集，并记录部分失败。"""
    p = parser(__doc__)
    p.add_argument('--output', default=os.environ.get('EVIDENCE_DIR'), help='新输出目录；环境变量 EVIDENCE_DIR，默认 WORK_DIR/evidence/唯一编号')
    p.add_argument('--since', help='开始时间，ISO 8601，含时区')
    p.add_argument('--until', help='结束时间，ISO 8601，含时区；默认当前时间')
    p.add_argument('--hours', type=float, default=2, help='未指定 --since 时的回溯小时数；默认 2')
    args = p.parse_args()
    if args.hours <= 0:
        p.error('--hours 必须为正数')
    end = utc_time(args.until) if args.until else datetime.now(timezone.utc)
    start = utc_time(args.since) if args.since else end - timedelta(hours=args.hours)
    if start >= end:
        p.error('开始时间必须早于结束时间')
    state = read_state(state_path(args))
    session, _ = aws_session(state['region'], args.profile, state['account'])
    out = local_path(args.output) if args.output else local_path(args.work_dir) / 'evidence' / uuid.uuid4().hex
    out.mkdir(parents=True, exist_ok=False)
    os.chmod(out, 0o700)
    manifest = {'collectedAt': now(), 'start': start, 'end': end, 'status': 'collecting', 'errors': [], 'files': []}

    def save(name, data):
        """应用只创建新证据文件，并同步更新采集清单。"""
        write_json(out / name, data, exclusive=True)
        manifest['files'].append(name)
        write_json(out / 'manifest.json', manifest)

    def capture(name, action, fallback):
        """应用保存单项访问失败，继续采集独立证据；最终仍返回失败状态。"""
        try:
            value = action()
            save(name, value)
            return value
        except Exception as exc:
            manifest['errors'].append({'file': name, 'type': type(exc).__name__, 'message': str(exc)})
            write_json(out / 'manifest.json', manifest)
            return fallback

    write_json(out / 'manifest.json', manifest, exclusive=True)
    logs, cp, cw = (session.client(name) for name in ('logs', 'bedrock-agentcore-control', 'cloudwatch'))
    config = capture('online-config.json', lambda: cp.get_online_evaluation_config(
        onlineEvaluationConfigId=state['online_config_id']), {})
    source = capture('source-events.json', lambda: read_events(logs, state['log_group'], start, end), [])
    result_group = state.get('result_log_group') or config.get('outputConfig', {}).get('cloudWatchConfig', {}).get('logGroupName')
    results = capture('online-events.json', lambda: read_events(logs, result_group, start, end), [])
    sessions, scores, errors, skipped = summarize(source, results)
    save('online-score-rows.json', scores)
    save('online-error-rows.json', errors)
    metrics = []
    for evaluator in config.get('evaluators', []):
        evaluator_id = evaluator['evaluatorId']
        # AWS 的 evaluatorName 是 metric 名称；不要假设它等于 evaluatorId。
        def metric():
            """CloudWatch 返回指定 service/config 的统计值，应用不混合其他配置的得分。"""
            name = cp.get_evaluator(evaluatorId=evaluator_id)['evaluatorName']
            dimensions = [{'Name': 'service.name', 'Value': state['service']},
                {'Name': 'onlineEvaluationConfigId', 'Value': state['online_config_id']}]
            result = cw.get_metric_statistics(Namespace='Bedrock-AgentCore/Evaluations', MetricName=name,
                Dimensions=dimensions, StartTime=start, EndTime=end, Period=3600,
                Statistics=['Average', 'SampleCount', 'Sum', 'Minimum', 'Maximum'])
            return {'name': name, 'dimensions': dimensions, 'start': start, 'end': end, 'period': 3600,
                    'datapoints': sorted(result.get('Datapoints', []), key=lambda row: row['Timestamp'])}
        value = capture(f'metric-{len(metrics)}.json', metric, {})
        metrics.append(value)
    save('cloudwatch-metrics.json', metrics)
    summary = {'collectedAt': manifest['collectedAt'], 'source': sessions, 'onlineResultEvents': len(results),
        'onlineScoreRows': [{k: v for k, v in row.items() if k != 'attributes'} for row in scores],
        'errorRows': len(errors), 'evaluationErrors': errors,
        'metrics': metrics, 'unparsedOrNonScoreEvents': skipped,
        'scoresObserved': bool(scores), 'collectionErrors': manifest['errors']}
    save('summary.json', summary)
    manifest['status'] = 'partial' if manifest['errors'] else 'complete'
    write_json(out / 'manifest.json', manifest)
    print(json.dumps({'output': str(out), 'status': manifest['status'], 'scoreRows': len(scores),
                      'errorRows': len(errors)}, ensure_ascii=False, indent=2))
    if manifest['errors']:
        raise SystemExit('部分证据读取失败；请查看输出目录 manifest.json，原证据和 AWS 资源均保留')


if __name__ == '__main__':
    entrypoint(main)
