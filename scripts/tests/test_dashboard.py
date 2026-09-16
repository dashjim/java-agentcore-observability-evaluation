"""本地测试替身验证看板统计、尺度和创建边界；测试程序不访问 AWS。"""

from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
import uuid

from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import local_path, read_state, write_json
import dashboard


class DashboardTests(unittest.TestCase):
    """测试程序只使用匿名元数据，工作文件保留在调用方 WORK_DIR。"""

    def setUp(self):
        """测试程序为各测试隔离工作目录，并模拟真实 API 的内置提示词档位。"""
        self.work = local_path(os.environ['WORK_DIR']) / 'dashboard-tests' / uuid.uuid4().hex
        self.work.mkdir(parents=True)
        self.state = {'account': 'test-account', 'region': 'us-east-1', 'partition': 'aws',
                      'service': 'test-service', 'online_config_id': 'test-config',
                      'result_log_group': '/test/results', 'retain_until_user_requests_deletion': True}
        self.path = self.work / 'aws-state.json'
        self.receipt_path = self.work / 'body.receipt.json'
        self.descriptions = []
        for index, identifier in enumerate(('Builtin.Helpfulness', 'Builtin.Correctness',
                                             'Builtin.ToolSelectionAccuracy', 'test-custom-id')):
            values = [0, 6] if index == 0 else ([1, 3, 5] if index == 3 else [0, 1])
            self.descriptions.append({'evaluatorId': identifier, 'evaluatorArn': 'test-arn',
                'evaluatorName': f'test-metric-{index}', 'evaluatorConfig': {'llmAsAJudge': {
                    'ratingScale': {'numerical': [{'value': value} for value in values]}}}})
        self.body = dashboard.build_dashboard(self.state, self.descriptions)

    def test_statistics_use_full_range_and_independent_metrics(self):
        """CloudWatch 卡片必须统计全时段均值；各指标保留两维度且无补零或合成表达式。"""
        self.assertEqual(self.body['start'], '-PT3H')
        self.assertNotIn('end', self.body)
        self.assertEqual(self.body['periodOverride'], 'inherit')
        cards = []
        stats = set()
        for widget in self.body['widgets']:
            self.assertLessEqual(widget['x'] + widget['width'], 24)
            props = widget['properties']
            if widget['type'] != 'text':
                self.assertEqual(props['region'], self.state['region'])
            if widget['type'] != 'metric':
                continue
            self.assertEqual(props['period'], 300)
            stats.add(props['stat'])
            if props['view'] == 'singleValue':
                cards.append(props)
                self.assertIs(props['setPeriodToTimeRange'], True)
                self.assertEqual(props['stat'], 'Average')
                self.assertEqual(len(props['metrics']), 1)
            for metric in props['metrics']:
                self.assertEqual(metric[0], 'Bedrock-AgentCore/Evaluations')
                self.assertIn(metric[1], [d['evaluatorName'] for d in self.descriptions])
                self.assertEqual(metric[2:-1], ['service.name', 'test-service',
                                               'onlineEvaluationConfigId', 'test-config'])
                self.assertEqual(metric[-1]['stat'], props['stat'])
                self.assertNotIn('expression', metric[-1])
        self.assertEqual(len(cards), 4)
        self.assertEqual(stats, {'Average', 'SampleCount', 'Sum'})
        logs = [w['properties']['query'] for w in self.body['widgets'] if w['type'] == 'log']
        self.assertEqual(len(logs), 2)
        for query in logs:
            self.assertTrue(query.startswith("SOURCE '/test/results'\n"))
            self.assertIn('jsonParse(@message)', query)
            self.assertNotIn('coalesce(score', query)
        self.assertIn('count(score) as score_samples', logs[0])
        self.assertIn('avg(score) as average_score', logs[0])
        self.assertIn('sum(evaluation_error) as evaluation_errors by evaluator', logs[0])

    def test_source_rejects_invalid_log_group_names(self):
        """应用拒绝含引号、换行或超长的日志组名，避免 SOURCE 拼接改变查询含义。"""
        for group in ('', '/test/\' | limit 1', '/test/"', '/test/\n', 'a' * 513, '测试'):
            with self.subTest(group=group), self.assertRaisesRegex(ValueError, '日志组名'):
                dashboard.result_queries(self.state, group)
        query, _ = dashboard.result_queries(self.state, '/test/valid.name_#-01')
        self.assertTrue(query.startswith("SOURCE '/test/valid.name_#-01'\n"))

    def test_builtin_prompt_scale_does_not_control_display_scale(self):
        """Helpfulness 的 0–6 或分类提示词档位均显示实测 0–1；自定义趋势单独显示 1–5。"""
        for scale in ({'numerical': [{'value': 0}, {'value': 6}]},
                      {'categorical': [{'label': 'Helpful'}, {'label': 'Unhelpful'}]}):
            with self.subTest(scale=scale):
                self.descriptions[0]['evaluatorConfig']['llmAsAJudge']['ratingScale'] = scale
                before = deepcopy((self.state, self.descriptions))
                body = dashboard.build_dashboard(self.state, self.descriptions)
                self.assertEqual((self.state, self.descriptions), before)
                card = body['widgets'][1]['properties']
                self.assertIn('0–1', card['title'])
                trends = [w['properties'] for w in body['widgets'] if 'yAxis' in w['properties']]
                self.assertEqual([p['yAxis']['left'] for p in trends],
                                 [{'min': 0, 'max': 1}, {'min': 1, 'max': 5}])
                self.assertEqual([len(p['metrics']) for p in trends], [3, 1])
        self.descriptions[0]['evaluatorId'] = 'Builtin.Unknown'
        with self.assertRaisesRegex(ValueError, '仅支持'):
            dashboard.build_dashboard(self.state, self.descriptions)
        self.descriptions[0]['evaluatorId'] = 'Builtin.Helpfulness'
        self.descriptions[-1]['evaluatorConfig']['llmAsAJudge']['ratingScale'] = {'numerical': [{'value': 10}]}
        with self.assertRaisesRegex(ValueError, '1–5'):
            dashboard.build_dashboard(self.state, self.descriptions)

    def test_history_is_absolute_and_timezone_is_required(self):
        """应用保存固定历史窗口，并拒绝无时区、倒序或不完整的固定范围。"""
        body = dashboard.build_dashboard(self.state, self.descriptions,
            start='2026-09-16T14:35:00+08:00', end='2026-09-16T06:55:00Z')
        self.assertEqual(body['start'], '2026-09-16T06:35:00Z')
        self.assertEqual(body['end'], '2026-09-16T06:55:00Z')
        for start, end in [('2026-09-16T06:35:00', None), ('-PT3H', '2026-09-16T06:55:00Z'),
                           ('2026-09-16T06:55:00Z', '2026-09-16T06:35:00Z')]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                dashboard.time_range(start, end)

    def test_existing_dashboard_noop_or_refuse_overwrite(self):
        """应用按解析后内容判断幂等性；同名异内容必须拒绝，不能调用 PutDashboard。"""
        for remote in (deepcopy(self.body), {'widgets': []}):
            with self.subTest(same=remote == self.body):
                client = Mock()
                client.get_dashboard.return_value = {'DashboardBody': json.dumps(remote, indent=4),
                                                     'DashboardArn': 'test-dashboard-arn'}
                if remote == self.body:
                    receipt = dashboard.apply_dashboard(client, 'test-name', self.body, {}, self.receipt_path)
                    self.assertEqual(receipt['status'], 'unchanged')
                else:
                    with self.assertRaisesRegex(ValueError, '拒绝覆盖.*--name'):
                        dashboard.apply_dashboard(client, 'test-name', self.body, {}, self.receipt_path)
                client.put_dashboard.assert_not_called()

    def test_creation_handles_missing_and_checks_validation_and_readback(self):
        """应用识别 AWS 的 ResourceNotFound，并保存验证消息或回读差异，不清理资源。"""
        for outcome in ('created', 'warning', 'mismatch', 'denied'):
            with self.subTest(outcome=outcome):
                client = Mock()
                code = 'AccessDenied' if outcome == 'denied' else 'ResourceNotFound'
                remote = {'widgets': []} if outcome == 'mismatch' else self.body
                client.get_dashboard.side_effect = [ClientError({'Error': {'Code': code}}, 'GetDashboard'),
                    {'DashboardBody': json.dumps(remote), 'DashboardArn': 'test-dashboard-arn'}]
                messages = [{'Message': '测试验证消息', 'DataPath': '/widgets/1'}] if outcome == 'warning' else []
                client.put_dashboard.return_value = {'DashboardValidationMessages': messages}
                if outcome == 'created':
                    dashboard.apply_dashboard(client, 'test-name', self.body, {}, self.receipt_path)
                else:
                    with self.assertRaises((ValueError, ClientError)):
                        dashboard.apply_dashboard(client, 'test-name', self.body, {}, self.receipt_path)
                receipt = read_state(self.receipt_path)
                self.assertEqual(receipt['status'], 'created' if outcome == 'created' else 'failed')
                if outcome == 'warning':
                    self.assertEqual(receipt['putDashboard']['DashboardValidationMessages'], messages)
                    self.assertEqual(client.get_dashboard.call_count, 1)
                if outcome == 'denied':
                    client.put_dashboard.assert_not_called()
                client.delete_dashboards.assert_not_called()

    def test_identity_mismatch_blocks_metadata_and_dashboard_calls(self):
        """测试程序调用真实 common 身份核对代码；账号不匹配时应用不得读取元数据或写 Dashboard。"""
        write_json(self.path, self.state)
        original = self.path.read_bytes()
        session = Mock()
        session.client.return_value.get_caller_identity.return_value = {'Account': 'other-account'}
        with patch.object(sys, 'argv', ['dashboard', '--state', str(self.path),
                                      '--work-dir', str(self.work), '--apply']), \
                patch('boto3.Session', return_value=session):
            with self.assertRaisesRegex(ValueError, '凭证账号与 state 不一致'):
                dashboard.main()
        session.client.assert_called_once_with('sts')
        self.assertEqual(self.path.read_bytes(), original)

    def test_preview_reads_metadata_without_cloudwatch_write_and_preserves_state(self):
        """应用预览读取真实 API 形状、输出控制台 URL 和回执；测试替身确认应用不创建云端客户端。"""
        write_json(self.path, self.state)
        original = self.path.read_bytes()
        session, cp = Mock(), Mock()
        session.client.return_value = cp
        cp.get_online_evaluation_config.return_value = {
            'onlineEvaluationConfigId': self.state['online_config_id'], 'onlineEvaluationConfigArn': 'test-config-arn',
            'dataSourceConfig': {'cloudWatchLogs': {'serviceNames': [self.state['service']]}},
            'outputConfig': {'cloudWatchConfig': {'logGroupName': '/test/current-results'}},
            'evaluators': [{'evaluatorId': d['evaluatorId']} for d in self.descriptions]}
        cp.get_evaluator.side_effect = self.descriptions
        output = self.work / 'body.json'
        with patch.object(sys, 'argv', ['dashboard', '--state', str(self.path), '--output', str(output)]), \
                patch.object(dashboard, 'aws_session', return_value=(session, {'Arn': 'test-caller'})) as identity, \
                patch('builtins.print') as printed:
            dashboard.main()
        identity.assert_called_once_with(self.state['region'], os.environ.get('AWS_PROFILE'), self.state['account'])
        session.client.assert_called_once_with('bedrock-agentcore-control')
        cp.get_online_evaluation_config.assert_called_once_with(onlineEvaluationConfigId=self.state['online_config_id'])
        self.assertEqual(cp.get_evaluator.call_count, 4)
        result = json.loads(printed.call_args.args[0])
        self.assertEqual(result['url'], read_state(self.receipt_path)['dashboardUrl'])
        self.assertIn('https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#dashboards:name=', result['url'])
        self.assertEqual(result['status'], 'rendered')
        queries = [w['properties']['query'] for w in read_state(output)['widgets'] if w['type'] == 'log']
        self.assertTrue(all(query.startswith("SOURCE '/test/current-results'\n") for query in queries))
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
