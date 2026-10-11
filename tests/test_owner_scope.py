"""Collection cache policy and local path checks in isolated homes."""
import copy
import tempfile
from pathlib import Path
import unittest
import content_contract as contract
from content_cache import ContentAdapter,CacheBlocked,validate_policy
from https_content import grant_check
from test_https_content import GRANT

class OwnerScope(unittest.TestCase):
    def setUp(self):
        self.grant=dict(copy.deepcopy(GRANT),ids={k:contract.OWNER_SCOPE for k in contract.TYPES})
        self.policy={'schema':1,'adapter':'codex','kinds':self.grant['artifact_types'],'ids':self.grant['ids'],'dependencies':{'commands':[],'python_modules':[]}}

    def test_owner_and_legacy_grants_not_interchangeable(self):
        grant_check(self.grant);grant_check(GRANT);validate_policy(self.policy,self.grant)
        with self.assertRaises(CacheBlocked):validate_policy(self.policy,GRANT)
        narrowed=copy.deepcopy(self.policy);narrowed['ids']=copy.deepcopy(GRANT['ids']);validate_policy(narrowed,self.grant)
        for bad in ({'memory':'personal-owner','rule':[],'skill':[]},{'memory':'*','rule':'*','skill':'*'}):
            with self.assertRaises(ValueError):grant_check(dict(self.grant,ids=bad))
        for identity in ('../outside','/tmp/outside','a/b','a%2Fb','a\\b'):
            self.assertFalse(contract.allows(self.grant['ids'],'skill',identity))

    def test_owned_namespace_dynamic_skill_and_external_files_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory).resolve()/'home';home.mkdir(mode=0o700)
            client=type('Client',(),{'grant':self.grant})()
            adapter=ContentAdapter(home,Path(directory)/'cache',self.policy,client,codex_home=home/'codex')
            self.assertEqual(adapter._path('.agents/skills/future-skill/SKILL.md'),home/'.agents/skills/future-skill/SKILL.md')
            for name in ('.agents/skills/future-skill/../outside','other/file','.config/agent-brain-client/content/rule/../outside.md'):
                with self.assertRaises((CacheBlocked,ValueError)):adapter._path(name)
            foreign=home/'.agents/skills/future-skill';foreign.mkdir(parents=True);(foreign/'SKILL.md').write_text('Foreign file')
            snapshot={'records':[contract.envelope('skill','future-skill',{'description':'Future','enabled':True,'files':{'SKILL.md':'---\nname: future-skill\ndescription: Future\n---\nApproved.'},'dependencies':{'commands':[],'python_modules':[]}})]}
            adapter._fetch=lambda:snapshot
            self.assertEqual(adapter.refresh()['status'],'content-refresh-blocked')
            self.assertEqual((foreign/'SKILL.md').read_text(),'Foreign file')

if __name__=='__main__':unittest.main()
