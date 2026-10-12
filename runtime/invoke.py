#!/usr/bin/env python3
"""调用应用使用 AWS SDK SigV4 调用 Java Runtime，并保留响应与 Session/Trace 证据。"""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy import SCHEMA, require_owner
from common import aws_session, entrypoint, local_path, now, read_state, write_json


def main():
    """调用应用读取专属部署状态、检查账号与归属，随后由 boto3 完成请求签名。"""
    from botocore.config import Config
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True)
    parser.add_argument('--session-id', default=str(uuid.uuid4()))
    parser.add_argument('--output', required=True)
    parser.add_argument('--prompt', default='查询订单 ORD-1001，用 calculator 计算单价乘数量，回答订单状态与商品总价（USD）。')
    parser.add_argument('--profile', default=os.environ.get('AWS_PROFILE'))
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{32,255}', args.session_id):
        parser.error('Session ID 必须含 33..256 位安全字符')
    state, output = read_state(args.state), local_path(args.output)
    if (state.get('schema') != SCHEMA or state.get('status') != 'ready'
            or state.get('retain_until_user_requests_deletion') is not True):
        raise ValueError('Runtime state 必须处于 ready 且保留资源状态')
    if not args.prompt.strip():
        parser.error('--prompt 不能为空')
    session, _ = aws_session(state['region'], args.profile, state['account'])
    control = session.client('bedrock-agentcore-control')
    require_owner(control.list_tags_for_resource(resourceArn=state['runtime_arn'])['tags'], state)
    client = session.client('bedrock-agentcore', config=Config(
        signature_version='v4', connect_timeout=30, read_timeout=360, retries={'max_attempts': 0}))
    evidence = {'started_at': now(), 'runtime_arn': state['runtime_arn'], 'region': state['region'],
                'runtime_version': state['runtime_version'], 'session_id': args.session_id,
                'request': {'prompt': args.prompt}, 'status': 'invoking',
                'retain_until_user_requests_deletion': True}
    write_json(output, evidence, exclusive=True)
    start = time.monotonic()
    try:
        response = client.invoke_agent_runtime(agentRuntimeArn=state['runtime_arn'], qualifier='DEFAULT',
            runtimeSessionId=args.session_id, contentType='application/json', accept='application/json',
            payload=json.dumps(evidence['request'], ensure_ascii=False).encode())
        body = response.pop('response')
        try:
            raw = body.read().decode('utf-8')
        finally:
            body.close()
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = raw
        evidence.update(status='received', response=decoded, response_metadata=response,
                        duration_seconds=round(time.monotonic() - start, 3), completed_at=now())
        if isinstance(decoded, dict):
            evidence['trace_id'] = decoded.get('trace_id')
            evidence['application_session_id'] = decoded.get('session_id')
        write_json(output, evidence)
        if response.get('statusCode', 200) != 200:
            raise ValueError('Runtime 返回非 200 状态；调用应用已保存完整响应')
        if not isinstance(decoded, dict) or decoded.get('session_id') != args.session_id:
            raise ValueError('Java 响应 Session ID 与调用不一致；调用应用已保存完整响应')
    except BaseException as exc:
        evidence.update(status='failed', error_type=type(exc).__name__, completed_at=now())
        write_json(output, evidence)
        raise
    print(json.dumps({'output': str(output), 'session_id': args.session_id,
                      'trace_id': evidence.get('trace_id'), 'response': evidence['response']},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    entrypoint(main)
