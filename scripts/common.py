"""脚本公共设施：应用负责本地状态、互斥和 AWS 身份校验，不调用模型。"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import uuid

OWNER_TAG = "AgentCoreEvaluationOwner"
PURPOSE = "java-agentcore-observability-evaluation"


def now():
    """应用生成带时区的审计时间。"""
    return datetime.now(timezone.utc).isoformat()


def local_path(value):
    """应用解析调用方路径，禁止把任务文件写入系统临时目录。"""
    path = Path(value).expanduser().resolve()
    if path == Path('/tmp') or Path('/tmp') in path.parents:
        raise ValueError("任务目录不能位于系统 /tmp；请设置 WORK_DIR 或显式路径")
    return path


def parser(description):
    """为各入口提供一致的工作目录、state 和凭证配置。"""
    result = argparse.ArgumentParser(description=description)
    result.add_argument('--work-dir', default=os.environ.get('WORK_DIR',
                        str(Path(__file__).resolve().parents[1] / 'work')),
                        help='本地工作目录；环境变量 WORK_DIR')
    result.add_argument('--state', default=os.environ.get('STATE_PATH'),
                        help='状态文件；环境变量 STATE_PATH，默认 WORK_DIR/aws-state.json')
    result.add_argument('--profile', default=os.environ.get('AWS_PROFILE'),
                        help='AWS profile；缺省使用标准凭证链')
    return result


def state_path(args):
    """应用优先使用显式 state 路径，否则使用工作目录。"""
    work = local_path(args.work_dir)
    return local_path(args.state) if args.state else work / 'aws-state.json'


def write_json(path, value, *, exclusive=False):
    """应用先 fsync 再原子提交；首次创建禁止覆盖，失败时保留旧版本。"""
    path = local_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.pending')
    try:
        with pending.open('x', encoding='utf-8') as handle:
            os.chmod(pending, 0o600)
            json.dump(value, handle, ensure_ascii=False, indent=2, default=str)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        if exclusive:
            os.link(pending, path)  # 目标已存在时失败，不发生先检查后覆盖的竞态。
        else:
            os.replace(pending, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        pending.unlink(missing_ok=True)


def read_state(path):
    """应用读取 JSON 对象；损坏的 state 必须由操作者检查。"""
    state = json.loads(local_path(path).read_text(encoding='utf-8'))
    if not isinstance(state, dict):
        raise ValueError('state 必须是 JSON 对象')
    return state


@contextmanager
def state_lock(path):
    """应用用 POSIX 文件锁阻止同一 state 的并行 setup/cleanup；崩溃自动释放锁。"""
    path = local_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + '.lock').open('a', encoding='utf-8') as handle:
        os.chmod(handle.name, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('另一个进程正在使用此 state') from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def aws_session(region, profile, account=None):
    """应用通过 STS 核对账号；AgentCore 服务角色与当前操作者身份不同。"""
    import boto3
    session = boto3.Session(region_name=region, profile_name=profile)
    identity = session.client('sts').get_caller_identity()
    if account and identity['Account'] != account:
        raise ValueError('当前 AWS 凭证账号与 state 不一致')
    return session, identity


def pages(client, operation, key, **kwargs):
    """应用完整读取使用 nextToken 的 AWS 目录，不遗漏后续页面。"""
    while True:
        response = getattr(client, operation)(**kwargs)
        yield from response.get(key, [])
        token = response.get('nextToken')
        if not token:
            return
        kwargs['nextToken'] = token


def missing_ok(call, **kwargs):
    """应用只把明确的不存在响应解释为缺失；拒绝吞掉权限或网络错误。"""
    from botocore.exceptions import ClientError
    try:
        return call(**kwargs)
    except ClientError as exc:
        if exc.response['Error']['Code'] in {
            'ResourceNotFoundException', 'NoSuchEntity', 'NoSuchEntityException'
        }:
            return None
        raise


def require_owner(tags, state):
    """应用以远端归属标签确认资源；相似名称不能证明所有权。"""
    if isinstance(tags, list):
        tags = {item['Key']: item['Value'] for item in tags}
    if not state.get('owner_id') or tags.get(OWNER_TAG) != state['owner_id']:
        raise ValueError('远端资源归属标签不匹配；应用拒绝接管或删除资源')


def entrypoint(main):
    """入口输出明确失败原因；中断也不触发任何自动清理。"""
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit('操作者中断了脚本；应用保留 state、资源和结果')
    except Exception as exc:
        raise SystemExit(f'{type(exc).__name__}: {exc}') from exc
