#!/usr/bin/env python3
"""应用读取 Online 配置和 evaluator 元数据，生成可复用的 CloudWatch Dashboard。

默认只写本地 JSON；操作者传入 --apply 时，应用才创建 Dashboard。
应用保留 state、AWS 资源和失败现场，不覆盖内容不同的同名 Dashboard。
"""

from datetime import datetime, timezone
import json
import math
import re
from urllib.parse import quote as url_quote
import uuid

from botocore.exceptions import ClientError

from common import (aws_session, entrypoint, local_path, now, parser, read_state,
                    state_path, write_json)

# 应用只为本项目实测的三个内置指标采用 0–1 显示范围，不解释其提示词档位。
BUILTIN_IDS = frozenset({'Builtin.Helpfulness', 'Builtin.Correctness',
                         'Builtin.ToolSelectionAccuracy'})


def time_range(start, end):
    """应用将带时区的历史范围保存为 UTC；缺省范围由控制台解释为最近三小时。"""
    def absolute(value):
        """应用拒绝无时区或非日期时间输入，避免历史窗口随本机时区漂移。"""
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if 'T' not in value or parsed.tzinfo is None:
            raise ValueError('--start/--end 必须使用含时区的 ISO 8601 日期时间')
        return parsed.astimezone(timezone.utc)

    result = {'start': '-PT3H', 'periodOverride': 'inherit'}
    if start != '-PT3H':
        begin = absolute(start)
        result['start'] = begin.isoformat().replace('+00:00', 'Z')
    if end is not None:
        if start == '-PT3H':
            raise ValueError('指定 --end 时也必须指定绝对 --start')
        finish = absolute(end)
        if begin >= finish:
            raise ValueError('--start 必须早于 --end')
        result['end'] = finish.isoformat().replace('+00:00', 'Z')
    return result


def display_scale(description):
    """应用对已实测内置指标显示 0–1；仅对自定义 evaluator 读取数值 ratingScale。"""
    identifier = description['evaluatorId']
    if identifier in BUILTIN_IDS:
        return 0, 1
    if identifier.startswith('Builtin.'):
        raise ValueError(f'本项目尚未确认该内置 evaluator 的运行分值范围：{identifier}')
    scale = description['evaluatorConfig']['llmAsAJudge']['ratingScale']
    values = [item['value'] for item in scale.get('numerical', [])]
    if not values or any(isinstance(v, bool) or not isinstance(v, (int, float))
                         or not math.isfinite(v) for v in values):
        raise ValueError(f"evaluator 缺少有效 numerical ratingScale：{description['evaluatorId']}")
    return min(values), max(values)


def result_queries(state, group):
    """应用构造 Logs Insights 查询；CloudWatch 按日志时间筛选并逐 evaluator 统计。"""
    # 应用按 CloudWatch LogGroupName 规则校验，再使用 Dashboard 官方示例的单引号 SOURCE。
    if not isinstance(group, str) or not re.fullmatch(r'[A-Za-z0-9._/#-]{1,512}', group):
        raise ValueError('日志组名必须为 1–512 个 ASCII 字母、数字或 . _ / # -，不能包含引号')
    quote = json.dumps
    config_arn = (f"arn:{state.get('partition', 'aws')}:bedrock-agentcore:{state['region']}:"
                  f"{state['account']}:online-evaluation-config/{state['online_config_id']}")
    # 错误事件可能没有 EMF 根字段；应用同时识别资源属性和配置 ARN，不依赖自动字段发现。
    base = (f"SOURCE '{group}'\n"
            '| fields jsonParse(@message) as r\n'
            '| filter r.name = "gen_ai.evaluation.result"\n'
            f'| filter coalesce(r.`service.name`, r.resource.attributes.`service.name`) = {quote(state["service"])}\n'
            f'| filter (r.onlineEvaluationConfigId = {quote(state["online_config_id"])} or '
            f'r.attributes.`aws.bedrock_agentcore.online_evaluation_config.arn` = {quote(config_arn)})\n')
    # 仅缺失的错误标记按“此事件未报错”计数；应用不填补缺失分数，也不生成不存在的 evaluator 行。
    summary = (base + '| fields r.attributes.`gen_ai.evaluation.name` as evaluator, '
               'r.attributes.`gen_ai.evaluation.score.value` as score, '
               'coalesce(r.attributes.error, 0) as evaluation_error\n'
               '| stats count(*) as result_events, count(score) as score_samples, '
               'avg(score) as average_score, sum(evaluation_error) as evaluation_errors by evaluator')
    recent = (base + '| fields @timestamp, r.attributes.`session.id` as session, r.traceId as trace, '
              'r.attributes.`gen_ai.evaluation.name` as evaluator, '
              'r.attributes.`gen_ai.evaluation.score.value` as value, '
              'r.attributes.`gen_ai.evaluation.explanation` as explanation, '
              'r.attributes.error as evaluation_error, r.attributes.`error.type` as error_type, '
              'r.attributes.`error.message` as error_message\n'
              '| sort @timestamp desc\n| limit 30')
    return summary, recent


def build_dashboard(state, evaluator_descriptions, *, start='-PT3H', end=None,
                    result_log_group=None):
    """应用纯函数组装 24 列看板；调用方传入四个 get_evaluator 原始响应，不发生 I/O。

    应用要求本仓库的三个内置 0–1 evaluator 和一个自定义 1–5 evaluator，
    使用 evaluatorName 作为真实 metric 名称；调用方负责获取并核对远端配置。
    """
    descriptions = list(evaluator_descriptions)
    builtin = [d for d in descriptions if d['evaluatorId'].startswith('Builtin.')]
    custom = [d for d in descriptions if not d['evaluatorId'].startswith('Builtin.')]
    if ({d['evaluatorId'] for d in builtin} != BUILTIN_IDS or len(custom) != 1
            or len({d['evaluatorId'] for d in descriptions}) != 4
            or len(descriptions) != 4
            or len({d['evaluatorName'] for d in descriptions}) != 4):
        raise ValueError('本看板仅支持 Builtin.Helpfulness、Correctness、ToolSelectionAccuracy 和一个自定义 evaluator')
    if display_scale(custom[0]) != (1, 5):
        raise ValueError('自定义 evaluator 的真实 ratingScale 与看板的 1–5 尺度不同')
    group = result_log_group or state.get('result_log_group')
    if not group:
        raise ValueError('Online 配置缺少结果日志组')
    body = {**time_range(start, end), 'widgets': []}

    def add(kind, title, x, y, width, height, **properties):
        """应用以明确坐标布置组件，所有指标和日志组件各自固定到 state 的 Region。"""
        # CloudWatch text 组件用 Markdown 标题；其 schema 不接受独立 title 属性。
        props = dict(properties)
        if kind != 'text':
            props['title'] = title
            props['region'] = state['region']
        body['widgets'].append({'type': kind, 'x': x, 'y': y, 'width': width,
                                'height': height, 'properties': props})

    def metric(description, stat):
        """CloudWatch 严格按 service/config 两个维度读取单个 evaluator 的指标。"""
        return ['Bedrock-AgentCore/Evaluations', description['evaluatorName'],
                'service.name', state['service'], 'onlineEvaluationConfigId', state['online_config_id'],
                {'stat': stat, 'label': description['evaluatorName']}]

    add('text', '应用与 CloudWatch 的统计口径', 0, 0, 24, 6, markdown=(
        '## AgentCore Evaluations 评分观测\n'
        f'应用选择服务 `{state["service"]}`、配置 `{state["online_config_id"]}`。'
        'CloudWatch Dashboard 名称在整个账号内共享；各图表读取指定 Region 的数据。\n\n'
        'AgentCore Evaluations 调用 evaluator 并写入评分/错误日志；CloudWatch 从 EMF'
        '（Embedded Metric Format，嵌入式指标格式）日志提取数值指标。'
        '各 evaluator 衡量不同方面，评分层级可能是 TRACE 或 SPAN；这些指标不是总分。'
        '应用根据本项目实测结果为这三项内置指标显示 0–1 范围，'
        '不使用内置提示词 ratingScale 换算运行分数或标签；应用只从自定义 ratingScale 读取 1–5 范围。\n\n'
        'CloudWatch 顶部卡片显示整个所选时段的 Average（该指标 Sum / SampleCount），'
        '不是最后五分钟的值，也不是各时间桶均值的再平均。趋势图按 300 秒分桶；'
        'SampleCount 是每项指标的评分样本数，不是会话数；Sum 只在同一指标内累加。'
        '应用不跨指标加总，不将缺失评分填成 0；CloudWatch 保留空白和断点。\n\n'
        'CloudWatch 指标按 EMF `_aws.Timestamp` 归属时间，Logs Insights 按日志 `@timestamp`'
        '筛选；AgentCore 的处理延迟可能使两者条数不同。'
        '默认窗口为最近三小时；本页面可固定历史时间范围；'
        '查看其他时间时使用控制台右上角时间选择器。CloudWatch 的数据可见范围受 AWS 数据保留期限制。'))
    for index, description in enumerate(descriptions):
        low, high = display_scale(description)
        add('metric', f'CloudWatch 全时段均值 · {description["evaluatorName"]}（{low:g}–{high:g}）',
            index * 6, 6, 6, 4, metrics=[metric(description, 'Average')],
            view='singleValue', stat='Average', period=300, setPeriodToTimeRange=True,
            sparkline=False)
    for x, title, items, low, high in (
            (0, 'CloudWatch 内置评分趋势（0–1）', builtin, 0, 1),
            (12, 'CloudWatch 自定义评分趋势（1–5）', custom, 1, 5)):
        add('metric', title, x, 10, 12, 6, metrics=[metric(d, 'Average') for d in items],
            view='timeSeries', stat='Average', period=300, stacked=False,
            yAxis={'left': {'min': low, 'max': high}})
    for x, stat, title in ((0, 'SampleCount', 'CloudWatch 各指标评分样本数（每 300 秒）'),
                           (12, 'Sum', 'CloudWatch 各指标分值之和（每 300 秒，逐项展示）')):
        add('metric', title, x, 16, 12, 6, metrics=[metric(d, stat) for d in descriptions],
            view='timeSeries', stat=stat, period=300, stacked=False)
    summary, recent = result_queries(state, group)
    add('log', 'CloudWatch 日志统计 · 各 evaluator 评分条数 / 均值 / 错误数',
        0, 22, 24, 6, query=summary, view='table')
    add('log', 'CloudWatch 最近评分与错误 · 会话 / Trace / evaluator / 分值 / 解释',
        0, 28, 24, 8, query=recent, view='table')
    return body


def dashboard_name(config, override=None):
    """应用以 Online 配置生成 ASCII 名称，操作者可用 --name 选择新的账号级名称。"""
    name = override if override is not None else ('agentcore-evaluations-' + re.sub(
        r'[^A-Za-z0-9_-]', '-', config['onlineEvaluationConfigId']))[:255]
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,255}', name):
        raise ValueError('Dashboard 名称必须为 1–255 个 ASCII 字母、数字、连字符或下划线')
    return name


def apply_dashboard(client, name, body, receipt, receipt_path):
    """应用只创建缺失的 Dashboard；同内容不操作，异内容拒绝覆盖并保留回执。"""
    try:
        receipt['status'] = 'checking'
        write_json(receipt_path, receipt)
        try:
            existing = client.get_dashboard(DashboardName=name)
        except ClientError as exc:
            if exc.response['Error']['Code'] not in {'ResourceNotFound', 'DashboardNotFoundError'}:
                raise
            existing = None
        if existing is not None:
            receipt['getDashboard'] = existing
            if json.loads(existing['DashboardBody']) != body:
                raise ValueError('同名 Dashboard 已存在且内容不同，应用拒绝覆盖；请用 --name 指定新名称')
            receipt['status'] = 'unchanged'
        else:
            # CloudWatch 没有条件创建接口；调用方须避免其他操作者并发写入同名 Dashboard。
            receipt['status'] = 'creating'
            write_json(receipt_path, receipt)
            response = client.put_dashboard(DashboardName=name, DashboardBody=json.dumps(body, ensure_ascii=False))
            receipt['putDashboard'] = response
            write_json(receipt_path, receipt)
            if response.get('DashboardValidationMessages'):
                raise ValueError('PutDashboard 返回验证消息；应用保留 Dashboard、body 和 receipt，请检查现场')
            receipt['getDashboard'] = client.get_dashboard(DashboardName=name)
            if json.loads(receipt['getDashboard']['DashboardBody']) != body:
                raise ValueError('GetDashboard 回读内容不一致；应用保留现场，不尝试覆盖或清理')
            receipt['status'] = 'created'
        receipt['dashboardArn'] = receipt['getDashboard']['DashboardArn']
    except Exception as exc:
        receipt.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        receipt['updatedAt'] = now()
        write_json(receipt_path, receipt)
    return receipt


def main():
    """应用先核对 AWS 账号，再读取真实元数据；只有 --apply 分支写入云端 Dashboard。"""
    p = parser(__doc__)
    p.add_argument('--name', help='账号内唯一的 ASCII Dashboard 名称；默认从配置 ID 生成')
    p.add_argument('--output', help='本地 Dashboard body JSON 路径；默认 WORK_DIR/dashboard/唯一编号/body.json')
    p.add_argument('--start', default='-PT3H', help='含时区的 ISO 8601 开始时间；默认 -PT3H')
    p.add_argument('--end', help='含时区的 ISO 8601 结束时间；默认省略，固定历史窗口时须同时指定 start/end')
    p.add_argument('--apply', action='store_true', help='显式创建云端 Dashboard；缺省仅读取元数据并生成本地文件')
    args = p.parse_args()
    time_range(args.start, args.end)
    path = state_path(args)
    state = read_state(path)
    if not state.get('account'):
        raise ValueError('state 缺少 account，应用无法核对 AWS 身份')
    output = (local_path(args.output) if args.output else
              local_path(args.work_dir) / 'dashboard' / uuid.uuid4().hex / 'body.json')
    receipt_path = output.with_name(output.stem + '.receipt.json')
    if path in (output, receipt_path):
        raise ValueError('body/receipt 路径不能覆盖 state')
    session, identity = aws_session(state['region'], args.profile, state['account'])
    cp = session.client('bedrock-agentcore-control')
    config = cp.get_online_evaluation_config(onlineEvaluationConfigId=state['online_config_id'])
    if config['onlineEvaluationConfigId'] != state['online_config_id']:
        raise ValueError('远端 Online 配置 ID 与 state 不一致')
    if state['service'] not in config['dataSourceConfig']['cloudWatchLogs']['serviceNames']:
        raise ValueError('远端 Online 配置未包含 state 中的 service')
    descriptions = []
    for evaluator in config['evaluators']:
        description = cp.get_evaluator(evaluatorId=evaluator['evaluatorId'])
        if description['evaluatorId'] != evaluator['evaluatorId']:
            raise ValueError('远端 evaluator ID 与 Online 配置不一致')
        descriptions.append(description)
    body = build_dashboard(state, descriptions, start=args.start, end=args.end,
                           result_log_group=config['outputConfig']['cloudWatchConfig']['logGroupName'])
    name = dashboard_name(config, args.name)
    url = (f'https://{state["region"]}.console.aws.amazon.com/cloudwatch/home?'
           f'region={url_quote(state["region"], safe="")}#dashboards:name={url_quote(name, safe="")}')
    receipt = {'status': 'rendered', 'createdAt': now(), 'account': state['account'],
               'region': state['region'], 'callerArn': identity['Arn'], 'statePath': str(path),
               'dashboardName': name, 'dashboardUrl': url, 'bodyPath': str(output),
               'onlineEvaluationConfigId': config['onlineEvaluationConfigId'],
               'onlineEvaluationConfigArn': config['onlineEvaluationConfigArn'],
               'evaluators': [{'evaluatorId': d['evaluatorId'], 'evaluatorArn': d['evaluatorArn'],
                               'metricName': d['evaluatorName'],
                               'ratingScale': d['evaluatorConfig']['llmAsAJudge']['ratingScale']}
                              for d in descriptions]}
    write_json(output, body)
    write_json(receipt_path, receipt)
    if args.apply:
        apply_dashboard(session.client('cloudwatch'), name, body, receipt, receipt_path)
    print(json.dumps({'name': name, 'url': url, 'status': receipt['status'], 'body': str(output),
                      'receipt': str(receipt_path)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    entrypoint(main)
