"""User-operated Rule/Skill management using the existing enrolled HTTPS identity.

The short-lived loopback page has no external resources. Listing is read-only;
only explicit Enable/Disable and Pull actions change Server/local state.
"""


import sys
from pathlib import Path

if __package__ in (None, ""):
    _ENTRY_ROOT = Path(__file__).resolve().parents[4]
    sys.path[:0] = [str(_ENTRY_ROOT / "src"), str(_ENTRY_ROOT)]


from agent_brain_client.application.content.management import (
    LiveReadRequired,
    Management,
)


import argparse
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import secrets
import webbrowser

ROOT = Path(__file__).resolve().parents[4]
import agent_brain_client.domain.contract as contract
from agent_brain_client.adapters.storage.content_session import ContentSession
from agent_brain_client.adapters.storage.registration import registration
from agent_brain_client.adapters.storage.metadata import private_json, canonical


def installed_adapter(home, *, harness=None):
    if harness is None:
        from agent_brain_client.runtime.client_lifecycle import registered_host
        port = registered_host(home)
        if port is None:
            raise ValueError("Registered native host required")
        harness = port.name
    from agent_brain_client.runtime.harness_content import content_host
    home = canonical(Path(home).expanduser(), existing=True)
    _, policy = registration(home)
    runtime = policy["runtime"]
    if canonical(runtime["device_home"]) != home:
        raise ValueError("Registered device home differs")
    from agent_brain_client.adapters.packages.client import policy_file

    meta = private_json(home / ".config/agent-brain-client/codex-mcp.json")
    policy_file(meta["release_policy"], meta["release_policy_sha256"])
    from agent_brain_client.adapters.server.https import HTTPSContentClient

    client = HTTPSContentClient(
        runtime["https_profile"], runtime["https_profile_sha256"]
    )
    content = private_json(runtime["content_policy"], runtime["content_policy_sha256"])
    port = content_host(harness)
    native, cache = port.location(home, runtime)
    return ContentSession(
        home,
        cache,
        content,
        client,
        host=port, native_home=native,
    )


PAGE = r"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Brain · Rule / Skill</title><style>
:root{font:15px/1.6 system-ui,sans-serif;color:#172c36;background:#f3f6f5}*{box-sizing:border-box}
body{max-width:1080px;margin:48px auto;padding:0 24px}header{display:flex;gap:20px;justify-content:space-between;align-items:center}
h1{font-size:30px;letter-spacing:-1px;margin:0}p{color:#536871}small{display:block;color:#536871}
button{font:inherit;border:1px solid #bfceca;background:white;border-radius:9px;padding:8px 15px;cursor:pointer}
button.primary{background:#166454;color:white;border-color:#166454}button:disabled{opacity:.5;cursor:wait}
nav{display:flex;gap:10px;flex-wrap:wrap;margin:24px 0}.card{background:white;border:1px solid #dae3e0;border-radius:14px;padding:20px;margin:12px 0}
.card header{align-items:flex-start}.card h2{font-size:18px;margin:0;overflow-wrap:anywhere}.pill{font-size:12px;background:#e6f2eb;color:#205d42;border-radius:20px;padding:3px 9px;white-space:nowrap}.off{background:#edf0f2;color:#5a6a74}
.meta{font-size:13px;color:#536871;margin:8px 0}.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:16px}
#notice{padding:16px;border-radius:10px;background:#e5efea;white-space:pre-wrap;overflow-wrap:anywhere}#notice.error{background:#fff0db;color:#7c4c16}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}details{margin-top:12px}label{display:block;padding:8px 0}
.empty{text-align:center;padding:50px;color:#536871}.warning{color:#986322}footer{margin-top:32px;color:#536871;font-size:13px}
</style><header><div><small>AGENT BRAIN · 本机管理</small><h1>Rule / Skill</h1></div><span class="pill">已有设备身份</span></header>
<p>启停会保存到共享 Server，并尝试同步到本机。其他设备需在正常启动或点击“拉取到本机”时同步。</p>
<nav><button id="list">刷新列表</button><button id="pull" class="primary">拉取到本机</button></nav>
<div id="notice" role="status" aria-live="polite">正在读取 Server 状态…</div><main id="records"></main><section id="operations"></section>
<details><summary>本次操作详情</summary><pre id="detail"></pre></details>
<footer>列表读取不改本地映射。拉取不重试待发送写入。文件同步不代表正在运行的模型已重新加载；必要时按宿主正常方式开启新会话或重载上下文。关闭终端中的管理程序即可结束本页会话。</footer>
<script>
'use strict';
const token=location.hash.slice(1)||sessionStorage.getItem('agentBrainManagementToken')||'';
if(token)sessionStorage.setItem('agentBrainManagementToken',token);history.replaceState(null,'',location.pathname);
let busy=false;const el=id=>document.getElementById(id);
const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n};
function buttons(disabled){document.querySelectorAll('button').forEach(n=>n.disabled=disabled||n.dataset.denied==='true')}
async function api(path,value){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+token},body:JSON.stringify(value)});const v=await r.json();if(!r.ok)throw new Error(v.message||v.status);return v}
function tell(text,error=false){el('notice').textContent=text;el('notice').className=error?'error':''}
function report(v){el('detail').textContent=JSON.stringify(v,null,2);const f=v.local_refresh||{},q=v.local_queue||{},e=v.error;
 if(q.server_saved===true){const ok=['content-current','content-refreshed'].includes(f.status);tell('Server 已保存；GitHub 备份'+(q.github_confirmed?'已确认':'待确认')+'。\n'+(ok?'本机文件已同步。':'本机文件同步尚未完成：'+(f.status||'未知')+' / '+(f.stage||'')+'。修复原因后点击“拉取到本机”，无需重新提交保存。')+'\n模型加载状态：未知。',!ok)}
 else if(e){tell('Server 保存未确认：'+e.code+'。\n'+(q.next_action||'保留原请求；检查连接或权限后重试。'),true)}
 else{const ok=['content-current','content-refreshed'].includes(f.status);tell(ok?'本机文件已按 Server 最新快照同步；模型加载状态未知。':'本机同步尚未完成：'+(f.status||'未知')+' / '+(f.stage||'')+'。请查看本次操作详情。',!ok)}}
async function run(action){if(busy)return;busy=true;buttons(true);try{await action()}catch(e){tell('操作未完成：'+e.message+'。保留原请求，恢复连接或文件权限后重试。',true)}finally{busy=false;buttons(false)}}
async function load(silent=false){let s;try{s=await api('/state',{})}catch(e){el('records').replaceChildren();el('operations').replaceChildren();if(silent){const previous=el('notice').textContent;tell(previous+'\n当前列表读取失败：'+e.message+'。恢复连接后刷新列表。',true);return}throw e}el('records').replaceChildren();el('operations').replaceChildren();
 if(!s.records.length)el('records').append(node('div','当前授权范围内没有 Rule / Skill。','empty'));
 for(const r of s.records){const c=node('article',undefined,'card'),h=node('header'),title=node('div');title.append(node('small',r.kind.toUpperCase()+' · '+r.id),node('h2',r.title));h.append(title,node('span',r.enabled?'已启用':'已停用','pill'+(r.enabled?'':' off')));c.append(h);
 const status={current:'与本次 Server 版本一致','pull-required':'待拉取','review-required':'文件或所有权检查未通过'};c.append(node('div','本机映射：'+status[r.mapping.status]+' · 模型加载：未知','meta'));
 const pending=s.operations.some(o=>o.kind===r.kind&&o.id===r.id&&o.state==='pending');const conflicts=s.operations.filter(o=>o.kind===r.kind&&o.id===r.id&&o.state==='conflict');let review;
 if(conflicts.length){review=node('input');review.type='checkbox';const l=node('label');l.append(review,document.createTextNode(' 依据当前版本重新应用，并将 '+conflicts.length+' 个启停冲突关联为已处理'));c.append(l)}
 const a=node('div',undefined,'actions');const b=node('button',r.enabled?'停用':'启用',r.enabled?'':'primary');if(!s.can_change||pending){b.dataset.denied='true';b.disabled=true}b.onclick=()=>run(async()=>{if(review&&!review.checked){tell('请先检查当前版本，并勾选冲突处理选项。',true);return}const request={request_id:crypto.randomUUID().replaceAll('-',''),kind:r.kind,id:r.id,enabled:!r.enabled,expected_revision:r.revision,reconcile_request_ids:conflicts.map(o=>o.request_id)};const v=await api('/change',request);report(v);await load(true)});a.append(b);c.append(a);if(pending)c.append(node('small','此项目有结果未确认的启停请求，请在下方使用原请求重试。','warning'));el('records').append(c)}
 if(s.operations.length){el('operations').append(node('h2','保留的启停请求'));for(const o of s.operations){const c=node('div',undefined,'card');c.append(node('div',o.kind+' / '+o.id+' → '+(o.enabled?'启用':'停用')+' · '+o.state),node('small',o.request_id));if(o.error)c.append(node('small',o.error.code));if(o.state!=='conflict'){const b=node('button','使用原请求重试');b.onclick=()=>run(async()=>{const v=await api('/retry',{request_id:o.request_id});report(v);await load(true)});c.append(b)}else c.append(node('small','刷新列表后检查当前状态，再明确选择需要的状态；原请求将保留。'));el('operations').append(c)}}
 if(!silent)tell('已读取 Server 当前状态。刷新列表不改本机文件。队列：待确认 '+s.queue.pending+'，冲突 '+s.queue.conflict+'，阻断 '+s.queue.blocked+'。')}
el('list').onclick=()=>run(()=>load());el('pull').onclick=()=>run(async()=>{report(await api('/pull',{}));await load(true)});run(()=>load());
</script></html>"""


class ManagementHTTP(HTTPServer):
    allow_reuse_address = False

    def __init__(self, management):
        super().__init__(("127.0.0.1", 0), Handler)
        self.management = management
        self.token = secrets.token_urlsafe(32)
        self.origin = "http://127.0.0.1:" + str(self.server_port)


class Handler(BaseHTTPRequestHandler):
    server: ManagementHTTP

    def log_message(self, *_args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def reply(self, status, value, html=False):
        raw = value.encode() if html else contract.encode(value)
        self.send_response(status)
        self.send_header(
            "Content-Type",
            "text/html; charset=utf-8" if html else "application/json; charset=utf-8",
        )
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(raw)

    def trusted_host(self):
        return self.headers.get_all("Host") == [
            self.server.origin.removeprefix("http://")
        ]

    def do_GET(self):
        if not self.trusted_host() or self.path != "/":
            self.reply(403, {"status": "management-access-denied"})
            return
        self.reply(200, PAGE, html=True)

    def do_POST(self):
        auth = self.headers.get("Authorization", "")
        if (
            not self.trusted_host()
            or self.headers.get_all("Authorization") != [auth]
            or not hmac.compare_digest(auth, "Bearer " + self.server.token)
            or self.headers.get("Origin") != self.server.origin
            or self.headers.get("Sec-Fetch-Site") not in (None, "same-origin")
        ):
            self.reply(403, {"status": "management-access-denied"})
            return
        routes = {
            "/state": self.server.management.state,
            "/change": self.server.management.change,
            "/retry": self.server.management.retry,
            "/pull": self.server.management.pull,
        }
        try:
            length = self.headers.get("Content-Length", "")
            if (
                self.headers.get("Transfer-Encoding") is not None
                or not length.isdecimal()
                or not 0 < int(length) <= 4096
                or self.headers.get("Content-Type") != "application/json"
            ):
                raise ValueError()

            def unique(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError()
                    value[key] = item
                return value

            value = json.loads(self.rfile.read(int(length)), object_pairs_hook=unique)
            if self.path not in routes:
                raise ValueError()
            if self.path == "/state":
                if value != {}:
                    raise ValueError()
                result = self.server.management.state()
            else:
                result = routes[self.path](value)
            self.reply(200, result)
        except LiveReadRequired:
            self.reply(
                503,
                {
                    "status": "management-live-read-required",
                    "message": "需要 Server 实时读取；离线缓存不能确认当前启停状态",
                },
            )
        except (ValueError, TypeError, KeyError):
            self.reply(400, {"status": "management-request-invalid"})
        except Exception:
            self.reply(
                503,
                {
                    "status": "management-unavailable",
                    "message": "检查原设备连接、授权和文件访问；已保存请求需保留原 ID 重试",
                },
            )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["content-manage"])
    parser.add_argument("--device-home", default=str(Path.home()))
    parser.add_argument("--harness", choices=("codex", "codebuddy"))
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    try:
        management = Management(installed_adapter(args.device_home, harness=args.harness))
        with ManagementHTTP(management) as server:
            url = server.origin + "/#" + server.token
            print("Rule / Skill 本机管理：" + url, flush=True)
            print("仅当前终端会话可用；Ctrl-C 关闭。", flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            server.serve_forever(poll_interval=0.2)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        print(
            json.dumps(
                {
                    "status": "content-management-unavailable",
                    "next_action": "Use the existing verified HTTPS Client registration; preserve identity, policy and queues.",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
