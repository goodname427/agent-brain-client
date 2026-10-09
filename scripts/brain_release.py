"""Isolated release engine: fixed source, immutable packages, trusted local migrations.

No daemon, model integration, credentials, arbitrary remote scripts or provisioning.
The adapter supplies scoped snapshot/refresh/restore/health callbacks. Deployment
must install one trusted startup entry and approve its source/channel/scope once.
"""
from contextlib import contextmanager
import hashlib
import inspect
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import uuid

from brain_lock import locked


class UpdateBlocked(RuntimeError):
    pass


def version(v):
    if not isinstance(v,str) or not re.fullmatch(r'\d+\.\d+\.\d+',v):
        raise UpdateBlocked('Invalid release version')
    return tuple(map(int,v.split('.')))


def digest(value):
    return hashlib.sha256(value).hexdigest()


def migration_hash(handler):
    return digest(inspect.getsource(handler).encode('utf-8'))


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    with temp.open('xb') as out:
        out.write((json.dumps(value,sort_keys=True,ensure_ascii=False)+'\n').encode());out.flush();os.fsync(out.fileno())
    os.replace(temp,path)


class GitReleaseSource:
    """Fetch a pinned authorized repository/ref without changing the data checkout."""
    def __init__(self,repository,policy,timeout=30):
        self.repository=Path(repository);self.policy=policy;self.timeout=timeout

    def git(self,*args):
        result=subprocess.run(['git','-C',str(self.repository),*args],capture_output=True,
                              stdin=subprocess.DEVNULL,timeout=self.timeout)
        if result.returncode:raise OSError('Release source unavailable; no credentials displayed')
        return result.stdout

    def latest(self):
        actual=self.git('remote','get-url','origin').decode().strip()
        if actual!=self.policy['origin']:raise UpdateBlocked('Release origin changed; review required')
        if re.search(r'https?://[^/]*@',actual):raise UpdateBlocked('Embedded credential URL rejected')
        ref=self.policy['ref']
        if not re.fullmatch(r'refs/heads/[a-zA-Z0-9][a-zA-Z0-9._/-]*',ref):raise UpdateBlocked('Invalid approved ref')
        self.git('fetch','--no-tags','origin',ref)
        commit=self.git('rev-parse','FETCH_HEAD').decode().strip()
        if not re.fullmatch(r'[a-f0-9]{40}',commit):raise UpdateBlocked('Invalid source object')
        anchor=self.policy['anchor_commit']
        if not re.fullmatch(r'[a-f0-9]{40}',anchor):raise UpdateBlocked('Invalid source anchor')
        try:self.git('merge-base','--is-ancestor',anchor,commit)
        except OSError:raise UpdateBlocked('Release ancestry changed; review required') from None
        raw=self.git('show',commit+':releases/manifest.json')
        if len(raw)>65536:raise UpdateBlocked('Manifest too large')
        manifest=json.loads(raw)
        return commit,manifest,lambda path:self.git('show',commit+':'+path)


class ReleaseEngine:
    def __init__(self,local,policy,source,adapter,migrations):
        self.local=Path(local).resolve();self.policy=policy;self.source=source;self.adapter=adapter
        # Handler definitions come from the already approved local installation.
        # The manifest can select a known handler; it cannot supply code/commands.
        self.migrations=migrations
        self.pointer=self.local/'active.json';self.journal=self.local/'journal.json'

    def active(self):
        if not self.pointer.exists():return None
        value=json.loads(self.pointer.read_text(encoding='utf-8'))
        if set(value)!={'commit','version','package','manifest_sha256'} or not re.fullmatch(r'[a-f0-9]{40}',value['commit']):
            raise UpdateBlocked('Invalid active runtime pointer')
        version(value['version'])
        expected=self.local/'packages'/(value['version']+'-'+value['commit'])
        if Path(value['package'])!=expected or expected.is_symlink():raise UpdateBlocked('Runtime pointer escaped cache')
        receipt=json.loads((expected/'verified.json').read_text(encoding='utf-8'))
        if receipt['commit']!=value['commit'] or receipt['manifest']['version']!=value['version']:
            raise UpdateBlocked('Runtime receipt changed')
        if digest(json.dumps(receipt['manifest'],sort_keys=True).encode())!=value['manifest_sha256']:
            raise UpdateBlocked('Active manifest integrity failed')
        self.validate(receipt['manifest'],None)
        for path,sha in receipt['manifest']['files'].items():
            target=expected/path
            if target.is_symlink() or digest(target.read_bytes())!=sha:raise UpdateBlocked('Active package integrity failed')
        return value

    def validate(self,manifest,current):
        keys={'schema','version','channel','core_api','state_schema','python_min','capabilities','files','migrations'}
        if not isinstance(manifest,dict) or set(manifest)!=keys or manifest['schema']!=1:
            raise UpdateBlocked('Unknown release manifest')
        target=version(manifest['version'])
        if current and target<version(current['version']):raise UpdateBlocked('Downgrade rejected')
        if self.policy.get('pin') and manifest['version']!=self.policy['pin']:raise UpdateBlocked('Version pin retained')
        if manifest['channel']!=self.policy['channel']:raise UpdateBlocked('Channel mismatch')
        if manifest['core_api']!=self.policy['core_api'] or manifest['state_schema']!=self.policy['state_schema']:
            raise UpdateBlocked('Protocol/schema change requires review')
        if sorted(manifest['capabilities'])!=sorted(self.policy['capabilities']):
            raise UpdateBlocked('Permission scope change requires review')
        if tuple(map(int,manifest['python_min'].split('.')))>sys.version_info[:3]:
            raise UpdateBlocked('Python dependency unavailable; no installer executed')
        files=manifest['files']
        if not isinstance(files,dict) or not 1<=len(files)<=250:raise UpdateBlocked('Invalid release files')
        for path,sha in files.items():
            p=PurePosixPath(path)
            if p.is_absolute() or '..' in p.parts or '\\' in path or not re.fullmatch(r'[a-f0-9]{64}',sha):
                raise UpdateBlocked('Unsafe release path/hash')
            if not any(path.startswith(prefix) for prefix in self.policy['file_prefixes']):
                raise UpdateBlocked('File outside approved runtime scope')
        for step in manifest['migrations']:
            if set(step)!={'id','sha256'} or step['id'] not in self.migrations:
                raise UpdateBlocked('Unknown migration; explicit review required')
            code_hash,handler=self.migrations[step['id']]
            if step['sha256']!=code_hash or migration_hash(handler)!=code_hash:
                raise UpdateBlocked('Migration code changed; review required')

    def prepare(self,commit,manifest,read):
        release=self.local/'packages'/(manifest['version']+'-'+commit)
        release.mkdir(parents=True,exist_ok=True)
        total=0
        for name,expected in manifest['files'].items():
            path=release/name
            if not release.resolve().is_relative_to(self.local.resolve()) or not path.resolve().is_relative_to(release.resolve()) or any(p.is_symlink() for p in [path,*path.parents] if p.is_relative_to(release)):
                raise UpdateBlocked('Release cache symlink rejected')
            # Retain already verified chunks after interruption.
            if path.is_file() and not path.is_symlink():
                size=path.stat().st_size
                if size>2*1024*1024:raise UpdateBlocked('Cached release file exceeds size limit')
                if digest(path.read_bytes())==expected:
                    total+=size
                    if total>20*1024*1024:raise UpdateBlocked('Cached release exceeds total size limit')
                    continue
            data=read(name);total+=len(data)
            if len(data)>2*1024*1024 or total>20*1024*1024 or digest(data)!=expected:
                raise UpdateBlocked('Release integrity/size check failed')
            path.parent.mkdir(parents=True,exist_ok=True)
            temp=path.with_name(path.name+'.pending');temp.write_bytes(data);os.replace(temp,path)
        for name,expected in manifest['files'].items():
            path=release/name
            if path.is_symlink() or digest(path.read_bytes())!=expected:raise UpdateBlocked('Prepared package changed')
        atomic_json(release/'verified.json',{'commit':commit,'manifest':manifest})
        return release

    def check(self):
        self.local.mkdir(parents=True,exist_ok=True)
        with locked(self.local/'update.lock'), locked(self.local.parent/'python-write.lock'):
            # An interrupted target is not yet healthy. Restore its predecessor
            # before validating the active bytes, which may themselves be damaged.
            if self.journal.exists():
                old=json.loads(self.journal.read_text(encoding='utf-8'))
                if old['phase'] in ('applying','activated'):
                    self.adapter.restore(old['backup']);self.set_active(old['before'])
                    atomic_json(self.journal,dict(old,phase='rolled-back-interrupted'))
            current=self.active()
            try:commit,manifest,read=self.source.latest()
            except OSError:return {'status':'offline','active':self.active()}
            self.validate(manifest,self.active())
            if current and current['commit']==commit:return {'status':'current','active':current}
            release=self.prepare(commit,manifest,read)
            # Adapter verifies protocol/path/contracts and dependencies before any mutation.
            self.adapter.preflight(release,manifest)
            backup=self.adapter.snapshot()
            backup_raw=json.dumps(backup,sort_keys=True).encode()
            backup_path=self.local/'backups'/(commit+'.json')
            atomic_json(backup_path,{'snapshot':backup,'sha256':digest(backup_raw)})
            saved=json.loads(backup_path.read_text(encoding='utf-8'))
            if digest(json.dumps(saved['snapshot'],sort_keys=True).encode())!=saved['sha256']:
                raise UpdateBlocked('Backup verification failed')
            before=self.active();next_active={'commit':commit,'version':manifest['version'],'package':str(release),
                                             'manifest_sha256':digest(json.dumps(manifest,sort_keys=True).encode())}
            journal={'phase':'applying','before':before,'target':next_active,'backup':backup,'completed':[]}
            atomic_json(self.journal,journal)
            try:
                for step in manifest['migrations']:
                    self.migrations[step['id']][1](release,manifest)
                    journal['completed'].append(step['id']);atomic_json(self.journal,journal)
                self.adapter.refresh(release,manifest)
                self.set_active(next_active);journal['phase']='activated';atomic_json(self.journal,journal)
                self.adapter.health(release,manifest)
                journal['phase']='complete';atomic_json(self.journal,journal)
                return {'status':'updated','active':next_active}
            except Exception:
                self.adapter.restore(backup);self.set_active(before)
                journal['phase']='rolled-back';atomic_json(self.journal,journal)
                raise

    def set_active(self,value):
        if value is None:self.pointer.unlink(missing_ok=True)
        else:atomic_json(self.pointer,value)
