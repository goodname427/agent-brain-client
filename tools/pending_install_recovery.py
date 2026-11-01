"""Refresh a verified, pre-mutation Windows plan through its original channel.

The origin key and enrolled device record are never read or rewritten here.
Old plans/packages remain available; native receipt recovery stays with apply.
"""
import copy
import json
from pathlib import Path

from brain_lock import WINDOWS_PLATFORMS, native_platform
from client_release import ClientReleases, dependency_environment, version
from content_contract import encode
from content_install import prepare, private_json, raw, safe, save, sha, render
from content_mcp import LOCAL_TOOLS

# Exact pre-fix platform detector shipped by runtime 0.5.0 / installer 0.5.2.
LEGACY_DETECTOR = '60a0b040b70216684c752ee7a49374a0c122dbf16f0af8e5bb74b9dea923a6f2'


def repair_crlf_preparation(home, native):
    """Migrate only exact known CRLF metadata, before any device enrollment.

    No source byte is normalized for verification. Both exact LF and CRLF
    digests are derived from the reviewed source; all old pins are archived in
    a write-ahead record before three deterministic metadata replacements.
    """
    if native_platform() not in WINDOWS_PLATFORMS:
        return
    import mac_first_install as wizard
    from https_content import profile_metadata
    from system_credentials import canonical
    state = safe(Path(home) / '.config/agent-brain-client/first-install')
    if not (state / 'binding.json').exists():
        return
    profile_path = state / 'https-profile.json'
    journal_path = state / 'crlf-recovery.json'
    if (not profile_path.exists() and not profile_path.is_symlink()
            and not journal_path.exists() and not journal_path.is_symlink()):
        # Binding precedes profile creation. Resume ordinary exact preparation;
        # there is no profile pin to migrate yet. An existing recovery journal
        # still requires its original profile/preimages and must fail closed.
        return
    profile = private_json(profile_path)
    source = (wizard.SOURCE / 'scripts/brain_windows.py').read_bytes()
    if b'\r' in source:
        raise ValueError('Exact LF checkout required')
    before_sha, after_sha = sha(source.replace(b'\n', b'\r\n')), sha(source)
    if not journal_path.exists() and profile.get('credential_backend', {}).get('source_sha256') != before_sha:
        return
    forbidden = ('pairing-attempt.json', 'install-plan.json', 'bootstrap', 'ready.json')
    root = state.parent
    if (any((state / name).exists() or (state / name).is_symlink() for name in forbidden)
            or any((root / name).exists() or (root / name).is_symlink() for name in
                   ('https', 'codex-mcp.json', 'content-install-receipt.json', 'uninstall-receipt.json'))
            or any((state / 'releases' / name).exists() for name in ('active.json', 'journal.json'))):
        # A completed metadata migration may later have enrolled normally.
        if journal_path.exists() and private_json(journal_path).get('phase') == 'complete' and profile.get('credential_backend', {}).get('source_sha256') == after_sha:
            return
        raise ValueError('Existing device or transaction requires its original recovery')
    with wizard.private_lease(state):
        if journal_path.exists():
            journal = private_json(journal_path)
            if set(journal) != {'schema', 'phase', 'before', 'after'} or journal['schema'] != 1 or journal['phase'] not in ('prepared', 'complete'):
                raise ValueError('CRLF recovery record changed')
            originals = journal['before']
        else:
            originals = {name: private_json(state / name) for name in ('binding.json', 'https-profile.json', 'release-policy.json')}
            journal = None
        if set(originals) != {'binding.json', 'https-profile.json', 'release-policy.json'}:
            raise ValueError('CRLF recovery scope changed')
        connection = wizard.connection_value(private_json(state / 'connection.json'))
        authenticated = private_json(root / 'access-bootstrap/connection.json')
        if connection != authenticated:
            raise ValueError('Authenticated Server binding changed')
        deps = private_json(state / 'dependencies.json')
        old_profile = profile_metadata(connection['endpoint'], connection['grant'],
            {'kind': 'windows-credential-manager', 'source_sha256': before_sha}, str(root / 'https'))
        old_materials = {'source_sha256': before_sha, 'dependencies': deps}
        old_binding = {'schema': 1, 'connection_sha256': sha(canonical(connection)),
            'bundle_sha256': sha(canonical(old_materials)), 'device_home': str(home),
            'codex_home': str(native), 'installation_id': wizard.installation_id(state)}
        old_policy = originals['release-policy.json']
        runtime = old_policy['runtime']
        if (originals['https-profile.json'] != old_profile or originals['binding.json'] != old_binding
                or {k: old_policy[k] for k in ('origin', 'ref', 'anchor_commit')} != connection['release']
                or old_policy['platform'] != connection['grant']['platform']
                or runtime['https_profile'] != str(profile_path)
                or runtime['https_profile_sha256'] != sha(encode(old_profile))
                or runtime['credential_source_sha256'] != before_sha
                or runtime['device_home'] != str(home) or runtime['codex_home'] != str(native)):
            raise ValueError('CRLF preparation binding changed')
        # Uses the original exact dependency policy and executable digests.
        dependency_environment(old_policy)
        new_profile = copy.deepcopy(old_profile)
        new_profile['credential_backend']['source_sha256'] = after_sha
        new_policy = copy.deepcopy(old_policy)
        new_policy['runtime'].update(credential_source_sha256=after_sha, https_profile_sha256=sha(encode(new_profile)))
        new_binding = dict(old_binding, bundle_sha256=sha(canonical(dict(old_materials, source_sha256=after_sha))))
        replacements = {'binding.json': new_binding, 'https-profile.json': new_profile, 'release-policy.json': new_policy}
        if journal is None:
            journal = {'schema': 1, 'phase': 'prepared', 'before': originals, 'after': replacements}
            save(journal_path, journal)
        elif journal['after'] != replacements:
            raise ValueError('CRLF recovery target changed')
        for name, value in replacements.items():
            if private_json(state / name) not in (originals[name], value):
                raise ValueError('CRLF preparation changed during recovery')
        for name, value in replacements.items():
            save(state / name, value)
        save(journal_path, dict(journal, phase='complete'))


def _refresh_platform_plan(path, update):
    path = safe(path)
    plan = private_json(path)
    if plan['platform'] not in WINDOWS_PLATFORMS:
        return plan
    state = Path(plan['device_home']) / '.config/agent-brain-client'
    journal_path = path.parent / 'platform-recovery.json'
    journal = private_json(journal_path) if journal_path.exists() else None
    if journal is None and sha(raw(Path(plan['package']) / 'scripts/brain_lock.py')) != LEGACY_DETECTOR:
        return plan
    # Never replace an applying, completed or rolled-back native transaction.
    if any((state / name).exists() or (state / name).is_symlink() for name in
           ('codex-mcp.json', 'content-install-receipt.json', 'uninstall-receipt.json')):
        if journal and journal.get('after') == plan and journal.get('phase') == 'complete':
            return plan
        raise ValueError('Existing native transaction requires its original recovery')
    if journal:
        if (set(journal) != {'schema', 'phase', 'before', 'after', 'attempt'} or journal['schema'] != 1
                or journal['phase'] not in ('prepared', 'complete')
                or plan not in (journal['before'], journal['after'])):
            raise ValueError('Pending platform recovery changed')
        old, new, attempt = journal['before'], journal['after'], journal['attempt']
    else:
        old = plan
        verified_pending(old)  # Exact original package/policy/native preimage.
        attempt_path = path.parent / 'pairing-attempt.json'
        attempt = private_json(attempt_path) if attempt_path.exists() else None
        if attempt is not None and attempt != {'schema': 1, 'plan_sha256': sha(encode(old))}:
            raise ValueError('Original pairing attempt changed')
        result = update()
        releases = ClientReleases(old['release_policy'], old['release_policy_sha256'])
        active = releases.verified_active()
        if result.get('status') not in ('current', 'updated') or not active or version(active['version']) < (0, 5, 3):
            raise ValueError('Fixed release unavailable through original channel')
        candidate = Path(active['package'])
        if sha(raw(candidate / 'scripts/brain_lock.py')) == LEGACY_DETECTOR:
            raise ValueError('Fixed platform detector required')
        new = prepare(old['device_home'], candidate, old['release_policy'], old['release_policy_sha256'], old['replace_existing'])
        if old != verified_pending(old):
            new = legacy_plan(new)
        journal = {'schema': 1, 'phase': 'prepared', 'before': old, 'after': new, 'attempt': attempt}
        validate_change(old, new)
        save(journal_path, journal)  # Write-ahead record precedes either local pin update.
    validate_change(old, new)
    # Revalidate both immutable trees, policy, dependencies and native preimages
    # on every retry, including an interruption between the two atomic writes.
    for item in (old, new):
        verified_pending(item)
    releases = ClientReleases(new['release_policy'], new['release_policy_sha256'])
    active = releases.verified_active()
    bootstrap_version = json.loads(raw(Path(new['package']) / 'verified.json'))['manifest']['version']
    if (not active or version(active['version']) < (0, 5, 3)
            or active['package'] != new['package'] and version(active['version']) <= version(bootstrap_version)):
        raise ValueError('Verified recovery release changed')
    # The immutable bootstrap stays pinned by the plan. Its independently
    # verified runtime pointer may advance through the same original policy
    # between retries; that does not replace the bootstrap or device profile.
    if attempt is not None and attempt != {'schema': 1, 'plan_sha256': sha(encode(old))}:
        raise ValueError('Original pairing attempt changed')
    attempt_path = path.parent / 'pairing-attempt.json'
    current_attempt = private_json(attempt_path) if attempt_path.exists() else None
    after_attempt = {'schema': 1, 'plan_sha256': sha(encode(new))} if attempt is not None else None
    if current_attempt not in (attempt, after_attempt):
        raise ValueError('Pairing attempt changed during recovery')
    if current_attempt is None and attempt is not None:
        raise ValueError('Pairing recovery record missing')
    save(path, new)
    if after_attempt is not None:
        save(attempt_path, after_attempt)
    save(journal_path, dict(journal, phase='complete'))
    return new


def validate_change(old, new):
    expected = copy.deepcopy(old)
    expected['package'] = new['package']
    expected['package_sha256'] = new['package_sha256']
    expected['mcp']['args'][2] = str(Path(new['package']) / 'adapters/codex/client_startup.py')
    expected['config_after_sha256'] = new['config_after_sha256']
    if expected != new or old['replace_existing'] or old['legacy_agents'] is not None:
        raise ValueError('Pending installation scope changed')


def legacy_plan(current):
    """Reconstruct only the exact historical server-method-only plan bytes."""
    old = copy.deepcopy(current)
    old.pop('tool_contract_version', None)
    old['mcp']['enabled_tools'] = sorted(set(current['mcp']['enabled_tools']) - LOCAL_TOOLS - {'memory_query'})
    config = raw(Path(current['codex_home']) / 'config.toml')
    old['config_after_sha256'] = sha(render(config, old['mcp'], False))
    return old


def verified_pending(plan):
    current = prepare(plan['device_home'], plan['package'], plan['release_policy'],
                      plan['release_policy_sha256'], plan['replace_existing'])
    if plan != current and (plan['replace_existing'] or plan['legacy_agents'] is not None
                            or plan != legacy_plan(current)):
        raise ValueError('Pending installation inputs changed')
    return current


def refresh_plan(path, update):
    path = safe(path)
    journal_path = path.parent / 'tools-recovery.json'
    if not journal_path.exists():
        _refresh_platform_plan(path, update)
    plan = private_json(path)
    journal = private_json(journal_path) if journal_path.exists() else None
    current = verified_pending(plan)
    if journal is None and current == plan:
        return plan
    state = Path(plan['device_home']) / '.config/agent-brain-client'
    if any((state / name).exists() or (state / name).is_symlink() for name in
           ('codex-mcp.json', 'content-install-receipt.json', 'uninstall-receipt.json')):
        raise ValueError('Native transaction requires its original recovery')
    if journal:
        if (set(journal) != {'schema', 'phase', 'before', 'after', 'attempt'}
                or journal['schema'] != 1 or journal['phase'] not in ('prepared', 'complete')
                or plan not in (journal['before'], journal['after'])):
            raise ValueError('Pending tool recovery changed')
        old, new, attempt = journal['before'], journal['after'], journal['attempt']
        if verified_pending(old) != new or verified_pending(new) != new or legacy_plan(new) != old:
            raise ValueError('Pending tool recovery scope changed')
    else:
        old, new = plan, current
        if old['replace_existing'] or old['legacy_agents'] is not None or legacy_plan(new) != old:
            raise ValueError('Pending tool recovery scope changed')
        attempt_path = path.parent / 'pairing-attempt.json'
        attempt = private_json(attempt_path) if attempt_path.exists() else None
        if attempt is not None and attempt != {'schema': 1, 'plan_sha256': sha(encode(old))}:
            raise ValueError('Original pairing attempt changed')
        journal = {'schema': 1, 'phase': 'prepared', 'before': old, 'after': new, 'attempt': attempt}
        save(journal_path, journal)
    if attempt is not None and attempt != {'schema': 1, 'plan_sha256': sha(encode(old))}:
        raise ValueError('Original pairing attempt changed')
    attempt_path = path.parent / 'pairing-attempt.json'
    current_attempt = private_json(attempt_path) if attempt_path.exists() else None
    after_attempt = {'schema': 1, 'plan_sha256': sha(encode(new))} if attempt is not None else None
    if current_attempt not in (attempt, after_attempt) or current_attempt is None and attempt is not None:
        raise ValueError('Pairing attempt changed during tool recovery')
    save(path, new)
    if after_attempt is not None:
        save(attempt_path, after_attempt)
    save(journal_path, dict(journal, phase='complete'))
    return new
