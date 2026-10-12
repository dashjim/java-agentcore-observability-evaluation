"""测试执行者本地验证权限边界、归属拒绝与安全恢复；测试不调用真实 AWS。"""

import argparse
import base64
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy
from botocore.exceptions import ClientError
from botocore.session import Session
from botocore.validate import validate_parameters


def sample_state():
    """测试执行者构造匿名账号的独立部署状态，不读取工作区真实 AWS 状态。"""
    name, account, owner = 'test_java_runtime', '123456789012', 'a' * 32
    repository = 'java-agentcore/test-java-runtime'
    group = '/aws/bedrock-agentcore/runtimes/test-evaluation'
    return {'schema': deploy.SCHEMA, 'name': name, 'account': account, 'partition': 'aws',
            'region': 'us-east-1', 'owner_id': owner, 'operations': {},
            'repository_name': repository, 'repository_arn': f'arn:aws:ecr:us-east-1:{account}:repository/{repository}',
            'role_name': name + '_runtime_role', 'role_arn': f'arn:aws:iam::{account}:role/{name}_runtime_role',
            'local_image': 'example:test', 'local_image_id': 'sha256:' + 'b' * 64,
            'image_tag': 'build-' + owner,
            'image_uri': f'{account}.dkr.ecr.us-east-1.amazonaws.com/{repository}@sha256:' + 'c' * 64,
            'evaluation': {'name': 'test_evaluation', 'owner_id': 'd' * 32, 'service': 'test-evaluation',
                           'log_group': group, 'log_group_arn': f'arn:aws:logs:us-east-1:{account}:log-group:{group}',
                           'online_config_id': 'test_evaluation-id'},
            'retain_until_user_requests_deletion': True}


class RuntimeDeploymentTests(unittest.TestCase):
    """测试执行者验证会影响 AWS 隔离与凭证处理的外部行为。"""

    def setUp(self):
        """测试执行者将所有临时状态写入工作区 tmp，并自动回收本地测试文件。"""
        root = Path(__file__).resolve().parents[3] / 'tmp'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='runtime-tests-', dir=root)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.state = sample_state()

    def worker(self):
        """测试执行者提供模拟 AWS 客户端，保证测试没有云端副作用。"""
        return deploy.Deployer(MagicMock(), self.state, self.work / 'state.json', self.work, 1)

    def test_policy_scopes_business_and_unscopable_apis(self):
        """测试执行者要求模型、ECR、日志权限受限，只有 AWS 不可限定 ARN 的 API 例外。"""
        trust, policy = deploy.policies(self.state)
        statements = {row['Sid']: row for row in policy['Statement']}
        self.assertEqual(statements['InvokeNovaLiteOnly']['Resource'],
                         'arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0')
        self.assertEqual(statements['ReadOwnedImage']['Resource'], self.state['repository_arn'])
        wildcard = [row for row in policy['Statement'] if row['Resource'] == '*']
        self.assertEqual({row['Sid'] for row in wildcard}, {'RegionalEcrToken', 'RegionalTelemetry'})
        for row in wildcard:
            self.assertEqual(row['Condition'], {'StringEquals': {'aws:RequestedRegion': 'us-east-1'}})
        self.assertIn('xray:PutSpans', statements['RegionalTelemetry']['Action'])
        for row in policy['Statement']:
            self.assertFalse(any('*' in action for action in row['Action']))
        self.assertEqual(trust['Statement'][0]['Condition']['StringEquals']['aws:SourceAccount'], self.state['account'])
        self.assertTrue(trust['Statement'][0]['Condition']['ArnLike']['aws:SourceArn'].endswith('/test_java_runtime-*'))

    def test_state_resume_rejects_account_artifact_and_binding_changes(self):
        """测试执行者确保部署应用不能借 --resume 改用其他账号、镜像或评估资源。"""
        args = argparse.Namespace(name=self.state['name'], region='us-east-1', image=self.state['local_image'])
        identity = {'Account': self.state['account'], 'Arn': 'arn:aws:iam::123456789012:user/test'}
        deploy.validate_resume(self.state, args, identity, self.state['local_image_id'], self.state['evaluation'])
        for key, value in [('account', '000000000000'), ('owner_id', ''), ('local_image_id', 'other'),
                           ('retain_until_user_requests_deletion', False), ('repository_name', 'foreign')]:
            changed = {**self.state, key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                deploy.validate_resume(changed, args, identity, self.state['local_image_id'], self.state['evaluation'])

    def test_resource_requires_owner_and_prior_intent(self):
        """测试执行者确保同名资源、缺少创建意图和删除后重建都不能被部署应用静默接受。"""
        with self.assertRaises(ValueError):
            deploy.require_owner({deploy.OWNER_TAG: 'foreign'}, self.state)
        create = MagicMock()
        worker = self.worker()
        with self.assertRaises(ValueError):
            worker.step('role', lambda: {'arn': 'existing'}, create)
        self.state['operations']['role'] = {'status': 'created', 'resource': {'arn': 'ours'}}
        with self.assertRaises(ValueError):
            worker.step('role', lambda: None, create)
        create.assert_not_called()

    def test_creation_intent_is_persisted_before_cloud_write(self):
        """测试执行者模拟云端失败，确认部署应用留下可恢复创建意图与保留标记。"""
        worker = self.worker()

        def failed_create():
            """模拟服务收到请求后连接中断；测试执行者检查请求前的持久化状态。"""
            saved = json.loads(worker.path.read_text())
            self.assertEqual(saved['operations']['repository']['status'], 'creating')
            self.assertIs(saved['retain_until_user_requests_deletion'], True)
            raise ConnectionError('simulated AWS failure')

        with self.assertRaises(ConnectionError):
            worker.step('repository', lambda: None, failed_create)

    def test_runtime_api_request_matches_installed_sdk(self):
        """测试执行者用 botocore 模型验证真实创建请求，并确认默认 SigV4 和协议。"""
        worker = self.worker()
        endpoint = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test_java_runtime-abcdef/runtime-endpoint/DEFAULT'
        request = {'agentRuntimeName': self.state['name'], 'clientToken': self.state['owner_id'] + '-runtime',
                   'tags': worker.tags, **worker.runtime_request(deploy.environment(self.state, endpoint))}
        model = Session().get_service_model('bedrock-agentcore-control')
        validate_parameters(request, model.operation_model('CreateAgentRuntime').input_shape)
        self.assertNotIn('authorizerConfiguration', request)
        self.assertEqual(request['networkConfiguration'], {'networkMode': 'PUBLIC'})
        self.assertEqual(request['protocolConfiguration'], {'serverProtocol': 'HTTP'})
        self.assertIn('cloud.resource_id=' + endpoint, request['environmentVariables']['OTEL_RESOURCE_ATTRIBUTES'])

    def test_docker_failure_never_exposes_stdin_or_output(self):
        """测试执行者模拟 Docker 把凭证写入 stderr；部署应用仍只返回无敏感值的失败提示。"""
        secret = 'DO-NOT-PRINT-THIS-TEST-TOKEN'
        with patch('deploy.subprocess.run', return_value=subprocess.CompletedProcess([], 1, secret, secret)) as run:
            with self.assertRaises(RuntimeError) as error:
                deploy.docker(['login', '--password-stdin'], stdin=secret)
        self.assertNotIn(secret, str(error.exception))
        self.assertNotIn(secret, str(run.call_args.args))
        self.assertEqual(run.call_args.kwargs['input'], secret)

    def test_push_uses_isolated_config_and_removes_credentials(self):
        """测试执行者验证 ECR token 只经 stdin，部署应用退出后移除隔离登录配置。"""
        worker = self.worker()
        digest, registry = 'sha256:' + 'e' * 64, '123456789012.dkr.ecr.us-east-1.amazonaws.com'
        worker.ecr.describe_images.side_effect = [
            ClientError({'Error': {'Code': 'ImageNotFoundException', 'Message': 'missing'}}, 'DescribeImages'),
            {'imageDetails': [{'imageDigest': digest}]}]
        worker.ecr.get_authorization_token.return_value = {'authorizationData': [{
            'authorizationToken': base64.b64encode(b'AWS:private-test-token').decode(),
            'proxyEndpoint': 'https://' + registry}]}
        calls = []

        def docker_call(args, **kwargs):
            """模拟 Docker 登录行为，将测试凭证写到独立配置以核对 finally 清理。"""
            calls.append((args, kwargs))
            if args[0] == 'login':
                config = Path(kwargs['env']['DOCKER_CONFIG'])
                self.assertTrue(config.is_relative_to(self.work))
                (config / 'config.json').write_text('private-test-token')
            return ''

        with patch('deploy.docker', side_effect=docker_call):
            worker.push({'uri': registry + '/' + self.state['repository_name']})
        login = next(row for row in calls if row[0][0] == 'login')
        self.assertEqual(login[1]['stdin'], 'private-test-token')
        self.assertFalse(Path(login[1]['env']['DOCKER_CONFIG']).exists())
        self.assertNotIn('private-test-token', worker.path.read_text())
        self.assertEqual(self.state['image_digest'], digest)


if __name__ == '__main__':
    unittest.main()
