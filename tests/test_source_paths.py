"""Portable source identity contract; paths are metadata, never opened."""
import copy
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_brain_client.adapters.storage import origins
from agent_brain_client.domain.contract import recall_origin
from agent_brain_client.domain.scopes import ordinary_drive_path, path, select, source_id


class SourcePaths(unittest.TestCase):
    def test_windows_normalization_and_non_project_scope(self):
        scope={'schema':1,'profile_id':'a'*32,'include_roots':[{'root':'C:/Work','sharing':'personal'}],
               'exclude_roots':['D:/Private'],'non_project_roots':['C:/Notes'],'recall_personal':True}
        origin={'kind':'project','cwd':r'C:\Work\app\src','project_root':'c:/work/app','repository_root':'C:/WORK/app'}
        self.assertEqual(select(scope,origin)['sharing'],'personal')
        self.assertEqual(source_id(scope,origin,'project'),source_id(scope,dict(origin,repository_root='c:/work/APP'),'project'))
        for key in ('cwd','project_root','repository_root'):
            denied=copy.deepcopy(origin);denied[key]='D:/Private/repository'
            with self.subTest(key=key):
                try: result=select(scope,denied)
                except ValueError: result=None
                self.assertIsNone(result)
        self.assertEqual(select(scope,{'kind':'non-project','cwd':'C:/Notes/today','project_root':None,'repository_root':None})['sharing'],'personal')
        self.assertIsNone(select(scope,{'kind':'non-project','cwd':'/synthetic/notes','project_root':None,'repository_root':None}))

    def test_nonlocal_ambiguous_or_traversal_paths_fail(self):
        for value in ('C:relative',r'\\server\share',r'C:\Work\..\Private','C:/Work/file:stream','//server/share','/synthetic/../private'):
            with self.subTest(value=value), self.assertRaises(ValueError): path(value)

    def test_equivalent_extended_drive_paths_keep_the_same_source_identity(self):
        scope = {
            'schema': 1, 'profile_id': 'a' * 32, 'recall_personal': False,
            'include_roots': [{'root': 'C:/Synthetic', 'sharing': 'project'}],
            'non_project_roots': [], 'exclude_roots': ['C:/Synthetic/Private'],
        }
        for root in (r'C:\Synthetic\Project', r'C:\Synthetic\Private', 'C:\\'):
            with self.subTest(root=root):
                ordinary = dict(kind='project', cwd=root, project_root=root, repository_root=root)
                extended = {**ordinary, 'cwd': '\\\\?\\' + root, 'project_root': '\\\\?\\' + root}
                normalized = {key: ordinary_drive_path(value) for key, value in extended.items()}
                self.assertEqual(normalized, ordinary)
                self.assertEqual(select(scope, normalized), select(scope, ordinary))
                self.assertEqual(source_id(scope, normalized, 'project'), source_id(scope, ordinary, 'project'))
                self.assertEqual(recall_origin(extended), recall_origin(ordinary))

    def test_unsupported_windows_paths_are_rejected_before_filesystem_resolution(self):
        unsupported = (
            r'\\host\share\project', r'\\?\UNC\host\share\project',
            r'\\.\C:\project', r'\\?\GLOBALROOT\Device\HarddiskVolume1\project',
            r'\\?\Volume{00000000-0000-0000-0000-000000000000}\project',
            r'C:project', r'project\src', r'\project', r'C:\project\..\outside',
            r'\\?\C:project', r'\\?\C:/project', r'\\?\C:\project\\src',
            r'\\?\C:\project\.\src', r'\\?\C:\project\..\outside',
            r'\\?\C:\project\dir:stream', r'\\?\C:\project\dir.',
            r'\\?\C:\project\dir ', r'\\?\C:\project\ dir',
            r'\\?\C:\project\NUL.txt', r'\\?\C:\project\NUL .txt',
            r'\\?\C:\project\COM¹', r'\\?\C:\project\CONOUT$',
            r'\\?\C:\project\dir?', '\\\\?\\C:\\project\\bad\tname',
            '//?/C:/project',
        )
        windows = SimpleNamespace(name='nt', fspath=os.fspath)
        with patch.object(origins, 'os', windows), patch.object(origins, 'Path') as fs:
            for value in unsupported:
                with self.subTest(value=value):
                    self.assertEqual(ordinary_drive_path(value), value)
                    origin = dict(kind='project', cwd=value, project_root=value, repository_root=value)
                    with self.assertRaises(ValueError):
                        recall_origin(origin)
                    with self.assertRaises(ValueError):
                        origins.resolve_cwd(value, strict=True)
            fs.assert_not_called()
