"""Compile this repo's ordinary Keychain helper; self-check never accesses secrets."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess

ROOT=Path(__file__).resolve().parents[1]

def build(output):
    output=Path(output).resolve()
    if output.exists():raise ValueError('Existing helper preserved')
    if platform.system()!='Darwin':raise ValueError('macOS Swift build host required')
    cache=ROOT/'.local/swift-module-cache';cache.mkdir(parents=True,exist_ok=True,mode=0o700)
    output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    source=ROOT/'native/CredentialStore.swift'
    subprocess.run(['/usr/bin/swiftc','-O','-target',platform.machine().lower()+'-apple-macos13.0','-module-cache-path',str(cache),str(source),'-o',str(output)],check=True)
    output.chmod(0o700)
    run=subprocess.run([str(output),'--self-check'],capture_output=True,text=True,check=True)
    check=json.loads(run.stdout)
    return {'schema':1,'platform':platform.system()+'-'+platform.machine(),'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'helper_sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'self_check':check,'installed':False,'keychain_accessed':False,'signing_identity_created':False}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,default=ROOT/'.local/credential-helper')
    a=p.parse_args();print(json.dumps(build(a.output),sort_keys=True))
