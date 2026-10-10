"""Version-one typed content contract; Core memory remains its canonical source.

Rule/Skill records are server-owned JSON, not client-supplied Git paths. The
registry is intentionally small and extensible; clients cannot invent types.
"""
import hashlib
import json
from pathlib import PurePosixPath
import re

TYPES={'memory','rule','skill'}
FIELDS={'content_read':{'kind','id'},'content_catalog':{'kinds'},
        'content_put':{'kind','id','record','expected_revision'},
        'content_remove':{'kind','id','expected_revision'}}
WRITES={'content_put','content_remove'}
SCOPES={'read','put','write','remove'}

def normalized_scopes(scopes):return {'put' if scope=='write' else scope for scope in scopes}

def permits_scope(scopes,method):
    if method=='content_put':return 'put' in normalized_scopes(scopes)
    if method=='content_remove':return 'remove' in scopes
    return 'read' in scopes

def methods(scopes):return {name for name in FIELDS if permits_scope(scopes,name)}

def encode(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()

def validate(method,params):
    if method not in FIELDS or not isinstance(params,dict) or set(params)!=FIELDS[method]:raise ValueError('Invalid content request')
    if method=='content_catalog':
        kinds=params['kinds']
        if not isinstance(kinds,list) or not kinds or any(not isinstance(k,str) or k not in TYPES for k in kinds) or len(set(kinds))!=len(kinds):raise ValueError('Unsupported kinds')
        return
    if not isinstance(params['kind'],str) or params['kind'] not in TYPES or not isinstance(params['id'],str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',params['id']):raise ValueError('Unsupported content identity')
    if method in WRITES and (not isinstance(params['expected_revision'],str) or not re.fullmatch(r'missing|[a-f0-9]{64}',params['expected_revision'])):raise ValueError('Read revision required')
    if method=='content_put':record(params['kind'],params['record'],params['id'])

def grant_ids(types,ids):
    if not isinstance(ids,dict) or set(ids)!=set(types):raise ValueError('Exact content IDs required')
    for kind,values in ids.items():
        if kind=='memory' and values=='*':continue
        if not isinstance(values,list) or len(values)>256 or any(not isinstance(v,str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',v) for v in values) or len(set(values))!=len(values):raise ValueError('Invalid content IDs')

def allows(ids,kind,identity):return kind=='memory' and ids.get(kind)=='*' or identity in ids.get(kind,[])

def text(value,limit=65536):
    if not isinstance(value,str) or not value.strip() or len(value.encode())>limit or '\0' in value:raise ValueError('Invalid text')

def strings(value,limit=32):
    if not isinstance(value,list) or len(value)>limit or any(not isinstance(v,str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.+-]{0,63}',v) for v in value) or len(set(value))!=len(value):raise ValueError('Invalid dependencies')

def skill_frontmatter(value,identity=None):
    front=re.match(r'\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)',value['files']['SKILL.md'],re.S)
    if not front:raise ValueError('Skill frontmatter required')
    def scalar(field):
        found=re.findall(r'^'+field+r':[ \t]*(.*)$',front[1],re.M)
        if len(found)!=1:raise ValueError('Unique Skill metadata required')
        raw=found[0].strip()
        if raw.startswith('"'):raw=json.loads(raw)
        elif raw.startswith("'"):
            if not raw.endswith("'") or len(raw)<2:raise ValueError('Invalid metadata')
            raw=raw[1:-1].replace("''", "'")
        elif raw[:1] in ('|','>','!','&','*','[','{','?') or raw.lower() in ('true','false','null','~','.nan','.inf','-.inf') or re.fullmatch(r'[-+]?\d+(?:\.\d+)?',raw) or re.search(r':\s',raw):raise ValueError('Unverified non-string/multiline/tagged metadata')
        else:raw=re.split(r'[ \t]+#',raw,1)[0].rstrip()
        text(raw,1024);return raw
    name,description=scalar('name'),scalar('description')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',name) or identity is not None and name!=identity or description!=value['description'].strip():raise ValueError('Skill discovery metadata mismatch')

def skill_path(name):
    if not isinstance(name,str) or len(name)>256 or '\\' in name:raise ValueError('Invalid skill path')
    path=PurePosixPath(name)
    if path.is_absolute() or path.as_posix()!=name or any(p.startswith('.') or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]*',p) for p in path.parts):raise ValueError('Unsafe skill path')

def record(kind,value,identity=None):
    if not isinstance(value,dict) or len(encode(value))>65536:raise ValueError('Invalid content record')
    if kind=='memory':
        if set(value)!={'title','description','body','type'} or value['type'] not in ('user','feedback','reference'):raise ValueError('Invalid memory record')
        text(value['title'],1024);text(value['description'],1024);text(value['body'])
    elif kind=='rule':
        if set(value)!={'title','body','enabled','trigger'} or type(value['enabled'])!=bool:raise ValueError('Invalid rule record')
        text(value['title'],1024);text(value['body'])
        trigger=value['trigger']
        if not isinstance(trigger,dict) or set(trigger)!={'mode','patterns'} or trigger['mode'] not in ('always','on-demand','conditional') or not isinstance(trigger['patterns'],list) or len(trigger['patterns'])>32:raise ValueError('Invalid rule trigger')
        for pattern in trigger['patterns']:text(pattern,256)
        if bool(trigger['patterns'])!=(trigger['mode']=='conditional'):raise ValueError('Conditional patterns required')
    elif kind=='skill':
        if set(value)!={'description','enabled','files','dependencies'} or type(value['enabled'])!=bool:raise ValueError('Invalid skill record')
        text(value['description'],1024)
        files=value['files']
        if not isinstance(files,dict) or 'SKILL.md' not in files or not 1<=len(files)<=32:raise ValueError('Skill entry required')
        for name,body in files.items():
            skill_path(name)
            text(body)
        dep=value['dependencies']
        if not isinstance(dep,dict) or set(dep)!={'commands','python_modules'}:raise ValueError('Invalid dependencies')
        strings(dep['commands']);strings(dep['python_modules'])
        if value['enabled']:skill_frontmatter(value,identity)
    else:raise ValueError('Unsupported content kind')

def envelope(kind,identity,value,raw=None):
    return {'content_api_version':1,'kind':kind,'id':identity,'revision':hashlib.sha256(raw if raw is not None else encode(value)).hexdigest() if value is not None else 'missing','record':value}

def path(kind,identity):return 'content/'+kind+'/'+identity+'.json'
