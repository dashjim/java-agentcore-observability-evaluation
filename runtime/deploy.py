#!/usr/bin/env python3
"""部署应用创建独立 Java Runtime；操作者显式恢复，脚本从不清理 AWS 资源。"""

import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from common import (aws_session, entrypoint, local_path, missing_ok, now, pages,
                    read_state, state_lock, write_json)

OWNER_TAG = 'JavaAgentCoreRuntimeOwner'
PURPOSE = 'java-agentcore-runtime-evaluation'
SCHEMA = 'java-agentcore-runtime-v1'
MODEL = 'amazon.nova-lite-v1:0'


def require_owner(tags, state):
    """部署应用拒绝接管只有相似名称、没有本次归属标签的云端资源。"""
    if isinstance(tags, list):
        tags = {row['Key']: row['Value'] for row in tags}
    if not state.get('owner_id') or tags.get(OWNER_TAG) != state['owner_id']:
        raise ValueError('云端归属标签不符；部署应用拒绝接管资源')


def policies(state):
    """部署应用限定业务 ARN；AWS 不支持资源级授权的 API 加 Region 条件。"""
    region, account, partition, name = (state[k] for k in ('region', 'account', 'partition', 'name'))
    logs = f'arn:{partition}:logs:{region}:{account}:log-group:'
    runtime_group = logs + '/aws/bedrock-agentcore/runtimes/' + name + '-*'
    evaluation_group = state['evaluation']['log_group_arn']
    trust = {'Version': '2012-10-17', 'Statement': [{
        'Effect': 'Allow', 'Principal': {'Service': 'bedrock-agentcore.amazonaws.com'},
        'Action': 'sts:AssumeRole', 'Condition': {
            'StringEquals': {'aws:SourceAccount': account},
            'ArnLike': {'aws:SourceArn': f'arn:{partition}:bedrock-agentcore:{region}:{account}:runtime/{name}-*'}}}]}
    policy = {'Version': '2012-10-17', 'Statement': [
        {'Sid': 'ReadOwnedImage', 'Effect': 'Allow',
         'Action': ['ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer'], 'Resource': state['repository_arn']},
        # ECR GetAuthorizationToken 和 X-Ray 写入没有可用的资源级 ARN。
        {'Sid': 'RegionalEcrToken', 'Effect': 'Allow', 'Action': ['ecr:GetAuthorizationToken'],
         'Resource': '*', 'Condition': {'StringEquals': {'aws:RequestedRegion': region}}},
        {'Sid': 'RegionalTelemetry', 'Effect': 'Allow',
         'Action': ['xray:PutTraceSegments', 'xray:PutTelemetryRecords', 'xray:PutSpans',
                    'xray:GetSamplingRules', 'xray:GetSamplingTargets'],
         'Resource': '*', 'Condition': {'StringEquals': {'aws:RequestedRegion': region}}},
        # CloudWatch 目录查询仅获得组元数据，AWS 要求使用本账号目录 ARN。
        {'Sid': 'DescribeLogCatalog', 'Effect': 'Allow', 'Action': ['logs:DescribeLogGroups'],
         'Resource': logs + '*'},
        {'Sid': 'OwnedLogGroups', 'Effect': 'Allow',
         'Action': ['logs:CreateLogGroup', 'logs:DescribeLogStreams'],
         'Resource': [runtime_group, evaluation_group, evaluation_group + ':*']},
        {'Sid': 'RuntimeLogDelivery', 'Effect': 'Allow', 'Action': ['logs:PutResourcePolicy'],
         'Resource': runtime_group},
        {'Sid': 'OwnedLogStreams', 'Effect': 'Allow',
         'Action': ['logs:CreateLogStream', 'logs:PutLogEvents'],
         'Resource': [runtime_group + ':log-stream:*', evaluation_group + ':log-stream:*']},
        {'Sid': 'InvokeNovaLiteOnly', 'Effect': 'Allow', 'Action': ['bedrock:InvokeModel'],
         'Resource': f'arn:{partition}:bedrock:{region}::foundation-model/{MODEL}'}]}
    return trust, policy


def environment(state, endpoint_arn=None):
    """部署应用将评估来源与 ADOT 出口绑定；ADOT Java 负责签名发送业务 spans。"""
    region, evaluation = state['region'], state['evaluation']
    group, service = evaluation['log_group'], evaluation['service']
    attrs = {'service.name': service, 'aws.service.type': 'gen_ai_agent', 'aws.log.group.names': group}
    if endpoint_arn:
        attrs['cloud.resource_id'] = endpoint_arn
    return {
        'AWS_REGION': region, 'AWS_DEFAULT_REGION': region, 'BEDROCK_MODEL_ID': MODEL,
        'OTEL_SERVICE_NAME': service, 'LOG_GROUP': group,
        'OTEL_RESOURCE_ATTRIBUTES': ','.join(f'{key}={value}' for key, value in attrs.items()),
        'OTEL_TRACES_EXPORTER': 'otlp', 'OTEL_METRICS_EXPORTER': 'none', 'OTEL_LOGS_EXPORTER': 'none',
        'OTEL_EXPORTER_OTLP_TRACES_PROTOCOL': 'http/protobuf',
        'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT': f'https://xray.{region}.amazonaws.com/v1/traces',
        'OTEL_EXPORTER_OTLP_TRACES_HEADERS': f'x-aws-log-group={group},x-aws-log-stream=spans',
        'OTEL_TRACES_SAMPLER': 'always_on', 'OTEL_AWS_APPLICATION_SIGNALS_ENABLED': 'false',
        'AGENT_OBSERVABILITY_ENABLED': 'true', 'AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT': 'true',
        'OTEL_BSP_SCHEDULE_DELAY': '1000', 'OTEL_BSP_EXPORT_TIMEOUT': '30000',
        'OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT': '131072', 'CAPTURE_CONTENT': 'true'}


def docker(arguments, *, env=None, stdin=None):
    """部署应用以 argv/stdin 调用 Docker；失败信息不输出 token 或 Docker 配置。"""
    result = subprocess.run(['docker', *arguments], input=stdin, text=True, capture_output=True, env=env)
    if result.returncode:
        raise RuntimeError(f'Docker {arguments[0]} 失败（退出码 {result.returncode}）；凭证与原始输出未记录')
    return result.stdout


def inspect_image(image):
    """部署应用在任何云端写入前确认本地镜像为 Linux ARM64。"""
    rows = json.loads(docker(['image', 'inspect', image]))
    if len(rows) != 1 or rows[0].get('Architecture') != 'arm64' or rows[0].get('Os') != 'linux':
        raise ValueError('AgentCore Runtime 镜像必须为 linux/arm64')
    return rows[0]['Id']


def validate_evaluation(value, region, account, partition):
    """部署应用只绑定明确可用、同账号同 Region 且保留资源的评估状态。"""
    if (value.get('schema_version') != 1 or not value.get('owner_id') or value.get('status') != 'ready'
            or value.get('retain_until_user_requests_deletion') is not True):
        raise ValueError('评估 state 必须是 setup_evaluation.py 创建的 ready、保留状态')
    for key, expected in [('region', region), ('account', account), ('partition', partition)]:
        if value.get(key) != expected:
            raise ValueError('评估 state 的账号或 Region 不一致')
    expected = f'arn:{partition}:logs:{region}:{account}:log-group:{value["log_group"]}'
    if value.get('log_group_arn') != expected or not value['log_group'].startswith('/aws/bedrock-agentcore/runtimes/'):
        raise ValueError('评估日志组 ARN 不符')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', value['service']):
        raise ValueError('评估 service 名称不安全')
    return {key: value[key] for key in ('name', 'owner_id', 'service', 'log_group', 'log_group_arn', 'online_config_id')}


def validate_resume(state, args, identity, image_id, evaluation):
    """部署应用禁止旧格式、错误账号、镜像替换或跨评估状态的隐式恢复。"""
    expected = {'schema': SCHEMA, 'name': args.name, 'region': args.region,
                'account': identity['Account'], 'partition': identity['Arn'].split(':')[1],
                'local_image': args.image, 'local_image_id': image_id, 'evaluation': evaluation,
                'retain_until_user_requests_deletion': True}
    if any(state.get(key) != value for key, value in expected.items()):
        raise ValueError('state 与账号、部署参数或保留策略不一致；部署应用拒绝恢复')
    if not re.fullmatch(r'[a-f0-9]{32}', state.get('owner_id', '')):
        raise ValueError('state 缺少安全归属标识')
    repository = 'java-agentcore/' + args.name.lower().replace('_', '-')
    if (state.get('repository_name') != repository or state.get('role_name') != args.name + '_runtime_role'
            or state.get('repository_arn') != f'arn:{state["partition"]}:ecr:{args.region}:{identity["Account"]}:repository/{repository}'):
        raise ValueError('state 的计划资源名称或 ARN 已变化')


class Deployer:
    """部署应用先持久化创建意图，再核对 AWS 标签；AgentCore 服务负责异步启动容器。"""

    def __init__(self, session, state, path, work_dir, timeout):
        """部署应用使用同一个凭证链、账号和 Region 创建服务客户端。"""
        self.state, self.path, self.work_dir, self.timeout = state, path, work_dir, timeout
        self.ecr, self.iam = session.client('ecr'), session.client('iam')
        self.cp, self.logs = session.client('bedrock-agentcore-control'), session.client('logs')
        self.tags = {OWNER_TAG: state['owner_id'], 'Purpose': PURPOSE}

    def save(self):
        """部署应用原子保存状态与保留策略，进程中断不会触发 AWS 清理。"""
        self.state['updated_at'] = now()
        write_json(self.path, self.state)

    def step(self, key, lookup, create):
        """部署应用只恢复已有创建意图且标签匹配的资源，不接管陌生资源。"""
        prior = self.state['operations'].get(key)
        found = lookup()
        if found is not None:
            if not prior or (prior.get('resource') and prior['resource'] != found):
                raise ValueError(f'{key} 已存在或标识变化；部署应用拒绝接管')
        else:
            if prior and prior.get('status') == 'created':
                raise ValueError(f'{key} 已记录成功但云端缺失；部署应用拒绝静默重建')
            self.state['operations'][key] = {'status': 'creating', 'started_at': now()}
            self.save()
            found = create()
        self.state['operations'][key] = {'status': 'created', 'resource': found, 'confirmed_at': now()}
        self.save()
        return found

    def repository(self):
        """部署应用要求独立仓库带本次 Owner 标签并禁止覆盖镜像 tag。"""
        from botocore.exceptions import ClientError
        try:
            row = self.ecr.describe_repositories(repositoryNames=[self.state['repository_name']])['repositories'][0]
        except ClientError as exc:
            if exc.response['Error']['Code'] == 'RepositoryNotFoundException':
                return None
            raise
        require_owner(self.ecr.list_tags_for_resource(resourceArn=row['repositoryArn'])['tags'], self.state)
        if row['repositoryArn'] != self.state['repository_arn'] or row['imageTagMutability'] != 'IMMUTABLE':
            raise ValueError('ECR 仓库 ARN 或 tag 不可变策略与计划不符')
        return {'arn': row['repositoryArn'], 'uri': row['repositoryUri']}

    def push(self, repository):
        """部署应用用短期 ECR token 的 stdin 登录；隔离配置仅存在于工作目录。"""
        from botocore.exceptions import ClientError
        state = self.state
        try:
            row = self.ecr.describe_images(repositoryName=state['repository_name'],
                                            imageIds=[{'imageTag': state['image_tag']}])['imageDetails'][0]
        except ClientError as exc:
            if exc.response['Error']['Code'] != 'ImageNotFoundException':
                raise
            row = None
        if row is not None:
            if not state['operations'].get('image_push'):
                raise ValueError('镜像 tag 已存在但没有本次 push 意图；部署应用拒绝接管')
            if state.get('image_digest') and state['image_digest'] != row['imageDigest']:
                raise ValueError('远端镜像摘要已变化')
        else:
            if state.get('image_digest'):
                raise ValueError('已记录镜像消失；部署应用拒绝静默重建')
            state['operations']['image_push'] = {'status': 'creating', 'started_at': now()}
            self.save()
            tagged = repository['uri'] + ':' + state['image_tag']
            docker(['tag', state['local_image_id'], tagged])
            self.work_dir.mkdir(parents=True, exist_ok=True)
            # 临时 Docker config 与用户全局配置完全隔离；退出时只删除本地短期登录数据。
            with tempfile.TemporaryDirectory(prefix='docker-config-', dir=self.work_dir) as config:
                os.chmod(config, 0o700)
                env = {**os.environ, 'DOCKER_CONFIG': config}
                auth = self.ecr.get_authorization_token(registryIds=[state['account']])['authorizationData'][0]
                user, password = base64.b64decode(auth['authorizationToken']).decode().split(':', 1)
                registry = repository['uri'].split('/')[0]
                if auth['proxyEndpoint'] != 'https://' + registry or user != 'AWS':
                    raise ValueError('ECR 登录端点与本次账号、Region 不一致')
                docker(['login', '--username', user, '--password-stdin', registry], env=env, stdin=password)
                del auth, password
                docker(['push', tagged], env=env)
            row = self.ecr.describe_images(repositoryName=state['repository_name'],
                                           imageIds=[{'imageTag': state['image_tag']}])['imageDetails'][0]
        state['image_digest'] = row['imageDigest']
        state['image_uri'] = repository['uri'] + '@' + row['imageDigest']
        state['image_tag_uri'] = repository['uri'] + ':' + state['image_tag']
        state['operations']['image_push'] = {'status': 'created', 'digest': row['imageDigest'], 'confirmed_at': now()}
        self.save()

    def role(self):
        """部署应用只认可归属、信任策略均匹配的专属执行角色。"""
        found = missing_ok(self.iam.get_role, RoleName=self.state['role_name'])
        if found is None:
            return None
        row = found['Role']
        require_owner(row.get('Tags', []), self.state)
        if row['AssumeRolePolicyDocument'] != self.state['trust_policy']:
            raise ValueError('Runtime 执行角色信任策略发生变化')
        return {'arn': row['Arn'], 'name': row['RoleName']}

    def runtime(self):
        """部署应用完整列举同名 Runtime，并以 Owner 标签确认响应丢失后的创建结果。"""
        rows = [row for row in pages(self.cp, 'list_agent_runtimes', 'agentRuntimes')
                if row['agentRuntimeName'] == self.state['name']]
        if len(rows) > 1:
            raise ValueError('Runtime 出现重名资源')
        if not rows:
            return None
        row = rows[0]
        require_owner(self.cp.list_tags_for_resource(resourceArn=row['agentRuntimeArn'])['tags'], self.state)
        return {'id': row['agentRuntimeId'], 'arn': row['agentRuntimeArn']}

    def runtime_request(self, env):
        """部署应用显式指定 PUBLIC/HTTP；省略 JWT 配置让 Runtime 使用默认 IAM SigV4。"""
        return {'agentRuntimeArtifact': {'containerConfiguration': {'containerUri': self.state['image_uri']}},
                'roleArn': self.state['role_arn'], 'networkConfiguration': {'networkMode': 'PUBLIC'},
                'protocolConfiguration': {'serverProtocol': 'HTTP'},
                'lifecycleConfiguration': {'idleRuntimeSessionTimeout': 900, 'maxLifetime': 3600},
                'environmentVariables': env,
                'description': 'Java order agent with ADOT and Online Evaluations; resources retained'}

    def wait(self, endpoint=False):
        """部署应用有界等待服务 READY，失败时保留角色、镜像、Runtime 与诊断状态。"""
        deadline = time.monotonic() + self.timeout
        while True:
            response = (self.cp.get_agent_runtime_endpoint(agentRuntimeId=self.state['runtime_id'], endpointName='DEFAULT')
                        if endpoint else self.cp.get_agent_runtime(agentRuntimeId=self.state['runtime_id']))
            key = 'endpoint' if endpoint else 'runtime'
            self.state[key + '_response'] = response
            self.save()
            if response['status'] == 'READY':
                return response
            if response['status'] not in {'CREATING', 'UPDATING'}:
                raise ValueError(f'{key}: {response["status"]}: {response.get("failureReason", "请检查 AWS 控制面")}')
            if time.monotonic() >= deadline:
                raise TimeoutError(f'{key} 尚未 READY；资源已保留，可用 --resume 恢复')
            time.sleep(5)

    def provision(self):
        """部署应用依序创建资源；AgentCore Runtime 承载 Java 应用，评估系统读取 ADOT spans。"""
        state = self.state
        # 绑定评估 state 还不足以证明云端归属；应用再次核对评估日志组标签。
        tags = self.logs.list_tags_for_resource(resourceArn=state['evaluation']['log_group_arn'])['tags']
        if tags.get('AgentCoreEvaluationOwner') != state['evaluation']['owner_id']:
            raise ValueError('评估日志组云端归属与 evaluation-state 不一致')
        state['trust_policy'], state['execution_policy'] = policies(state)
        self.save()
        repository = self.step('repository', self.repository, lambda: self.create_repository())
        self.push(repository)
        role = self.step('role', self.role, lambda: self.create_role())
        state['role_arn'] = role['arn']
        found = missing_ok(self.iam.get_role_policy, RoleName=role['name'], PolicyName='RuntimeOnly')
        if found and found['PolicyDocument'] != state['execution_policy']:
            raise ValueError('Runtime 执行角色权限已变化；部署应用拒绝覆盖')
        if self.iam.list_attached_role_policies(RoleName=role['name'])['AttachedPolicies']:
            raise ValueError('Runtime 专属角色存在非预期托管策略')
        if set(self.iam.list_role_policies(RoleName=role['name'])['PolicyNames']) - {'RuntimeOnly'}:
            raise ValueError('Runtime 专属角色存在非预期内联策略')
        if not found:
            self.iam.put_role_policy(RoleName=role['name'], PolicyName='RuntimeOnly',
                                     PolicyDocument=json.dumps(state['execution_policy']))
            time.sleep(10)  # IAM 服务传播权限；失败保留现场，由操作者显式恢复。
        runtime = self.step('runtime', self.runtime, lambda: self.create_runtime())
        state.update(runtime_id=runtime['id'], runtime_arn=runtime['arn'])
        self.save()
        detail, endpoint = self.wait(), self.wait(endpoint=True)
        desired_env = environment(state, endpoint['agentRuntimeEndpointArn'])
        expected = self.runtime_request(desired_env)
        for key in ('agentRuntimeArtifact', 'roleArn', 'networkConfiguration', 'protocolConfiguration', 'lifecycleConfiguration'):
            if detail.get(key) != expected[key]:
                raise ValueError(f'Runtime {key} 与本次部署计划不一致')
        if detail.get('authorizerConfiguration'):
            raise ValueError('Runtime 意外配置了 JWT authorizer；部署应用拒绝覆盖')
        if detail.get('environmentVariables') not in (environment(state), desired_env):
            raise ValueError('Runtime 环境变量与本次部署计划不一致')
        # 首次创建前无法知道服务分配的 Runtime ID；应用仅在此补充准确的 endpoint ARN。
        if detail['environmentVariables'] != desired_env:
            state['operations']['runtime_resource_attribute'] = {'status': 'updating', 'started_at': now()}
            self.save()
            self.cp.update_agent_runtime(agentRuntimeId=state['runtime_id'],
                clientToken=state['owner_id'] + '-resource-attribute', **expected)
            detail = self.wait()
        version = detail['agentRuntimeVersion']
        endpoint = self.wait(endpoint=True)
        if endpoint.get('liveVersion') != version:
            self.cp.update_agent_runtime_endpoint(agentRuntimeId=state['runtime_id'], endpointName='DEFAULT',
                agentRuntimeVersion=version, clientToken=state['owner_id'] + '-default-' + version)
            endpoint = self.wait(endpoint=True)
        if endpoint.get('liveVersion') != version or endpoint.get('targetVersion') not in (None, version):
            raise ValueError('DEFAULT Endpoint 未指向当前 Runtime 版本')
        state.update(status='ready', runtime_version=version, endpoint_arn=endpoint['agentRuntimeEndpointArn'],
                     environment_variables=desired_env, endpoint_status=endpoint['status'])
        state['operations']['runtime_resource_attribute'] = {'status': 'created', 'confirmed_at': now()}
        self.save()

    def create_repository(self):
        """ECR 服务创建本次独立、不可覆盖 tag 的镜像仓库。"""
        row = self.ecr.create_repository(repositoryName=self.state['repository_name'], imageTagMutability='IMMUTABLE',
            imageScanningConfiguration={'scanOnPush': True},
            tags=[{'Key': k, 'Value': v} for k, v in self.tags.items()])['repository']
        return {'arn': row['repositoryArn'], 'uri': row['repositoryUri']}

    def create_role(self):
        """IAM 服务创建本次独立角色；部署应用不挂载宽泛托管策略。"""
        row = self.iam.create_role(RoleName=self.state['role_name'],
            AssumeRolePolicyDocument=json.dumps(self.state['trust_policy']),
            Description='Java AgentCore Runtime only; retained until user requests deletion',
            Tags=[{'Key': k, 'Value': v} for k, v in self.tags.items()])['Role']
        return {'arn': row['Arn'], 'name': row['RoleName']}

    def create_runtime(self):
        """AgentCore 服务创建启用 IAM SigV4 的 HTTP Runtime，客户端 token 使创建请求可恢复。"""
        row = self.cp.create_agent_runtime(agentRuntimeName=self.state['name'], tags=self.tags,
            clientToken=self.state['owner_id'] + '-runtime', **self.runtime_request(environment(self.state)))
        return {'id': row['agentRuntimeId'], 'arn': row['agentRuntimeArn']}


def main():
    """操作者显式部署或恢复；应用检查参数和身份后才执行云端写入。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--region', default='us-east-1')
    parser.add_argument('--name', required=True)
    parser.add_argument('--state', required=True)
    parser.add_argument('--image', required=True, help='已经构建的 linux/arm64 本地 Docker 镜像')
    parser.add_argument('--evaluation-state', required=True)
    parser.add_argument('--profile', default=os.environ.get('AWS_PROFILE'))
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('--work-dir', default=str(Path(__file__).resolve().parents[3] / 'tmp' / 'java-agentcore-runtime'))
    args = parser.parse_args()
    if args.region != 'us-east-1' or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,47}', args.name):
        parser.error('本次部署限定 us-east-1；Runtime 名称须以字母开头且仅含 1..48 位字母、数字、下划线')
    if args.timeout < 1:
        parser.error('--timeout 必须为正整数')
    path, work_dir = local_path(args.state), local_path(args.work_dir)
    with state_lock(path):
        if path.exists() and not args.resume:
            raise ValueError('state 已存在；请选择新 state 或显式 --resume')
        if args.resume and not path.exists():
            raise ValueError('--resume 需要已经存在的 state')
        image_id = inspect_image(args.image)
        session, identity = aws_session(args.region, args.profile)
        partition = identity['Arn'].split(':')[1]
        evaluation = validate_evaluation(read_state(args.evaluation_state), args.region, identity['Account'], partition)
        if args.resume:
            state = read_state(path)
            validate_resume(state, args, identity, image_id, evaluation)
        else:
            owner = uuid.uuid4().hex
            repository = 'java-agentcore/' + args.name.lower().replace('_', '-')
            state = dict(schema=SCHEMA, owner_id=owner, name=args.name, region=args.region,
                account=identity['Account'], partition=partition, local_image=args.image, local_image_id=image_id,
                repository_name=repository,
                repository_arn=f'arn:{partition}:ecr:{args.region}:{identity["Account"]}:repository/{repository}',
                role_name=args.name + '_runtime_role', image_tag='build-' + owner,
                evaluation=evaluation, evaluation_state_path=str(local_path(args.evaluation_state)),
                created_at=now(), status='planned', operations={}, retain_until_user_requests_deletion=True)
            write_json(path, state, exclusive=True)
        worker = Deployer(session, state, path, work_dir, args.timeout)
        try:
            state['status'] = 'provisioning'
            worker.save()
            worker.provision()
        except BaseException as exc:
            state['status'] = 'failed'
            # 错误类别足够定位恢复阶段；不持久化 SDK/Docker 原始异常以避免凭证误入日志。
            state.setdefault('errors', []).append({'at': now(), 'type': type(exc).__name__})
            worker.save()
            raise
    print(json.dumps({'state': str(path), **{key: state.get(key) for key in (
        'runtime_id', 'runtime_arn', 'runtime_version', 'endpoint_arn', 'endpoint_status',
        'image_digest', 'retain_until_user_requests_deletion')}}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    entrypoint(main)
