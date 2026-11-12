#!/usr/bin/env python3
"""Python web GUI. No Flask, Node.js, X11 or external assets are required."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import threading
import webbrowser
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from hpc_monitor_core import DEFAULTS, VERSION, LogStore, analyze, demo_store, natural_key, options_from
from hpc_monitor_files import directory_listing

ASSET = Path(__file__).with_name("HPC_Usage_Monitor.html")
TRANSPORT = r"""
  let worker=null,workerURL=null;
  const SERVER_TOKEN=__SERVER_TOKEN__;
  const codec={
    defaults:{offset:540,interval:30,startedMode:'snapshot',nodeClock:true,denominator:'observed',bucket:'day'},
    iso:(t,o=540)=>new Date(t+o*60000).toISOString().slice(0,19),
    time:(v,o=540)=>{
      if(typeof v==='number')return Number.isFinite(v)?v:null;
      const s=String(v||'').trim();if(!s)return null;
      const zone=/Z$|[+-]\d{2}:?\d{2}$/.test(s);
      const t=Date.parse(zone?s:s+'Z');return Number.isFinite(t)?t-(zone?0:o*60000):null;
    }
  };
  const api=async(op,payload={})=>{
    let response;
    if(op==='import'){
      const form=new FormData();
      for(const file of payload.files)form.append('files',file,file.name);
      form.append('options',JSON.stringify({replace:payload.replace,settings:payload.settings}));
      response=await fetch('/api/import',{method:'POST',headers:{'X-HPC-Token':SERVER_TOKEN},body:form});
    }else{
      response=await fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json','X-HPC-Token':SERVER_TOKEN},body:JSON.stringify({op,...payload})});
    }
    const result=await response.json();
    if(!response.ok||result.error)throw Error(result.error||('HTTP '+response.status));
    return result.data;
  };
"""

DIRECTORY_DIALOG = r"""
<style>
#server-files{width:850px;max-width:94vw}.file-path{display:flex;gap:8px;margin:15px 0}.file-path input{flex:1;min-width:0}
#server-file-list{height:330px;overflow-y:auto;overflow-x:hidden;border:1px solid var(--line);border-radius:6px}
.file-row{display:flex;align-items:center;gap:9px;width:100%;padding:9px 12px;text-align:left;border-bottom:1px solid var(--line);overflow-wrap:anywhere}
.file-row:hover{background:var(--panel-soft)}.file-row .file-name{flex:1;min-width:0}.file-kind{font-size:11px;color:var(--muted);white-space:nowrap}
.file-icon{font-size:18px;flex-shrink:0}.file-icon.folder{color:#c28c27}.file-icon.log{color:#4d92d2}.file-icon.session{color:#319c84}
#server-file-error{color:var(--error);min-height:20px;font-size:12px}#server-file-count{font-size:12px;color:var(--muted)}
</style>
<dialog id="server-files" aria-labelledby="server-files-title">
  <div class="dialog-title"><h2 id="server-files-title">서버 폴더에서 로그 불러오기</h2><button data-close="server-files" aria-label="파일 선택 닫기">×</button></div>
  <p>Python 서버가 실행 중인 컴퓨터의 폴더입니다. PC 파일은 아래 버튼으로 선택하세요.</p>
  <form id="server-path-form" class="file-path"><button type="button" id="server-up" class="button subtle" aria-label="상위 폴더로 이동">↑ 상위 폴더</button><input id="server-path" aria-label="서버 폴더 경로" placeholder="/path/to/logs 또는 ~/logs" autocomplete="off"><button class="button subtle" type="submit">이동</button></form>
  <div id="server-file-list" aria-label="폴더와 파일 목록" aria-busy="false"></div>
  <div id="server-file-error" role="alert"></div><div id="server-file-count" aria-live="polite">파일을 선택하세요.</div>
  <div class="dialog-actions"><button class="button subtle" id="local-file-button">내 PC 파일 선택</button><button class="button subtle" data-close="server-files">취소</button><button class="button primary" id="server-import" disabled>선택한 파일 불러오기</button></div>
</dialog>
"""

DIRECTORY_SCRIPT = r"""
  let serverDirectory=null,serverBrowseGeneration=0;
  const fileKinds={folder:'폴더',log:'로그 파일',session:'통합 JSON',other:'파일'};
  async function browseServer(path){
    const generation=++serverBrowseGeneration,list=$('#server-file-list');
    $('#server-file-error').textContent='';list.setAttribute('aria-busy','true');
    $('#server-import').disabled=true;$('#server-up').disabled=true;
    try{
      const listing=await api('browse',{path,base:serverDirectory?.path});
      if(generation!==serverBrowseGeneration)return;
      serverDirectory=listing;$('#server-path').value=listing.path;
      list.innerHTML=listing.entries.map((row,i)=>{
        const icon='<span class="file-icon '+row.kind+'" aria-hidden="true">'+(row.directory?'▸':'≡')+'</span>';
        const body=icon+'<span class="file-name">'+esc(row.name)+'</span><span class="file-kind">'+fileKinds[row.kind]+'</span>';
        return row.directory?'<button class="file-row" data-server-folder="'+i+'">'+body+'</button>':'<label class="file-row"><input type="checkbox" data-server-file="'+i+'">'+body+'</label>';
      }).join('')||'<p style="padding:15px">빈 폴더입니다.</p>';
      list.scrollTop=0;$('#server-file-count').textContent='파일을 선택하세요.';
    }catch(error){if(generation===serverBrowseGeneration){$('#server-file-error').textContent=error.message;if(serverDirectory)$('#server-path').value=serverDirectory.path;}}
    finally{if(generation===serverBrowseGeneration){list.setAttribute('aria-busy','false');$('#server-up').disabled=!serverDirectory||serverDirectory.path===serverDirectory.parent;updateServerSelection();}}
  }
  function updateServerSelection(){
    const count=$$('#server-file-list input:checked').length;
    $('#server-import').disabled=!count||$('#server-file-list').getAttribute('aria-busy')==='true';
    $('#server-file-count').textContent=count?count+'개 파일 선택':'파일을 선택하세요.';
  }
  function openServerFiles(){
    if(S.loading)return;
    $('#server-files').showModal();browseServer(serverDirectory?.path||'.');
  }
  $('#server-path-form').onsubmit=event=>{event.preventDefault();browseServer($('#server-path').value);};
  $('#server-up').onclick=()=>{if(serverDirectory)browseServer(serverDirectory.parent);};
  $('#server-file-list').onclick=event=>{const target=event.target.closest('[data-server-folder]');if(target&&serverDirectory)browseServer(serverDirectory.entries[+target.dataset.serverFolder].path);};
  $('#server-file-list').onchange=updateServerSelection;
  $('#local-file-button').onclick=()=>{$('#server-files').close();$('#file-input').click();};
  $('#server-import').onclick=async()=>{
    if(!serverDirectory)return;
    const paths=$$('#server-file-list input:checked').map(input=>serverDirectory.entries[+input.dataset.serverFile].path);
    if(!paths.length)return;
    $('#server-files').close();await importFiles(paths,'importPaths');
  };
"""


def web_page(token):
    text = ASSET.read_text(encoding="utf-8")
    import re
    text = re.sub(r'<script id="hpc-engine" type="text/plain">.*?</script>', "", text, flags=re.S)
    first = text.index("  let api,worker,workerURL,request=0,waiting=new Map(),localState;")
    last = text.index("  let saved={};", first)
    text = text[:first] + TRANSPORT.replace("__SERVER_TOKEN__", json.dumps(token)) + text[last:]
    text = text.replace('<input type="file" id="file-input" multiple hidden>', '<input type="file" id="file-input" multiple hidden>' + DIRECTORY_DIALOG)
    text = text.replace("  const openImport=()=>$('#file-input').click();", DIRECTORY_SCRIPT + "\n  const openImport=openServerFiles;")
    initial = """
  render();
  api('meta',{settings:S.options}).then(meta=>{
    if(meta.min!==null){S.meta=meta;setRange(meta);fillFilters(meta,true);analyze();}
  }).catch(e=>toast(e.message));
})();
"""
    marker = "  render();\n})();"
    if marker not in text:
        raise RuntimeError("HTML 버전과 Python 웹 어댑터가 맞지 않습니다.")
    text = text.replace(marker, initial)
    text = text.replace("<title>HPC 사용량 모니터</title>", "<title>HPC 사용량 모니터 · Python Web</title>")
    text = text.replace("브라우저 내 로컬 분석", "Python 서버에서 로컬 분석")
    text = text.replace("외부 전송 · 계정 연결 없음", "선택한 로그를 실행 서버로 전달")
    text = text.replace("외부 라이브러리 없이 오프라인 실행", "Python 집계 · 인터넷 연결 없이 실행")
    text = text.replace("HPC Usage Monitor · 로그 기반 이력 분석", "HPC Usage Monitor · Python Web " + VERSION)
    return text.encode("utf-8")


class MonitorSession:
    def __init__(self, store=None):
        self.store = store if store is not None else LogStore()
        self.last = None
        self.lock = threading.RLock()

    def command(self, message):
        with self.lock:
            op = message.get("op")
            if op == "meta":
                return self.store.describe(message.get("settings"))
            if op == "browse":
                return directory_listing(message.get("path", "."), message.get("base"))
            if op == "clear":
                self.store, self.last = LogStore(), None
                return True
            if op == "demo":
                self.store, self.last = demo_store(), None
                return self.store.describe(message.get("settings"))
            if op == "analyze":
                self.last = analyze(self.store, message.get("options"))
                if self.last["empty"]:
                    return self.last
                return dict(self.last, jobs=self.last["jobs"][:50], jobsTotal=len(self.last["jobs"]))
            if op == "jobs":
                if not self.last or self.last["empty"]:
                    return dict(rows=[], total=0)
                rows = self.last["jobs"]
                query = str(message.get("query", "")).lower()
                if query:
                    rows = [r for r in rows if any(query in str(r.get(k, "")).lower()
                                                   for k in ("id", "user", "partition", "nodes", "status", "state"))]
                key = str(message.get("key", "start"))
                present = [r for r in rows if r.get(key) is not None]
                missing = [r for r in rows if r.get(key) is None]
                present.sort(key=lambda r: (0, r[key]) if isinstance(r[key], (float, int)) else (1, natural_key(r[key])),
                             reverse=message.get("dir") == -1)
                rows = present + missing
                page = max(0, int(message.get("page", 0)))
                return dict(rows=rows[page * 50:page * 50 + 50], total=len(rows))
            if op == "jobExport":
                return self.last["jobs"] if self.last and not self.last["empty"] else []
            if op == "session":
                return json.dumps(self.store.session(options_from(message.get("settings"))), ensure_ascii=False, allow_nan=False)
            raise ValueError("지원하지 않는 명령입니다.")

    def import_paths(self, paths, settings, replace=False, maximum=128 * 1024 * 1024):
        if not isinstance(paths, list) or not 1 <= len(paths) <= 500 or any(not isinstance(path, str) or not path for path in paths):
            raise ValueError("불러올 파일을 1~500개 선택하세요.")
        files, used = [], 0
        for raw in paths:
            path = Path(raw).expanduser().resolve(strict=True)
            if not path.is_file():
                raise ValueError("일반 로그 파일을 선택하세요: " + str(path))
            if path.stat().st_size > maximum - used:
                raise ValueError(f"선택한 파일의 합계는 {maximum / 1024 / 1024:g} MB 이하여야 합니다.")
            with path.open("rb") as stream:
                data = stream.read(maximum - used + 1)
            used += len(data)
            if used > maximum:
                raise ValueError("선택한 파일의 크기 제한을 초과했습니다.")
            files.append((path.name, data))
        return self.import_files(files, settings, replace)

    def import_files(self, files, settings, replace=False):
        with self.lock:
            if replace or self.store.demo:
                self.store = LogStore()
            self.store.imported_settings = None
            for name, data in files:
                name = Path(name).name
                try:
                    self.store.load_text(data, name)
                except (ValueError, UnicodeError, TypeError) as exc:
                    self.store.issue(name, 0, "파일 읽기 실패: " + str(exc))
                    self.store.files.append(dict(name=name, rows=0, duplicates=0, invalid=1, types=[]))
            restored = self.store.imported_settings if len(files) == 1 else None
            self.last = None
            return dict(meta=self.store.describe(restored or settings), restored=restored)


def make_server(host="127.0.0.1", port=8888, session=None, max_upload_mb=128):
    session = session if session is not None else MonitorSession()
    token = secrets.token_urlsafe(32)
    page = web_page(token)
    maximum = int(max_upload_mb * 1024 * 1024)

    class Handler(BaseHTTPRequestHandler):
        server_version = "HPCMonitor/" + VERSION

        def valid_host(self):
            hostname = urlparse("http://" + self.headers.get("Host", "")).hostname
            return host not in ("127.0.0.1", "localhost", "::1") or hostname in ("127.0.0.1", "localhost", "::1")

        def log_message(self, fmt, *args):
            # File names, usernames and request bodies never enter access logs.
            if args and str(args[0]).startswith("POST"):
                return

        def reply(self, status, payload, content_type="application/json; charset=utf-8"):
            if not isinstance(payload, bytes):
                payload = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self' blob:; script-src 'self' 'unsafe-inline' blob:; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; worker-src blob:; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if not self.valid_host():
                self.reply(403, dict(error="허용되지 않은 Host입니다."))
                return
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self.reply(200, page, "text/html; charset=utf-8")
            elif path == "/health":
                self.reply(200, dict(status="ok", version=VERSION, engine="python"))
            elif path == "/favicon.ico":
                self.reply(204, b"", "image/x-icon")
            else:
                self.reply(404, dict(error="페이지를 찾을 수 없습니다."))

        def do_POST(self):
            if not self.valid_host():
                self.reply(403, dict(error="허용되지 않은 Host입니다."))
                return
            if not secrets.compare_digest(self.headers.get("X-HPC-Token", ""), token):
                self.reply(403, dict(error="현재 웹 화면에서 다시 시도하세요."))
                return
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host", ""):
                self.reply(403, dict(error="다른 출처의 요청은 허용하지 않습니다."))
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 0 or size > maximum:
                    self.reply(413, dict(error=f"한 번에 업로드할 수 있는 크기는 {max_upload_mb:g} MB입니다."))
                    return
                body = self.rfile.read(size)
                if len(body) != size:
                    raise ValueError("요청 본문이 완전하지 않습니다.")
                path = urlparse(self.path).path
                if path == "/api/command":
                    message = json.loads(body)
                    if not isinstance(message, dict):
                        raise ValueError("JSON 객체가 필요합니다.")
                    if message.get("op") == "importPaths":
                        result = session.import_paths(message.get("files"), message.get("settings"), message.get("replace") is True, maximum)
                    else:
                        result = session.command(message)
                elif path == "/api/import":
                    content_type = self.headers.get("Content-Type", "")
                    if not content_type.startswith("multipart/form-data"):
                        raise ValueError("multipart 파일 입력이 필요합니다.")
                    mime = BytesParser(policy=policy.default).parsebytes(
                        b"Content-Type: " + content_type.encode("ascii") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
                    if not mime.is_multipart():
                        raise ValueError("파일 입력 형식 오류")
                    files, options = [], {}
                    for part in mime.iter_parts():
                        if part.get_content_disposition() != "form-data":
                            continue
                        field = part.get_param("name", header="content-disposition")
                        if field == "files":
                            files.append((part.get_filename() or "input.tsv", part.get_payload(decode=True) or b""))
                        elif field == "options":
                            options = json.loads(part.get_payload(decode=True) or b"{}")
                    if not files:
                        raise ValueError("로그 파일을 선택하세요.")
                    if len(files) > 500:
                        raise ValueError("한 번에 최대 500개 파일을 선택하세요.")
                    result = session.import_files(files, options.get("settings"), options.get("replace") is True)
                else:
                    self.reply(404, dict(error="지원하지 않는 API입니다."))
                    return
                self.reply(200, dict(data=result))
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                self.reply(400, dict(error=str(exc)))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except OSError as exc:
                self.reply(400, dict(error=str(exc)))
            except Exception:
                self.reply(500, dict(error="처리 중 오류가 발생했습니다. 로그 형식과 조회 조건을 확인하세요."))

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.monitor_session = session
    server.monitor_token = token
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="HPC 사용량 모니터 · Python 웹 GUI")
    parser.add_argument("files", nargs="*", help="시작할 때 읽을 TSV/LOG/JSON 파일")
    parser.add_argument("--host", default="127.0.0.1", help="기본 127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8888")))
    parser.add_argument("--no-browser", action="store_true", help="서버의 브라우저를 자동으로 열지 않음")
    parser.add_argument("--demo", action="store_true", help="실제 로그 대신 내장 예제로 시작")
    parser.add_argument("--max-upload-mb", type=float, default=128)
    parser.add_argument("--self-test", action="store_true", help="GUI 없이 집계 엔진 검증")
    args = parser.parse_args(argv)
    if args.self_test:
        result = analyze(demo_store())
        assert result["summary"]["coreHours"] == 35968
        print("PASS: Python web engine · 6종 로그 · 35,968 core·h")
        return 0
    store = demo_store() if args.demo else LogStore()
    if args.files:
        store = LogStore()
        for path in args.files:
            store.load_path(path)
    if not 0 <= args.port <= 65535 or args.max_upload_mb <= 0:
        parser.error("port 또는 업로드 크기를 확인하세요.")
    try:
        server = make_server(args.host, args.port, MonitorSession(store), args.max_upload_mb)
    except OSError as exc:
        parser.exit(2, f"서버를 열 수 없습니다: {exc}\n다른 --port 값을 지정하세요.\n")
    address = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{address}:{server.server_address[1]}/"
    print(f"HPC Monitor Python Web {VERSION}\n브라우저 주소: {url}", flush=True)
    print("종료: Ctrl+C · 로그는 서버 메모리에서 처리하며 자동 저장하지 않습니다.", flush=True)
    if not args.no_browser and not os.environ.get("SSH_CONNECTION"):
        threading.Timer(.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
