"""Synthetic local workspace markers; no host configuration or source bodies."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_brain_client.adapters.storage.origins import discover
from agent_brain_client.application.collection.routing import eligible
from agent_brain_client.domain.scopes import select


class NonGitOrigins(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.project = self.home / 'work' / 'game'
        self.project.mkdir(parents=True)
        self.child = self.project / 'Source'
        self.child.mkdir()
        self.enterContext(patch.dict(os.environ, {'HOME': str(self.home), 'P4CONFIG': '.p4config'}))
        self.enterContext(patch.object(Path, 'home', return_value=self.home))
        self.scope = {'schema': 1, 'profile_id': 'a' * 32,
                      'include_roots': [{'root': str(self.home), 'sharing': 'personal'}],
                      'exclude_roots': [], 'non_project_roots': [], 'recall_personal': True}

    def admitted(self, cwd):
        class Ports:
            approved = staticmethod(lambda value: True)
            canonical = staticmethod(lambda value: str(Path(value).resolve()))
            discover = staticmethod(discover)
        return eligible({'scope': self.scope}, str(cwd), ports=Ports())

    def test_p4_workspace_and_descendant_keep_approved_scope_unchanged(self):
        (self.project / '.p4config').write_text('synthetic metadata, never read')
        before = copy.deepcopy(self.scope)
        with patch.object(Path, 'read_text', side_effect=AssertionError('no configuration body reads')):
            for cwd in (self.project, self.child):
                local = self.admitted(cwd)
                self.assertEqual(local['project_root'], str(self.project))
                self.assertEqual(local['repository_root'], str(self.project))
        self.assertEqual(before, self.scope)

    def test_unreal_project_and_source_engine_markers(self):
        marker = self.project / 'Synthetic.uproject'
        marker.touch()
        self.assertEqual(discover(self.child)['project_root'], str(self.project))
        marker.unlink()
        engine = self.project / 'Engine'
        (engine / 'Build').mkdir(parents=True)
        (engine / 'Build' / 'Build.version').touch()
        self.assertIsNone(self.admitted(self.child))
        (engine / 'Source').mkdir()
        self.assertEqual(self.admitted(self.child)['project_root'], str(self.project))

    def test_plain_directories_home_and_siblings_stay_non_project(self):
        (self.home / '.p4config').touch()
        (self.home / 'Synthetic.uproject').touch()
        self.scope['all_projects'] = True
        for cwd in (self.home, self.project, self.child):
            self.assertIsNone(self.admitted(cwd))
        (self.project / '.p4config').touch()
        sibling = self.project.parent / 'notes'
        sibling.mkdir()
        self.assertIsNone(self.admitted(sibling))
        self.assertIsNotNone(self.admitted(self.child))

    def test_exclusions_and_narrow_include_still_apply(self):
        (self.project / '.p4config').touch()
        self.scope['exclude_roots'] = [str(self.project)]
        self.assertIsNone(self.admitted(self.child))
        self.scope['exclude_roots'] = [str(self.child)]
        self.assertIsNone(self.admitted(self.child))
        self.assertIsNotNone(self.admitted(self.project))
        self.scope['exclude_roots'] = []
        self.scope['include_roots'] = [{'root': str(self.child), 'sharing': 'personal'}]
        self.assertIsNone(self.admitted(self.child))

    def test_config_name_must_be_local_and_markers_cannot_be_symlinks(self):
        marker = self.project / 'workspace.p4config'
        marker.touch()
        with patch.dict(os.environ, {'P4CONFIG': marker.name}):
            self.assertIsNotNone(self.admitted(self.child))
        for name in (str(marker), '../workspace.p4config', r'..\workspace.p4config', 'C:config', ''):
            with self.subTest(name=name), patch.dict(os.environ, {'P4CONFIG': name}):
                self.assertIsNone(self.admitted(self.child))
        # Simulate link metadata portably: Windows may require a special
        # privilege to create symlinks in otherwise ordinary test directories.
        links = {self.project / '.p4config', self.project / 'Alias.uproject'}
        for link in links:
            link.touch()
        is_symlink = Path.is_symlink
        with patch.object(Path, 'is_symlink', lambda path: path in links or is_symlink(path)):
            self.assertIsNone(self.admitted(self.child))

    def test_git_ancestor_preserves_identity(self):
        (self.project.parent / '.git').mkdir()
        (self.project / '.p4config').touch()
        self.assertEqual(discover(self.child)['project_root'], str(self.project.parent))

    def test_windows_metadata_case_and_cross_directory_boundaries(self):
        scope = dict(self.scope, include_roots=[{'root': 'G:/', 'sharing': 'personal'}],
                     exclude_roots=['g:/private'])
        local = {'kind': 'project', 'cwd': r'g:\Work\Game\Source',
                 'project_root': 'G:/WORK/Game', 'repository_root': 'g:/work/game'}
        self.assertIsNotNone(select(scope, local))
        self.assertIsNone(select(scope, dict(local, cwd='G:/Private/Game',
                                            project_root='G:/Private', repository_root='G:/Private')))
        self.assertIsNone(select(scope, dict(local, cwd='J:/work/game',
                                            project_root='J:/work/game', repository_root='J:/work/game')))
        self.assertIsNone(select(scope, {'kind': 'non-project', 'cwd': 'G:/Notes',
                                        'project_root': None, 'repository_root': None}))
