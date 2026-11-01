"""Portable source identity contract; paths are metadata, never opened."""
import copy
import unittest
from global_memory_scope import path, select, source_id


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
        for value in ('C:relative',r'\\server\share',r'\\?\C:\Work',r'C:\Work\..\Private','C:/Work/file:stream','//server/share','/synthetic/../private'):
            with self.subTest(value=value), self.assertRaises(ValueError): path(value)
