#!/usr/bin/env python3
"""仅供操作者显式清理已获删除授权的本次自有资源；其他脚本绝不调用此入口。

只要 state 中存在 retain_until_user_requests_deletion 键，本脚本就拒绝执行，
即使值为 false 也拒绝；没有 force 绕过选项。用户后续明确要求删除后，操作者
必须先单独审核并更改保留策略。默认只展示计划；--execute 还需要核对账号和逐项资源。
AgentCore 服务自动生成的评分结果日志未由应用打归属标签，因此本脚本始终保留它们。
"""

import json
import time

from common import (aws_session, entrypoint, missing_ok, now, parser, read_state,
                    state_lock, state_path, write_json)
from setup_evaluation import Provisioner


def ensure_unprotected(state):
    """用户保留标记优先于所有参数；应用在创建任何 AWS 客户端之前阻止清理。"""
    if 'retain_until_user_requests_deletion' in state:
        raise ValueError('state 存在用户保留标记，应用拒绝清理；没有 force 绕过方式')
    if state.get('schema_version') != 1 or not state.get('owner_id'):
        raise ValueError('state 缺少可验证归属记录，应用拒绝清理')


def cleanup_plan(state, delete_logs):
    """应用只纳入创建已确认的资源；未确认操作需要操作者先恢复并核实。"""
    operations = state['operations']
    if any(item['status'] != 'created' for item in operations.values()):
        raise ValueError('state 存在未确认创建操作；请先检查/恢复，应用不猜测资源归属')
    kinds = ['online_config', 'evaluator', 'role', 'resource_policy']
    if delete_logs:
        kinds.append('log_group')
    return [{'kind': kind, **operations[kind]['resource'],
             'confirmation': kind + ':' + operations[kind]['resource']['id']}
            for kind in kinds if kind in operations]


def wait_absent(client, method, parameters, timeout):
    """应用等待 AgentCore 真正删除完成，避免仍在运行的配置失去依赖资源。"""
    deadline = time.monotonic() + timeout
    while missing_ok(getattr(client, method), **parameters) is not None:
        if time.monotonic() >= deadline:
            raise TimeoutError('服务资源仍在删除中；state 和依赖资源已保留，请稍后显式重试')
        time.sleep(5)


def main():
    """操作者确认资源清单；应用先全面预检远端归属，再按依赖顺序删除。"""
    p = parser(__doc__)
    p.add_argument('--execute', action='store_true', help='显式执行删除；缺省仅输出计划')
    p.add_argument('--confirm-account', help='操作者确认的 AWS 账号，必须同时匹配 STS 和 state')
    p.add_argument('--confirm-resource', action='append', default=[], help='逐项填写计划中的 confirmation；可重复')
    p.add_argument('--delete-logs', action='store_true', help='同时删除应用创建且有归属标签的来源日志组；评分结果日志始终保留')
    p.add_argument('--wait-seconds', type=int, default=180)
    args = p.parse_args()
    path = state_path(args)
    ensure_unprotected(read_state(path))
    if args.wait_seconds < 1:
        p.error('--wait-seconds 必须为正数')
    with state_lock(path):
        state = read_state(path)
        ensure_unprotected(state)
        plan = cleanup_plan(state, args.delete_logs)
        print(json.dumps({'account': state['account'], 'region': state['region'], 'resources': plan,
                          'preserved_result_log_group': state.get('result_log_group')}, ensure_ascii=False, indent=2))
        if not args.execute:
            return
        if args.confirm_account != state['account']:
            raise ValueError('--confirm-account 必须准确匹配 state 账号')
        expected = {item['confirmation'] for item in plan}
        if set(args.confirm_resource) != expected or len(args.confirm_resource) != len(expected):
            raise ValueError('--confirm-resource 必须逐项且仅包含计划中的具体资源')
        session, _ = aws_session(state['region'], args.profile, state['account'])
        inspector = Provisioner(session, state, path, args.wait_seconds)
        lookups = {'log_group': inspector.source, 'resource_policy': inspector.delivery_policy,
                   'role': inspector.role, 'evaluator': lambda: inspector.config_resource('evaluator'),
                   'online_config': lambda: inspector.config_resource('online_config')}
        present = set()
        for item in plan:
            remote = lookups[item['kind']]()
            if remote is not None:
                if remote != {'id': item['id'], 'arn': item['arn']}:
                    raise ValueError('远端资源 ID/ARN 与 state 不符，应用拒绝执行')
                present.add(item['kind'])
        if 'role' in present:
            role_name = state['role_name']
            # 应用不删除后来人工附加的策略；发现其他依赖即停止整个清理。
            attached = inspector.iam.list_attached_role_policies(RoleName=role_name)
            inline = inspector.iam.list_role_policies(RoleName=role_name)
            if attached['AttachedPolicies'] or attached.get('IsTruncated') or inline.get('IsTruncated') or set(inline['PolicyNames']) - {'EvaluationOnly'}:
                raise ValueError('角色含额外策略，应用拒绝删除可能被复用的角色')
            if 'EvaluationOnly' in inline['PolicyNames']:
                policy = inspector.iam.get_role_policy(RoleName=role_name, PolicyName='EvaluationOnly')
                if policy['PolicyDocument'] != state['execution_policy']:
                    raise ValueError('角色策略内容与 state 不同，应用拒绝删除已被修改的角色')

        def remove(client, operation, **parameters):
            """应用在删除前记录请求；AWS 错误保留为失败记录，不伪装成清理成功。"""
            audit = {'at': now(), 'operation': operation, 'parameters': parameters, 'status': 'requested'}
            state.setdefault('cleanup', []).append(audit)
            write_json(path, state)
            try:
                missing_ok(getattr(client, operation), **parameters)
                audit['status'] = 'deleted_or_already_absent'
            except Exception as exc:
                audit.update(status='failed', error=str(exc))
                raise
            finally:
                write_json(path, state)

        for item in plan:
            kind, identifier = item['kind'], item['id']
            if kind not in present:
                continue
            # 每个删除前再次核对归属，避免只依赖前面批量预检的结果。
            if lookups[kind]() != {'id': identifier, 'arn': item['arn']}:
                raise ValueError('资源在预检后变化，应用停止删除')
            if kind == 'online_config':
                parameters = {'onlineEvaluationConfigId': identifier}
                remove(inspector.cp, 'delete_online_evaluation_config', **parameters)
                wait_absent(inspector.cp, 'get_online_evaluation_config', parameters, args.wait_seconds)
            elif kind == 'evaluator':
                parameters = {'evaluatorId': identifier}
                remove(inspector.cp, 'delete_evaluator', **parameters)
                wait_absent(inspector.cp, 'get_evaluator', parameters, args.wait_seconds)
            elif kind == 'role':
                remove(inspector.iam, 'delete_role_policy', RoleName=identifier, PolicyName='EvaluationOnly')
                remove(inspector.iam, 'delete_role', RoleName=identifier)
            elif kind == 'resource_policy':
                # 上方 delivery_policy 已复核 ARN、RESOURCE scope 和完整文档；资源级删除只传 ARN。
                remove(inspector.logs, 'delete_resource_policy', resourceArn=item['arn'])
            elif kind == 'log_group':
                remove(inspector.logs, 'delete_log_group', logGroupName=identifier)
        state['cleanup_completed_at'] = now()
        write_json(path, state)
        print('应用已完成本次显式清理；本地证据、评分结果日志及 CloudWatch 已发布指标均保留。')


if __name__ == '__main__':
    entrypoint(main)
