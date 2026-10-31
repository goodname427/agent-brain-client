"""One reviewed exact recall through the installed HTTPSContentClient. No retry."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',required=True);parser.add_argument('--plan-sha256',required=True)
    args=parser.parse_args();path=Path(args.plan)
    if path.is_symlink() or path.stat().st_mode&0o077 or path.stat().st_uid!=os.getuid():raise ValueError('Private owned approved plan required')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=args.plan_sha256:raise ValueError('Plan pin changed')
    plan=json.loads(raw)
    required={'approved','not_before','expires_at','recall_session_id','cwd','client_root','client_manifest_sha256',
              'profile','profile_sha256','device_task','device_task_sha256','request','request_sha256'}
    if set(plan)!=required or plan['approved'] is not True or not 0<plan['expires_at']-plan['not_before']<=3600 or not plan['not_before']<=time.time()<plan['expires_at']:raise ValueError('Approved finite plan required')
    if os.environ.get('CODEX_THREAD_ID')!=plan['recall_session_id'] or str(Path.cwd().resolve())!=plan['cwd']:raise ValueError('Run in the actual independent GUI session and cwd')
    root=Path(plan['client_root']);manifest=root/'releases/manifest.json'
    if not root.is_absolute() or root.resolve()!=root or hashlib.sha256(manifest.read_bytes()).hexdigest()!=plan['client_manifest_sha256']:raise ValueError('Installed Client pin required')
    for name,digest in json.loads(manifest.read_text())['files'].items():
        target=root/name
        if target.is_symlink() or target.resolve().is_relative_to(root) is False or hashlib.sha256(target.read_bytes()).hexdigest()!=digest:raise ValueError('Installed Client bytes changed')
    sys.path[:0]=[str(root/'adapters/codex'),str(root/'scripts')]
    from https_content import HTTPSContentClient,PinnedDeviceTask,profile_file,owned,unique
    from content_contract import validate
    request_raw=owned(plan['request']).read_bytes()
    if hashlib.sha256(request_raw).hexdigest()!=plan['request_sha256']:raise ValueError('Request pin changed')
    request=json.loads(request_raw,object_pairs_hook=unique);validate('memory_query',request['params'])
    if request['params']['context']['session_id']!=plan['recall_session_id'] or 'selection' not in request['params']:raise ValueError('Exact independent selection required')
    task=PinnedDeviceTask(plan['device_task'],plan['device_task_sha256'],profile_file(plan['profile'],plan['profile_sha256']),plan['profile_sha256'])
    task.check(request,task.value['device_id'])
    marker=path.parent/(request['request_id']+'.attempted')
    with marker.open('x') as stream:stream.write(str(time.time())+'\n')
    marker.chmod(0o600)
    client=HTTPSContentClient(plan['profile'],plan['profile_sha256'],device_task=(plan['device_task'],plan['device_task_sha256']))
    result=client.request(request)  # Existing credential custody + authenticated /v1/requests.
    print(json.dumps(result,ensure_ascii=False))
    return 2 if 'error' in result else 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError,KeyError,TypeError):
        print(json.dumps({'status':'exact-recall-stopped','retryable':False}));raise SystemExit(2)
