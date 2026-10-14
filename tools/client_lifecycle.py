"""Owned Mac/Codex removal; keep identity, immutable runtimes and recovery data."""
import base64
import copy
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace

import content_install as install
from content_cache import ContentAdapter, BEGIN, END, BLOCK
from content_mcp import pinned
from brain_lock import locked, require_platform, private_mode
from client_release import HTTPS_CONTENT_CAPABILITIES,CHECK_UPDATE_COMMAND,UPDATE_COMMAND


def state_path(home):
    return install.safe(Path(home) / '.config/agent-brain-client')


def journal_path(home):
    return state_path(home) / 'uninstall-receipt.json'


def read_removal(home):
    path = journal_path(home)
    private_mode(install.safe(path), owner=True, blocked=install.InstallBlocked)
    return json.loads(install.raw(path, 3 * 1048576), object_pairs_hook=install.pairs)


def lease(path):
    path = install.safe(path)
    parent = path.parent.stat()
    if not path.parent.is_dir() or parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise install.InstallBlocked('Non-writable owned lock directory required')
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except FileExistsError:
        pass
    # Existing nonsecret 0.2.5 lock files can be 0644 inside a private directory.
    # Keep their inode/mode; never follow a link or reset the kernel lease.
    stat = path.stat()
    if not path.is_file() or stat.st_uid != os.getuid() or stat.st_nlink != 1:
        raise install.InstallBlocked('Owned lock required')
    return locked(path, timeout=0)


def adapter_for(home, meta, config_image=None):
    policy, mcp, _, native = install.registered_context(home, meta, config_image)
    if policy['capabilities'] != HTTPS_CONTENT_CAPABILITIES:
        raise install.InstallBlocked('Only the existing HTTPS Mac installation is supported')
    runtime = policy['runtime']
    content = pinned(runtime['content_policy'], runtime['content_policy_sha256'])
    # Read-only filesystem view: no credential access, transport or constructor writes.
    adapter = object.__new__(ContentAdapter)
    adapter.home = Path(home)
    adapter.native_home = native
    adapter.cache = install.safe(runtime['content_cache'])
    adapter.platform = policy['platform']
    adapter.policy = content
    adapter.binding = {'device_home': str(home), 'codex_home': str(native)}
    adapter.policy_sha = install.sha(install.contract.encode({'policy': content, 'binding': adapter.binding}))
    adapter.client = SimpleNamespace(grant={'scopes': ['read', 'put'], 'artifact_types': content['kinds']})
    return adapter, mcp, native


def queue_counts(adapter):
    counts = dict.fromkeys(('pending', 'confirmed', 'conflict', 'blocked', 'reconciled'), 0)
    directory = adapter.cache / 'outbox'
    adapter._safe(directory, adapter.cache)
    rows = list(directory.iterdir()) if directory.exists() else []
    if directory.exists():
        private_mode(directory, directory=True, owner=True, blocked=install.InstallBlocked)
    if len(rows) > 2048:
        raise install.InstallBlocked('Queue inventory requires recovery')
    binding = None
    for path in rows:
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,128}\.json', path.name):
            raise install.InstallBlocked('Unknown queue item retained')
        row = adapter._load('outbox/' + path.name)
        keys = {'schema', 'policy_sha', 'created', 'request', 'request_sha', 'state', 'response', 'remote', 'reconciles', 'resolution_request_id'}
        if not isinstance(row, dict) or set(row) != keys or row['schema'] != 1 or row['state'] not in counts or not re.fullmatch(r'[a-f0-9]{64}', row['policy_sha']):
            raise install.InstallBlocked('Unknown queue state retained')
        adapter._check_write(row['request'])
        if path.stem != row['request']['request_id'] or install.sha(install.contract.encode(row['request'])) != row['request_sha']:
            raise install.InstallBlocked('Queue checksum changed')
        if binding is not None and binding != row['policy_sha']:
            raise install.InstallBlocked('Mixed queue identities retained')
        binding = row['policy_sha']
        counts[row['state']] += 1
    imports = adapter._load('imports.json')
    if imports is not None:
        if not isinstance(imports, dict) or set(imports) != {'schema', 'policy_sha', 'items', 'coverage'} or imports['schema'] != 1 or not isinstance(imports['items'], dict):
            raise install.InstallBlocked('Import inventory retained')
        for item in imports['items'].values():
            phase = item.get('status') if isinstance(item, dict) else None
            if phase not in ('inventoried', 'pending', 'imported', 'duplicate', 'conflict', 'blocked', 'reconciled'):
                raise install.InstallBlocked('Unknown import retained')
            if phase in ('inventoried', 'pending', 'conflict', 'blocked'):
                raise install.InstallBlocked('Unfinished import retained; recover using the existing MCP')
    return counts


def remove_table(raw):
    text = (raw or b'').decode()
    parsed = install.tomllib.loads(text)
    if install.NAME not in parsed.get('mcp_servers', {}):
        raise install.InstallBlocked('Owned table missing')
    pattern = re.compile(r'^\[mcp_servers\.' + install.NAME + r'\][ \t]*(?:#.*)?\r?\n', re.M)
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise install.InstallBlocked('Owned table syntax changed')
    start, end = matches[0].span()
    following = re.search(r'^\[', text[end:], re.M)
    desired = (text[:start] + text[end + following.start():] if following else text[:start]).encode()
    expected = copy.deepcopy(parsed)
    del expected['mcp_servers'][install.NAME]
    actual = install.tomllib.loads(desired.decode())
    if not actual.get('mcp_servers') and not expected['mcp_servers']:
        actual.pop('mcp_servers', None)
        expected.pop('mcp_servers', None)
    if actual != expected:
        raise install.InstallBlocked('Unrelated TOML changed')
    return desired


def prepare_removal(home):
    require_platform('native-install')
    home = install.safe(home)
    inspected=install.inspect(home)
    if not inspected.get('registration_verified',inspected.get('installed')):
        raise install.InstallBlocked('Installed ownership requires repair')
    registration = state_path(home) / 'codex-mcp.json'
    meta = install.private_json(registration)
    adapter, _, native = adapter_for(home, meta)
    counts = queue_counts(adapter)
    if any(counts[s] for s in ('pending', 'conflict', 'blocked')):
        return {'status': 'uninstall-unconfirmed-writes', 'installed': True, 'native_changed': False,
                'queue': counts, 'next_action': 'Keep the installation. Retry the original request IDs or resolve conflicts through MCP, then rerun uninstall.'}
    transaction = adapter._load('transaction.json')
    if transaction and transaction.get('phase') not in ('complete', 'rolled-back'):
        raise install.InstallBlocked('Mapping transaction needs repair')
    owned = adapter._owned()
    payload = {native / 'config.toml': remove_table(install.raw(native / 'config.toml')), registration: None}
    for name in owned['files']:
        path = adapter._path(name)
        if name == '.codex/AGENTS.md':
            text = adapter._raw(name).decode()
            if text.count(BEGIN) != 1 or text.count(END) != 1 or len(BLOCK.findall(text)) != 1:
                raise install.InstallBlocked('Managed instructions changed')
            payload[path] = BLOCK.sub('', text).encode()
        else:
            payload[path] = None
    payload[adapter.cache / 'owned.json'] = install.contract.encode(dict(owned, files={}))
    # The 0.2.5 refresh fast path trusts active.json. Remove its rebuildable
    # pointer so reinstall remaps the retained snapshot even at the same commit.
    payload[adapter.cache / 'active.json'] = None
    before = {}
    for path in payload:
        value = install.raw(path, 1048576)
        before[str(path)] = {'data': base64.b64encode(value).decode() if value is not None else None,
                             'sha': install.digest_or_none(value), 'mode': path.stat().st_mode & 0o777 if value is not None else 0o600}
    return {'schema': 1, 'phase': 'prepared', 'device_home': str(home), 'registration': meta,
            'before': before, 'after': {str(p): base64.b64encode(v).decode() if v is not None else None for p, v in payload.items()},
            'queue': counts, 'removed_mapping_files': len(owned['files'])}


def validate_journal(home, journal):
    if set(journal) != {'schema', 'phase', 'device_home', 'registration', 'before', 'after', 'queue', 'removed_mapping_files'} or journal['schema'] != 1 or journal['device_home'] != str(home) or journal['phase'] not in ('prepared', 'applying', 'complete', 'reinstalled'):
        raise install.InstallBlocked('Removal receipt changed')
    metadata=journal['registration']
    policy=install.policy_file(metadata['release_policy'],metadata['release_policy_sha256'],runtime_check=False)
    config=Path(policy['runtime']['codex_home'])/'config.toml'
    original_config=decode(journal['before'][str(config)]['data'])
    adapter, mcp, native = adapter_for(home, metadata, original_config)
    scope = {native / 'config.toml', state_path(home) / 'codex-mcp.json', adapter.cache / 'owned.json', adapter.cache / 'active.json'}
    if set(journal['before']) != set(journal['after']):
        raise install.InstallBlocked('Removal receipt scope changed')
    for name in journal['before']:
        path = install.safe(name)
        if path not in scope:
            relative = '.codex/AGENTS.md' if path == native / 'AGENTS.md' else path.relative_to(home).as_posix()
            if adapter._path(relative) != path:
                raise install.InstallBlocked('Removal path changed')
        old = journal['before'][name]
        if set(old) != {'data', 'sha', 'mode'} or type(old['mode']) is not int or old['mode'] & ~0o777:
            raise install.InstallBlocked('Removal image changed')
        if install.digest_or_none(decode(old['data'])) != old['sha']:
            raise install.InstallBlocked('Removal checksum changed')
    if not scope <= {Path(p) for p in journal['before']}:
        raise install.InstallBlocked('Removal receipt incomplete')
    if journal['after'][str(adapter.cache / 'active.json')] is not None:
        raise install.InstallBlocked('Removal snapshot pointer changed')
    registration = state_path(home) / 'codex-mcp.json'
    if decode(journal['before'][str(registration)]['data']) != install.contract.encode(journal['registration']) or journal['after'][str(registration)] is not None:
        raise install.InstallBlocked('Removal registration image changed')
    config = native / 'config.toml'
    config_before = decode(journal['before'][str(config)]['data'])
    if install.tomllib.loads(config_before.decode()).get('mcp_servers', {}).get(install.NAME) != mcp or decode(journal['after'][str(config)]) != remove_table(config_before):
        raise install.InstallBlocked('Removal table image changed')
    owned_path = adapter.cache / 'owned.json'
    original_owned = decode(journal['before'][str(owned_path)]['data'])
    owned = json.loads(original_owned) if original_owned is not None else {'policy_sha': adapter.policy_sha, 'binding': adapter.binding, 'files': {}}
    if set(owned) != {'policy_sha', 'binding', 'files'} or owned['policy_sha'] != adapter.policy_sha or owned['binding'] != adapter.binding or not isinstance(owned['files'], dict):
        raise install.InstallBlocked('Removal mapping identity changed')
    paths = {adapter._path(name) for name in owned['files']}
    if {Path(p) for p in journal['before']} != scope | paths or journal['removed_mapping_files'] != len(paths) or decode(journal['after'][str(owned_path)]) != install.contract.encode(dict(owned, files={})):
        raise install.InstallBlocked('Removal mapping scope changed')
    for name, digest in owned['files'].items():
        path = str(adapter._path(name))
        before = decode(journal['before'][path]['data'])
        if install.sha(before) != digest:
            raise install.InstallBlocked('Removal mapping image changed')
        desired = None
        if name == '.codex/AGENTS.md':
            text = before.decode()
            if text.count(BEGIN) != 1 or text.count(END) != 1 or len(BLOCK.findall(text)) != 1:
                raise install.InstallBlocked('Removal instruction image changed')
            desired = BLOCK.sub('', text).encode()
        if decode(journal['after'][path]) != desired:
            raise install.InstallBlocked('Removal mapping output changed')
    if len(install.contract.encode(journal)) > 3 * 1048576:
        raise install.InstallBlocked('Removal receipt exceeds bound')
    return adapter


def decode(value):
    return base64.b64decode(value, validate=True) if value is not None else None


def validate_uninstalled_state(home, journal, *, allow_unrelated_config_edits=False,
                               resume_plan=None, resume_receipt=None,
                               require_owned_postimages=True):
    """Validate the whole completed removal before starting a fresh reinstall.

    A user may edit unrelated Codex settings after removal. In that case the
    configuration must still parse and the Agent Brain table must still be
    absent. Every other recorded removal postimage remains byte-pinned.
    """
    validate_journal(home, journal)
    config_paths = [Path(name) for name in journal['before']
                    if Path(name).name == 'config.toml']
    if len(config_paths) != 1:
        raise install.InstallBlocked('Removal config scope changed')
    config = config_paths[0]
    resume_paths = set()
    if resume_plan is not None:
        _, _, scoped = install.plan_scope(resume_plan)
        resume_paths = scoped
        if resume_receipt is not None:
            install.validate_recovery(resume_receipt, resume_plan)
    changed = []
    for name, after in journal['after'].items():
        path = install.safe(name)
        current = install.raw(path, 1048576)
        if install.digest_or_none(current) == install.digest_or_none(decode(after)):
            continue
        changed.append(name)
        if path in resume_paths:
            if resume_receipt is None and install.digest_or_none(current) != resume_plan['before'].get(str(path)):
                raise install.InstallBlocked('Reinstall preimage changed')
            continue
        if path != config and require_owned_postimages:
            raise install.InstallBlocked('Uninstalled state changed')
        if path != config:
            continue
        if resume_plan is not None:
            before_sha = resume_plan['before'].get(str(config))
            if (install.digest_or_none(current) not in
                    (before_sha, resume_plan['config_after_sha256'])):
                raise install.InstallBlocked('Reinstall config recovery drift')
            continue
        if not allow_unrelated_config_edits:
            continue
        try:
            parsed = install.tomllib.loads((current or b'').decode())
        except Exception as error:
            raise install.InstallBlocked('Uninstalled config is invalid') from error
        if install.NAME in parsed.get('mcp_servers', {}):
            raise install.InstallBlocked('Agent Brain MCP registration conflict')
    return changed


def finish_removal(home, journal):
    validate_journal(home, journal)
    # All preimages checked before any write; interrupted runs accept only own images.
    for name, old in journal['before'].items():
        if install.digest_or_none(install.raw(name, 1048576)) not in (old['sha'], install.digest_or_none(decode(journal['after'][name]))):
            raise install.InstallBlocked('Outside edit retained')
    journal['phase'] = 'applying'
    install.save(journal_path(home), journal)
    for name, value in journal['after'].items():
        path = install.safe(name)
        desired = decode(value)
        old=journal['before'][name]
        if install.digest_or_none(install.raw(path,1048576)) not in (old['sha'],install.digest_or_none(desired)):
            raise install.InstallBlocked('Outside edit retained')
        if desired is None:
            path.unlink(missing_ok=True)
        else:
            install.atomic(path, desired, journal['before'][name]['mode'])
    journal['phase'] = 'complete'
    install.save(journal_path(home), journal)
    # Empty skill directories no longer expose an installed skill. Never recurse-delete.
    for name in journal['before']:
        path = Path(name)
        if '.agents/skills' in str(path):
            for parent in path.parents:
                if parent == home / '.agents/skills':
                    break
                try:
                    parent.rmdir()
                except OSError:
                    break
    return {'status': 'client-uninstalled', 'installed': False, 'native_changed': True,
            'removed_mapping_files': journal['removed_mapping_files'], 'queue': journal['queue'],
            'retained': ['identity-in-login-Keychain', 'fixed-helper-and-bootstrap', 'profiles-and-install-receipts', 'outbox-imports-and-snapshots'],
            'next_action': 'Reinstall with the same connection using the install command; no new invitation. Codex and Server data remain intact.'}


def uninstall(home, confirm):
    require_platform('native-install')
    home = install.safe(home)
    state = state_path(home)
    registration = state / 'codex-mcp.json'
    if not registration.exists() and not journal_path(home).exists():
        return {'status': 'client-not-installed', 'installed': False, 'native_changed': False}
    with lease(state / 'content-install.lock'):
        previous = read_removal(home) if journal_path(home).exists() else None
        continuing = previous and previous['phase'] in ('applying', 'complete')
        if previous and previous['phase'] == 'complete' and not registration.exists():
            validate_journal(home, previous)
            for name, value in previous['after'].items():
                if install.digest_or_none(install.raw(name, 1048576)) != install.digest_or_none(decode(value)):
                    raise install.InstallBlocked('Uninstalled files changed')
            return {'status': 'client-uninstalled', 'installed': False, 'native_changed': False}
        if continuing:
            assert previous is not None
            meta = previous['registration']
        else:
            meta = install.private_json(registration)
        if continuing:
            assert previous is not None
            adapter=validate_journal(home,previous)
        else:
            adapter, _, _ = adapter_for(home, meta)
        adapter.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        private_mode(adapter.cache, directory=True, owner=True, blocked=install.InstallBlocked)
        with lease(adapter.cache / '.content-runtime.lock'), lease(adapter.cache / 'content.lock'):
            if continuing:
                assert previous is not None
                journal = previous
            else:
                journal = prepare_removal(home)
            if journal.get('status'):
                return journal
            if not continuing and confirm('Remove only Agent Brain MCP registration and ' + str(journal['removed_mapping_files']) + ' owned mappings. Keep recoverable data, helper and Keychain identity. Codex/Server data are unchanged.') != 'Continue':
                return {'status': 'uninstall-cancelled', 'installed': True, 'native_changed': False}
            return finish_removal(home, journal)


def status(home):
    require_platform('native-install')
    state = state_path(home)
    registration = state / 'codex-mcp.json'
    removal = journal_path(home)
    journal = None
    if removal.exists() or removal.is_symlink():
        review = {'status': 'uninstall-state-review-required', 'installed': False,
                  'native_changed': False, 'uninstall_verified': False,
                  'registration_present': registration.exists() or registration.is_symlink(),
                  'next_action': 'Preserve the current files, registration and removal receipt. Review the recorded uninstall state and any later edits or interrupted reinstall; this status check did not modify or clean up anything.'}
        try:
            journal = read_removal(home)
        except Exception:
            return dict(review, reason='uninstall-receipt-unverified')
        if not isinstance(journal, dict):
            return dict(review, reason='uninstall-receipt-unverified')
        if journal.get('phase') == 'complete':
            try:
                validate_journal(home, journal)
            except Exception:
                return dict(review, reason='uninstall-receipt-unverified')
            try:
                changed = validate_uninstalled_state(
                    home, journal, require_owned_postimages=False
                )
            except Exception:
                return dict(review, reason='uninstall-postimage-unverified')
            if changed:
                return dict(review, reason='uninstall-postimage-changed', changed_postimages=len(changed))
            return {'status': 'client-uninstalled', 'installed': False, 'native_changed': False,
                    'uninstall_verified': True, 'verified_postimages': len(journal['after'])}
    if not registration.exists():
        if journal is not None:
            validate_journal(home, journal)
            return {'status': 'uninstall-resume-required', 'installed': False, 'native_changed': False}
        return install.inspect(home)
    result = install.inspect(home)
    if result.get('registration_verified',result.get('installed')):
        try:
            adapter, _, _ = adapter_for(home, install.private_json(registration))
            result['queue'] = queue_counts(adapter)
            result['owned_mapping_files'] = len(adapter._owned()['files'])
        except Exception:
            return dict(result,status='content-local-state-review-required',installed=False,
                        installation_state='present-review-required',reason='local-content-state-review-required',
                        next_action='Preserve outbox, import inventory and native mappings. Review unknown local state or file ownership before uninstall; do not delete data or reset Keychain.')
        result.update(active_package_version=result['installed_version'],running_worker_version=None,
                      latest_channel_version=None,channel_checked=False,reload_required=None)
        result['next_action'] = ('Service is paused; maintenance does not re-enable it. ' if result['status']=='content-native-client-disabled' else '')+'Status is local only. Run '+CHECK_UPDATE_COMMAND+' to inspect the original trusted channel, then '+UPDATE_COMMAND+' to apply an available package. Repeating install only verifies registration. Confirm the running version from MCP client_runtime after a normal host reload; no new invitation.'
    return result


def repair(home):
    require_platform('native-install')
    path = journal_path(home)
    if path.exists() and read_removal(home)['phase'] == 'applying':
        return uninstall(home, lambda message: 'Continue')
    return status(home)
