"""测试执行者验证 Runtime 镜像升级保留配置并拒绝覆盖未知版本，不调用 AWS。"""

from copy import deepcopy
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from update import update_request, verify_runtime
from botocore.session import Session
from botocore.validate import validate_parameters
from test_deploy import sample_state
from deploy import environment


class RuntimeUpdateTests(unittest.TestCase):
    """测试执行者覆盖升级中的状态漂移与完整配置保留两个关键边界。"""

    def setUp(self):
        """测试执行者构造匿名旧版本与对应服务响应。"""
        self.previous = sample_state()
        self.previous.update(runtime_id='test_java_runtime-abcdefghij', runtime_version='2',
            runtime_arn='arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test_java_runtime-abcdefghij',
            environment_variables=environment(self.previous))
        self.detail = {'agentRuntimeId': self.previous['runtime_id'], 'agentRuntimeArn': self.previous['runtime_arn'],
            'agentRuntimeVersion': '2', 'status': 'READY', 'roleArn': self.previous['role_arn'],
            'environmentVariables': deepcopy(self.previous['environment_variables']),
            'agentRuntimeArtifact': {'containerConfiguration': {'containerUri': self.previous['image_uri']}},
            'networkConfiguration': {'networkMode': 'PUBLIC'}, 'protocolConfiguration': {'serverProtocol': 'HTTP'},
            'lifecycleConfiguration': {'idleRuntimeSessionTimeout': 900, 'maxLifetime': 3600},
            'metadataConfiguration': {'requireMMDSV2': True}, 'description': 'existing configuration'}
        self.previous['runtime_response'] = deepcopy(self.detail)

    def test_upgrade_rejects_remote_drift(self):
        """测试执行者确认已被其他部署修改的版本、镜像、权限或环境不能被静默覆盖。"""
        verify_runtime(self.previous, self.detail)
        for key, value in [('agentRuntimeVersion', '3'), ('roleArn', 'foreign'), ('environmentVariables', {}),
                           ('status', 'UPDATING'), ('agentRuntimeArtifact', {})]:
            changed = {**self.detail, key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                verify_runtime(self.previous, changed)

    def test_request_preserves_every_supported_configuration_field(self):
        """测试执行者用真实 SDK schema 检查镜像更新只替换 artifact，其余可更新字段保持原值。"""
        model = Session().get_service_model('bedrock-agentcore-control').operation_model('UpdateAgentRuntime')
        uri = self.previous['image_uri'].split('@')[0] + '@sha256:' + 'e' * 64
        request = update_request(self.detail, uri, model.input_shape.members)
        request.update(agentRuntimeId=self.previous['runtime_id'], clientToken='a' * 32 + '-update')
        validate_parameters(request, model.input_shape)
        self.assertEqual(request['agentRuntimeArtifact']['containerConfiguration']['containerUri'], uri)
        for key in ('environmentVariables', 'networkConfiguration', 'lifecycleConfiguration', 'roleArn',
                    'protocolConfiguration', 'metadataConfiguration', 'description'):
            self.assertEqual(request[key], self.detail[key])
        self.assertNotIn('agentRuntimeVersion', request)
        self.assertNotIn('status', request)


if __name__ == '__main__':
    unittest.main()
