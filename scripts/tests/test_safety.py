"""本地安全回归：测试替身替代所有 AWS 客户端，不创建、修改或删除云端资源。"""

import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
import uuid

import botocore.session
from botocore.stub import Stubber

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import local_path, read_state, require_owner, write_json
import cleanup_evaluation as cleanup
from collect_evidence import summarize
import setup_evaluation as setup


class SafetyTests(unittest.TestCase):
    """测试应用的保留、防覆盖、失败恢复和权限边界，工作文件由调用方保留。"""

    def setUp(self):
        """测试程序把每次运行放入显式 WORK_DIR，不使用系统临时目录。"""
        self.work = local_path(os.environ['WORK_DIR']) / 'script-tests' / uuid.uuid4().hex
        self.work.mkdir(parents=True)
        self.path = self.work / 'aws-state.json'
        self.state = {'schema_version': 1, 'owner_id': uuid.uuid4().hex, 'account': 'test-account',
            'region': 'us-east-1', 'partition': 'aws', 'name': 'unit_test', 'service': 'unit-test',
            'log_group_arn': 'arn:aws:logs:us-east-1:test-account:log-group:unit-test',
            'judge_model_id': 'amazon.nova-lite-v1:0', 'judge_regions': ['us-east-1'],
            'judge_inference_profile': None, 'operations': {}}

    def test_protection_blocks_even_false_before_aws(self):
        """cleanup 对存在但值为 false 的标记也拒绝，且不会获取 AWS 身份。"""
        for marker in (True, False, None):
            with self.subTest(marker=marker):
                write_json(self.path, dict(self.state, retain_until_user_requests_deletion=marker))
                with patch.object(sys, 'argv', ['cleanup', '--state', str(self.path), '--execute']), \
                        patch.object(cleanup, 'aws_session') as aws:
                    with self.assertRaisesRegex(ValueError, '保留标记'):
                        cleanup.main()
                    aws.assert_not_called()

    def test_first_state_never_overwritten(self):
        """首次原子写入在目标已存在时失败，原状态内容不变。"""
        write_json(self.path, {'original': True}, exclusive=True)
        with self.assertRaises(FileExistsError):
            write_json(self.path, {'replacement': True}, exclusive=True)
        self.assertEqual(read_state(self.path), {'original': True})

    def test_new_setup_retains_on_preflight_failure(self):
        """Transaction Search 未启用时，setup 仍记录 true 和失败现场，不调用资源创建。"""
        session = Mock()
        session.client.return_value.get_trace_segment_destination.return_value = {'Destination': 'XRay', 'Status': 'ACTIVE'}
        identity = {'Account': 'test-account', 'Arn': 'arn:aws:iam::test-account:user/test'}
        with patch.object(sys, 'argv', ['setup', '--state', str(self.path)]), \
                patch.object(setup, 'aws_session', return_value=(session, identity)), \
                patch.object(setup.Provisioner, 'provision') as provision:
            with self.assertRaisesRegex(ValueError, 'Transaction Search'):
                setup.main()
        provision.assert_not_called()
        saved = read_state(self.path)
        self.assertIs(saved['retain_until_user_requests_deletion'], True)
        self.assertEqual(saved['log_retention'], 'never_expire')
        self.assertEqual(saved['status'], 'failed')
        self.assertEqual(saved['operations'], {})

    def test_step_records_intent_and_recovers_response_loss(self):
        """AWS 响应丢失时，应用留下 creating；恢复时核对远端资源而不重复创建。"""
        worker = setup.Provisioner(Mock(), self.state, self.path, 1)
        create = Mock(side_effect=TimeoutError('模拟响应丢失'))
        with self.assertRaises(TimeoutError):
            worker.step('evaluator', lambda: None, create)
        self.assertEqual(read_state(self.path)['operations']['evaluator']['status'], 'creating')
        resource = {'id': 'owned-evaluator', 'arn': 'owned-arn'}
        create.reset_mock()
        self.assertEqual(worker.step('evaluator', lambda: resource, create), resource)
        create.assert_not_called()

    def test_step_rejects_unplanned_existing_resource(self):
        """相同名称不能授权应用接管资源，setup 不静默覆盖。"""
        worker = setup.Provisioner(Mock(), self.state, self.path, 1)
        create = Mock()
        with self.assertRaisesRegex(ValueError, '拒绝覆盖'):
            worker.step('role', lambda: {'id': 'foreign', 'arn': 'foreign'}, create)
        create.assert_not_called()

    def test_confirmed_missing_resource_not_recreated(self):
        """已经确认成功但被外部移除的资源不能被 resume 静默重建。"""
        self.state['operations']['role'] = {'status': 'created', 'resource': {'id': 'role', 'arn': 'arn'}}
        worker = setup.Provisioner(Mock(), self.state, self.path, 1)
        create = Mock()
        with self.assertRaisesRegex(ValueError, '拒绝静默重建'):
            worker.step('role', lambda: None, create)
        create.assert_not_called()

    def test_cleanup_rejects_pending_operations(self):
        """cleanup 不猜测 creating 状态对应的资源是否已经存在。"""
        self.state['operations']['role'] = {'status': 'creating'}
        with self.assertRaisesRegex(ValueError, '未确认'):
            cleanup.cleanup_plan(self.state, True)

    def test_ownership_requires_remote_tag(self):
        """应用拒绝缺失或不同的远端归属标签。"""
        with self.assertRaisesRegex(ValueError, '归属标签'):
            require_owner({}, self.state)

    def test_policies_scope_catalog_and_shared_spans(self):
        """目录查询范围与内容读取范围分开；执行角色没有全资源权限。"""
        trust, execution, delivery = setup.policies(self.state)
        for document in (execution, delivery):
            for statement in document['Statement']:
                resource = statement['Resource']
                self.assertNotEqual(resource, '*')
                self.assertNotIn('*', resource if isinstance(resource, list) else [])
        catalog = execution['Statement'][0]
        self.assertEqual(catalog['Resource'], 'arn:aws:logs:us-east-1:test-account:log-group:*')
        self.assertEqual(catalog['Action'], ['logs:DescribeLogGroups'])
        self.assertEqual(trust['Statement'][0]['Condition']['StringEquals']['aws:SourceAccount'], 'test-account')

    def policy_worker(self):
        """测试用静态假凭证构造 Logs 客户端，Stubber 拦截全部请求，不访问 AWS。"""
        worker = setup.Provisioner(Mock(), self.state, self.path, 1)
        worker.logs = botocore.session.get_session().create_client('logs', region_name=self.state['region'],
            aws_access_key_id='test', aws_secret_access_key='test')
        self.state['xray_resource_policy'] = setup.policies(self.state)[2]
        arn = self.state['log_group_arn']
        row = {'resourceArn': arn, 'policyScope': 'RESOURCE', 'policyName': 'service-generated-name',
               'policyDocument': json.dumps(self.state['xray_resource_policy'])}
        return worker, row

    def test_resource_policy_resume_put_uses_only_resource_arn(self):
        """应用恢复 creating 状态时只传 ARN/document；Stubber 拒绝额外的 policyName。"""
        worker, row = self.policy_worker()
        arn = self.state['log_group_arn']
        self.state.update(log_group='unit-test', retain_until_user_requests_deletion=True)
        source = {'id': self.state['log_group'], 'arn': arn}
        self.state['operations'] = {'log_group': {'status': 'created', 'resource': source},
                                    'resource_policy': {'status': 'creating'}}
        with Stubber(worker.logs) as stub:
            for stream in ('spans', 'runtime-logs'):
                stub.add_response('describe_log_streams', {'logStreams': [{'logStreamName': stream}]},
                    {'logGroupName': self.state['log_group'], 'logStreamNamePrefix': stream})
            stub.add_response('describe_resource_policies', {'resourcePolicies': []},
                {'resourceArn': arn, 'policyScope': 'RESOURCE'})
            stub.add_response('put_resource_policy', {'resourcePolicy': row},
                {'resourceArn': arn, 'policyDocument': row['policyDocument']})
            with patch.object(worker, 'source', return_value=source), \
                    patch.object(worker, 'role', side_effect=RuntimeError('stop-after-policy')):
                with self.assertRaisesRegex(RuntimeError, 'stop-after-policy'):
                    worker.provision()
            stub.assert_no_pending_responses()
        saved = read_state(self.path)
        self.assertEqual(saved['operations']['resource_policy']['resource'], {'id': arn, 'arn': arn})
        self.assertEqual(saved['operations']['resource_policy']['status'], 'created')
        self.assertIs(saved['retain_until_user_requests_deletion'], True)

    def test_resource_policy_lookup_checks_scope_arn_and_document(self):
        """应用接受服务生成的任意名称，但拒绝账号级策略、错误 ARN 或不同文档。"""
        worker, row = self.policy_worker()
        arn = self.state['log_group_arn']
        for change in ({}, {'policyScope': 'ACCOUNT'}, {'resourceArn': arn + '-other'},
                       {'policyDocument': json.dumps({'Version': '2012-10-17', 'Statement': []})}):
            with self.subTest(change=change), Stubber(worker.logs) as stub:
                stub.add_response('describe_resource_policies', {'resourcePolicies': [{**row, **change}]},
                    {'resourceArn': arn, 'policyScope': 'RESOURCE'})
                if change:
                    with self.assertRaisesRegex(ValueError, '不匹配'):
                        worker.delivery_policy()
                else:
                    self.assertEqual(worker.delivery_policy(), {'id': arn, 'arn': arn})
                stub.assert_no_pending_responses()

    def test_resource_policy_cleanup_verifies_before_arn_only_delete(self):
        """显式 cleanup 两次核对完整策略后只传 resourceArn 删除，Stubber 捕获参数和顺序。"""
        worker, row = self.policy_worker()
        arn = self.state['log_group_arn']
        self.state['operations'] = {'resource_policy': {'status': 'created', 'resource': {'id': arn, 'arn': arn}}}
        write_json(self.path, self.state)
        argv = ['cleanup', '--state', str(self.path), '--execute', '--confirm-account', self.state['account'],
                '--confirm-resource', 'resource_policy:' + arn]
        with Stubber(worker.logs) as stub:
            for _ in range(2):
                stub.add_response('describe_resource_policies', {'resourcePolicies': [row]},
                    {'resourceArn': arn, 'policyScope': 'RESOURCE'})
            stub.add_response('delete_resource_policy', {}, {'resourceArn': arn})
            with patch.object(sys, 'argv', argv), patch.object(cleanup, 'aws_session', return_value=(Mock(), {})), \
                    patch.object(cleanup, 'Provisioner', return_value=worker), patch('builtins.print'):
                cleanup.main()
            stub.assert_no_pending_responses()

    def test_non_json_logs_preserved_without_fake_scores(self):
        """文本日志不会导致整个采集中断，缺失分数不会成为零分或成功记录。"""
        sessions, scores, errors, skipped = summarize([{'message': '普通日志'}], [
            {'message': json.dumps({'attributes': {'session.id': 'demo'}})}])
        self.assertEqual(sessions, {})
        self.assertEqual(scores, [])
        self.assertEqual(errors, [])
        self.assertEqual(skipped, {'source': 1, 'results': 1})

    def test_evaluation_errors_keep_content_separate_from_scores(self):
        """应用单列两次裁判失败并保留原文；真实零分仍是分数，错误不混入 unparsed。"""
        events = []
        for session_id in ('session-001', 'session-002'):
            events.append({'timestamp': 1000, 'message': json.dumps({'traceId': session_id,
                'severityNumber': 17, 'attributes': {'session.id': session_id,
                    'gen_ai.evaluation.name': 'custom-judge', 'error': 1, 'error.type': 'ValueError',
                    'error.message': 'No score found in evaluation result'}})})
        zero_score = {'message': json.dumps({'attributes': {'gen_ai.evaluation.score.value': 0}})}
        _, scores, errors, skipped = summarize([], [*events, zero_score])
        self.assertEqual([row['value'] for row in scores], [0])
        self.assertEqual(len(errors), 2)
        self.assertEqual(skipped, {})
        for row, event in zip(errors, events):
            self.assertEqual(row['rawEvent'], event)
            self.assertEqual(row['errorType'], 'ValueError')
            self.assertEqual(row['errorMessage'], 'No score found in evaluation result')
            self.assertEqual(row['attributes'], json.loads(event['message'])['attributes'])
            self.assertNotIn('value', row)


if __name__ == '__main__':
    unittest.main()
