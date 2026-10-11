"""Fixed approved SSH MCP startup: check pinned client releases, then serve.

Deploy this bootstrap and its dependencies in a reviewed fixed directory; pass
the policy digest through the forced SSH command. No daemon or native rewriting.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
from client_release import ClientReleases,CONTENT_CAPABILITIES,SSH_CONTENT_CAPABILITIES,LEGACY_PRESERVATION,version

OUTBOX_MIN_VERSION='0.2.1'


def content_runtime_blocker(active,policy):
    """Old content runtimes cannot interpret the new private local ledgers.

    Check existence only: retain even empty, damaged or linked state as evidence.
    This guard belongs to the reviewed fixed bootstrap, outside rollback packages.
    """
    if policy['capabilities'] not in (CONTENT_CAPABILITIES,SSH_CONTENT_CAPABILITIES) or version(active['version'])>=version(OUTBOX_MIN_VERSION):return None
    cache=Path(policy['runtime']['content_cache']);found=False
    try:
        for name in ('outbox','imports.json'):
            try:(cache/name).lstat()
            except FileNotFoundError:continue
            found=True;break
    except OSError:
        return {'status':'content-local-state-unverified','served':False,'native_changed':False,
                'required_client_version':OUTBOX_MIN_VERSION,
                'next_action':'Local content state could not be checked. Preserve the content cache and restore access or an outbox-compatible verified client before serving.'}
    if found:
        return {'status':'content-outbox-runtime-incompatible','served':False,'native_changed':False,
                'required_client_version':OUTBOX_MIN_VERSION,
                'next_action':'Local outbox or import inventory is retained, but the active client cannot safely handle it. Restore a verified client version 0.2.1 or later; do not delete local state to enable the old runtime.'}
    return None


def startup(policy_path, policy_sha256, check_only=False, installation_check=False):
    releases=ClientReleases(policy_path,policy_sha256)
    result=releases.update()
    active=result.get('active')
    if not active:
        if check_only:return result
        return {'status':result['status'],'served':False,'next_action':'A verified compatible client is required before serving MCP.'}
    # ReleaseEngine.active rechecks every package hash before execution.
    active=releases.verified_active();runtime=releases.policy['runtime']
    blocked=content_runtime_blocker(active,releases.policy)
    if blocked:return dict(result,**blocked)
    if check_only:return result
    if releases.policy['capabilities'] in (CONTENT_CAPABILITIES,SSH_CONTENT_CAPABILITIES):
        if not installation_check:
            from content_install import inspect
            if not inspect(runtime['device_home']).get('installed'):return {'status':'content-native-install-pending','served':False,'next_action':'Complete or recover the exact native installation transaction before mapping cloud content.'}
        command=[sys.executable,'-I','-B',str(Path(active['package'])/'adapters/codex/content_mcp.py'),'--client-version',active['version']]
        for name,value in runtime.items():
            if name not in LEGACY_PRESERVATION:command.extend(['--'+name.replace('_','-').replace('content-cache','cache'),value])
        if installation_check:command.append('--installation-check')
        return {'status':result['status'],'served':True,'exit_code':subprocess.run(command).returncode}
    if installation_check:return {'status':'content-runtime-review-required','served':False}
    command=[sys.executable,'-I','-B',str(Path(active['package'])/'adapters/codex/remote_mcp.py'),
             '--endpoint',runtime['endpoint'],'--token-file',runtime['token_file'],'--client-version',active['version'],
             '--client-platform',releases.policy['platform']]
    if 'personal-memory-explicit' in releases.policy['capabilities']:command.extend(['--data-mode','personal'])
    if result['status'] not in ('current','updated'):
        print(json.dumps({'client_update':result['status'],'serving':'previous verified package'}),file=sys.stderr)
    return {'status':result['status'],'served':True,'exit_code':subprocess.run(command).returncode}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy-file',type=Path,required=True)
    parser.add_argument('--policy-sha256',required=True)
    parser.add_argument('--check-only',action='store_true')
    parser.add_argument('--installation-check',action='store_true',help='Read-only content protocol/authentication check; no native mapping.')
    args=parser.parse_args()
    try:result=startup(args.policy_file,args.policy_sha256,args.check_only,args.installation_check)
    except Exception:
        result={'status':'startup-review-required','served':False,'next_action':'Inspect the approved fixed bootstrap, policy digest and isolated client paths; preserve existing packages.'}
    if args.check_only:print(json.dumps(result))
    elif not result.get('served'):print(json.dumps(result),file=sys.stderr)
    checked=args.check_only and result['status'] in ('current','updated','offline') and bool(result.get('active'))
    return result.get('exit_code',0 if checked else 2)


if __name__=='__main__':sys.exit(main())
