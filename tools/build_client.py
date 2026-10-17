"""Build from this independent Client repository, never from an external Core."""
import argparse,hashlib,json,shutil,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'adapters/codex'));sys.path.insert(0,str(ROOT/'scripts'))
from client_release import CLIENT_FILES,CONTENT_CLIENT_FILES,SSH_CONTENT_CLIENT_FILES,HTTPS_CONTENT_CLIENT_FILES,CAPABILITIES,PERSONAL_CAPABILITIES,CONTENT_CAPABILITIES,SSH_CONTENT_CAPABILITIES,HTTPS_CONTENT_CAPABILITIES
from brain_release import version as validate_version
from publication_check import require_publishable
FILES={n:n for n in CLIENT_FILES}
PAIRING_FILES={n:n for n in ('adapters/codex/client_pairing.py','native/DeviceComponent.swift')}
CONTENT_FILES={n:n for n in CONTENT_CLIENT_FILES if n not in CLIENT_FILES and n not in PAIRING_FILES}

def export(source,output,pairing_candidate=False,content_candidate=False,ssh_content_candidate=False,https_content_candidate=False):
    if Path(source).resolve()!=ROOT:raise ValueError('Build only the independent Client repository; external source prohibited')
    output=Path(output).resolve()
    if output.exists():raise ValueError('Existing artifact preserved')
    if https_content_candidate and any((pairing_candidate,content_candidate,ssh_content_candidate)):raise ValueError('Separate HTTPS capability candidate required')
    if ssh_content_candidate and any((pairing_candidate,content_candidate)):raise ValueError('Separate reviewed transport capability required')
    names=HTTPS_CONTENT_CLIENT_FILES if https_content_candidate else SSH_CONTENT_CLIENT_FILES if ssh_content_candidate else CONTENT_CLIENT_FILES if content_candidate else CLIENT_FILES+('adapters/codex/client_pairing.py','native/DeviceComponent.swift') if pairing_candidate else CLIENT_FILES
    payload={}
    for name in names:
        p=ROOT/name
        if not p.is_file() or p.is_symlink():raise ValueError('Missing or linked local source')
        payload[name]=p.read_bytes()
    require_publishable(payload)
    output.mkdir(parents=True)
    for name,data in payload.items():
        p=output/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
    manifest={'schema':1,'review_status':'unpublished-candidate','files':{n:hashlib.sha256(v).hexdigest() for n,v in payload.items()},'git_history_copied':False,'core_exported':False,'native_install_wired':not any((pairing_candidate,content_candidate,ssh_content_candidate,https_content_candidate)),'actual_deployment':'not-performed-by-build','source_authority':'independent-client-repository','release_policy_scope_delta':{'added':[],'removed':[]},'production_release_policy_scope_compatibility':'unchanged-approved-file-scope'}
    if https_content_candidate:manifest.update(content='https-system-credential-uninstalled-candidate',production_release_policy_compatible=False,production_release_policy_compatibility_reason='Candidate has not been installed or deployed; production compatibility is not claimed.',credential_helper_installed=False,keychain_accessed=False)
    if ssh_content_candidate:manifest.update(content='ssh-server-credential-uninstalled-candidate',production_release_policy_compatible=False,production_release_policy_compatibility_reason='Candidate has not been installed or deployed; production compatibility is not claimed.')
    if pairing_candidate or content_candidate:manifest.update(pairing='source-only-uninstalled-unsigned-candidate',production_release_policy_compatible=False,production_release_policy_compatibility_reason='Candidate has not been installed or deployed; production compatibility is not claimed.',existing_ssh_checkpoint_available=True)
    (output/'export-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return manifest

def release(source,output,version,data_mode='synthetic'):
    modes={'synthetic':CAPABILITIES,'personal':PERSONAL_CAPABILITIES,'personal-content':CONTENT_CAPABILITIES,'personal-content-ssh':SSH_CONTENT_CAPABILITIES,'personal-content-https':HTTPS_CONTENT_CAPABILITIES}
    if data_mode not in modes:raise ValueError('Unknown reviewed data mode')
    validate_version(version)
    built=export(source,output,content_candidate=data_mode=='personal-content',ssh_content_candidate=data_mode=='personal-content-ssh',https_content_candidate=data_mode=='personal-content-https')
    manifest={'schema':1,'version':version,'channel':'stable','core_api':1,'state_schema':1,'python_min':'3.11','capabilities':modes[data_mode],'files':built['files'],'migrations':[]}
    p=Path(output)/'releases/manifest.json';p.parent.mkdir();p.write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n');return manifest

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path);parser.add_argument('--version');parser.add_argument('--mode',choices=['synthetic','personal','personal-content','personal-content-ssh','personal-content-https'],default='personal-content-https');parser.add_argument('--scan',action='store_true')
    a=parser.parse_args()
    if a.scan:
        payload={p.relative_to(ROOT).as_posix():p.read_bytes() for p in ROOT.rglob('*') if p.is_file() and not any(part in ('.git','.local','__pycache__') for part in p.relative_to(ROOT).parts)}
        print(json.dumps(require_publishable(payload)));return
    if a.output is None:parser.error('--output is required for a build')
    print(json.dumps(release(ROOT,a.output,a.version,a.mode) if a.version else export(ROOT,a.output,content_candidate=a.mode=='personal-content',ssh_content_candidate=a.mode=='personal-content-ssh',https_content_candidate=a.mode=='personal-content-https')))
if __name__=='__main__':main()
