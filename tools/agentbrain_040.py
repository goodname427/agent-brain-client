"""Local 0.4 build/install/update entry, using the existing Client transactions.

No publishing, new credentials, host-policy changes or implicit capture enablement.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tools'),str(ROOT/'adapters/codex'),str(ROOT/'scripts')]


def dispatch(args):
    if args.action=='build':
        from build_client import release
        value=release(ROOT,args.output,args.version,'personal-content-https')
        return {'status':'client-candidate-built','version':value['version'],
                'published':False,'installed':False,'output':str(Path(args.output).resolve())}
    if args.action=='plan':
        from content_install import prepare,save,safe
        import content_contract
        value=prepare(args.device_home,args.package,args.release_policy,args.release_policy_sha256,args.replace_existing)
        path=safe(args.output)
        if path.exists():raise ValueError('Existing plan preserved')
        save(path,value)
        return {'status':'install-plan-prepared','plan':str(path),'plan_sha256':hashlib.sha256(content_contract.encode(value)).hexdigest(),'installed':False,'native_changed':False}
    if args.action=='apply':
        from content_install import apply,inspect_plan
        from content_mcp import pinned
        inspect_plan(args.plan_file,args.plan_sha256)
        return apply(pinned(args.plan_file,args.plan_sha256),args.plan_sha256)
    if args.action=='status':
        from content_install import inspect
        return inspect(args.device_home)
    if args.action in ('check-update','update'):
        from mac_first_install import manage_update
        return manage_update(Path(args.device_home).resolve(),args.action)
    # Preserve the finite policy/request pin checks and one-call transport path.
    from https_content import main
    forwarded=[str(ROOT/'adapters/codex/https_content.py'),args.action]
    for field in ('https_profile','https_profile_sha256','device_task_policy','device_task_policy_sha256','task_request_file','task_request_sha256'):
        value=getattr(args,field)
        if value is not None:forwarded+=['--'+field.replace('_','-'),value]
    previous=sys.argv
    try:sys.argv=forwarded;return main()
    finally:sys.argv=previous


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('build','plan','apply','status','check-update','update','task-check','task-execute'))
    p.add_argument('--device-home',default=str(Path.home()))
    p.add_argument('--output');p.add_argument('--version',default='0.4.4')
    for field in ('package','release-policy','release-policy-sha256','plan-file','plan-sha256',
                  'https-profile','https-profile-sha256','device-task-policy','device-task-policy-sha256',
                  'task-request-file','task-request-sha256'):
        p.add_argument('--'+field)
    p.add_argument('--replace-existing',action='store_true')
    args=p.parse_args(argv)
    required={'build':('output',),'plan':('package','release_policy','release_policy_sha256','output'),
              'apply':('plan_file','plan_sha256')}
    for field in required.get(args.action,()):
        if not getattr(args,field):p.error('--'+field.replace('_','-')+' is required')
    if args.action in ('check-update','update') and any(getattr(args,key) for key in ('package','release_policy','release_policy_sha256','https_profile','https_profile_sha256')):
        p.error('Updates reuse the existing registration and approved policy; replacement profiles are not accepted')
    try:
        result=dispatch(args)
        if isinstance(result,int):return result
        print(json.dumps(result,ensure_ascii=False))
        if args.action in ('check-update','update'):
            from mac_first_install import lifecycle_exit
            return lifecycle_exit(args.action,result)
        if args.action=='apply':return 0 if result.get('installed') is True else 2
        return 0 if not result.get('error') and result.get('status') not in ('content-native-install-review-required','client-update-review-required','client-update-unavailable') else 2
    except Exception:
        print(json.dumps({'status':'client-operation-review-required','operation':args.action,
                          'native_state':'unconfirmed-review-required','credentials_created':False}))
        return 2


if __name__=='__main__':raise SystemExit(main())
