#!/usr/bin/env python3
"""部署应用以新状态文件更新同一 Runtime 镜像；旧版本证据与 AWS 资源始终保留。"""

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy import Deployer, SCHEMA, inspect_image, require_owner
from common import (aws_session, entrypoint, local_path, now, read_state, state_lock, write_json)


def verify_runtime(previous, detail):
    """部署应用拒绝更新不再对应旧 state 的版本、镜像、权限、协议或遥测配置。"""
    expected = {
        'agentRuntimeId': previous['runtime_id'], 'agentRuntimeArn': previous['runtime_arn'],
        'agentRuntimeVersion': previous['runtime_version'], 'status': 'READY',
        'roleArn': previous['role_arn'], 'environmentVariables': previous['environment_variables'],
        'agentRuntimeArtifact': {'containerConfiguration': {'containerUri': previous['image_uri']}}}
    for key in ('networkConfiguration', 'protocolConfiguration', 'lifecycleConfiguration',
                'authorizerConfiguration', 'requestHeaderConfiguration', 'metadataConfiguration',
                'filesystemConfigurations'):
        expected[key] = previous['runtime_response'].get(key)
    for key, value in expected.items():
        if detail.get(key) != value:
            raise ValueError(f'Runtime {key} 已变化；部署应用拒绝覆盖其他更新')


def update_request(detail, new_image, accepted_keys):
    """部署应用完整保留控制面支持的现行配置，仅替换容器镜像 URI。"""
    request = {key: deepcopy(value) for key, value in detail.items()
               if key in accepted_keys and key not in {'agentRuntimeId', 'clientToken'}}
    request['agentRuntimeArtifact'] = {'containerConfiguration': {'containerUri': new_image}}
    return request


def main():
    """操作者选择旧 state 和全新输出；部署应用不创建 IAM/Evaluation，也不删除旧镜像。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, help='前一版 ready 状态文件，只读')
    parser.add_argument('--output-state', required=True, help='新版本状态文件，禁止覆盖')
    parser.add_argument('--image', required=True, help='已构建的 linux/arm64 Docker 镜像')
    parser.add_argument('--work-dir', default=str(Path(__file__).resolve().parents[3] / 'tmp' / 'java-agentcore-runtime'))
    parser.add_argument('--profile', default=os.environ.get('AWS_PROFILE'))
    parser.add_argument('--timeout', type=int, default=900)
    args = parser.parse_args()
    source, output, work = local_path(args.state), local_path(args.output_state), local_path(args.work_dir)
    if source == output or output.exists():
        raise ValueError('新版本 state 必须是尚不存在的独立文件；部署应用不会覆盖旧证据')
    if args.timeout < 1:
        parser.error('--timeout 必须为正整数')
    # 同时锁住旧状态与新状态，阻止同一工作区并行启动两次升级；旧文件内容不变。
    with state_lock(source), state_lock(output):
        previous = read_state(source)
        if (previous.get('schema') != SCHEMA or previous.get('status') != 'ready'
                or previous.get('retain_until_user_requests_deletion') is not True):
            raise ValueError('前一版本必须是 ready 且保留资源的 Runtime state')
        image_id = inspect_image(args.image)
        if image_id == previous['local_image_id']:
            raise ValueError('本地镜像与前一版相同，无需创建新 Runtime 版本')
        session, _ = aws_session(previous['region'], args.profile, previous['account'])
        worker = Deployer(session, deepcopy(previous), output, work, args.timeout)
        control = worker.cp
        require_owner(control.list_tags_for_resource(resourceArn=previous['runtime_arn'])['tags'], previous)
        repository = worker.repository()  # helper 核对 ECR Owner、ARN 与 immutable tag 策略。
        if repository is None or worker.role() is None:
            raise ValueError('原 ECR 仓库或 IAM 角色缺失；部署应用拒绝重建')
        role = worker.iam.get_role_policy(RoleName=previous['role_name'], PolicyName='RuntimeOnly')
        if role['PolicyDocument'] != previous['execution_policy']:
            raise ValueError('Runtime IAM 权限已变化；部署应用拒绝升级')
        if (worker.iam.list_attached_role_policies(RoleName=previous['role_name'])['AttachedPolicies']
                or set(worker.iam.list_role_policies(RoleName=previous['role_name'])['PolicyNames']) != {'RuntimeOnly'}):
            raise ValueError('Runtime IAM 角色存在非预期策略')
        current = control.get_agent_runtime(agentRuntimeId=previous['runtime_id'])
        verify_runtime(previous, current)
        endpoint = control.get_agent_runtime_endpoint(agentRuntimeId=previous['runtime_id'], endpointName='DEFAULT')
        if (endpoint['status'] != 'READY' or endpoint['liveVersion'] != previous['runtime_version']
                or endpoint['agentRuntimeEndpointArn'] != previous['endpoint_arn']):
            raise ValueError('DEFAULT Endpoint 与旧版本不符；部署应用拒绝升级')
        state = worker.state
        revision_id = uuid.uuid4().hex
        state.update(previous_state=str(source), previous_state_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            previous_runtime_version=previous['runtime_version'], previous_image_digest=previous['image_digest'],
            revision_id=revision_id, revision_created_at=now(), status='updating', operations={},
            local_image=args.image, local_image_id=image_id, image_tag='build-' + revision_id)
        for key in ('image_digest', 'image_uri', 'image_tag_uri', 'errors'):
            state.pop(key, None)
        write_json(output, state, exclusive=True)
        try:
            worker.push(repository)
            # 推送镜像可能耗时；应用在更新前再次确认没有其他部署更换 Runtime 版本。
            latest = control.get_agent_runtime(agentRuntimeId=previous['runtime_id'])
            verify_runtime(previous, latest)
            accepted = control.meta.service_model.operation_model('UpdateAgentRuntime').input_shape.members
            request = update_request(latest, state['image_uri'], accepted)
            state['update_request'] = request
            state['operations']['runtime_update'] = {'status': 'updating', 'started_at': now()}
            worker.save()
            response = control.update_agent_runtime(agentRuntimeId=previous['runtime_id'],
                clientToken=revision_id + '-update', **request)
            state['runtime_update_response'] = response
            worker.save()
            # AWS API 没有 If-Match；若检查后仍发生并发写，应用显式报错并保留现场，不自动回滚。
            expected_version = str(int(previous['runtime_version']) + 1)
            if response['agentRuntimeVersion'] != expected_version:
                raise ValueError('Runtime 返回非预期新版本；可能有并发更新，部署应用停止且不自动回滚')
            detail = worker.wait()
            if detail['agentRuntimeVersion'] != expected_version or detail['agentRuntimeArtifact'] != request['agentRuntimeArtifact']:
                raise ValueError('Runtime 更新后版本或镜像与本次请求不符')
            for key in request:
                if detail.get(key) != request[key]:
                    raise ValueError(f'Runtime 更新后 {key} 与保留配置不符')
            endpoint = worker.wait(endpoint=True)
            if endpoint['liveVersion'] != expected_version:
                # DEFAULT 仍指向旧版本时，应用仅在 Runtime 最新版本未再变化的情况下推进 Endpoint。
                latest = control.get_agent_runtime(agentRuntimeId=previous['runtime_id'])
                if latest['agentRuntimeVersion'] != expected_version:
                    raise ValueError('DEFAULT 同步前发现其他 Runtime 更新；部署应用停止')
                control.update_agent_runtime_endpoint(agentRuntimeId=previous['runtime_id'], endpointName='DEFAULT',
                    agentRuntimeVersion=expected_version, clientToken=revision_id + '-default')
                endpoint = worker.wait(endpoint=True)
            if endpoint['liveVersion'] != expected_version or endpoint.get('targetVersion') not in (None, expected_version):
                raise ValueError('DEFAULT Endpoint 未指向新 Runtime 版本')
            state.update(status='ready', runtime_version=expected_version, endpoint_status=endpoint['status'],
                         endpoint_arn=endpoint['agentRuntimeEndpointArn'])
            state['operations']['runtime_update'] = {'status': 'created', 'confirmed_at': now()}
            worker.save()
        except BaseException as exc:
            state['status'] = 'failed'
            state.setdefault('errors', []).append({'at': now(), 'type': type(exc).__name__})
            worker.save()
            raise
    print(json.dumps({'state': str(output), 'previous_state': str(source), **{key: state[key] for key in (
        'runtime_arn', 'runtime_version', 'image_digest', 'endpoint_status', 'retain_until_user_requests_deletion')}},
        ensure_ascii=False, indent=2))


if __name__ == '__main__':
    entrypoint(main)
