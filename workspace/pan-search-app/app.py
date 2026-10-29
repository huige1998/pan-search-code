#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网盘搜索 + 自动转存 后端 v1(网页版试用)

流程(顺序固定,不跳步):
  群成员搜索 -> 候选广告标记 -> 提交转存任务 -> 管理员确认 -> worker 前置检查
  -> watch.py --dry-run 预览 -> watch.py 正式转存(转存->改短名->查重)
  -> 核验后入基线由 watch.py 负责 -> 状态回到网页,分享链接生成为下一步

只用 Python 标准库;搜索复用已安装的 ~/workspace/quark-share-watch/pan_search.py。
凭证不进本服务:夸克 cookie 只以文件形式存在 quark-share-watch/cookie.txt,
本服务只检查它是否存在,从不读取或返回它的内容。管理员口令在
data/admin_key.txt(0600),首次启动自动生成,只保存在本机。
"""
import json, os, re, secrets, subprocess, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
STATIC_DIR = os.path.join(APP_DIR, "static")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
ADMIN_KEY_FILE = os.path.join(DATA_DIR, "admin_key.txt")
APP_CONFIG_FILE = os.path.join(APP_DIR, "app_config.json")

if not os.path.isdir(os.path.expanduser("~/workspace/quark-share-watch")) \
        and os.path.isdir("/home/hatch/workspace/quark-share-watch"):
    # systemd 等环境下 HOME 可能不是用户家目录,回退到已知路径
    os.environ["HOME"] = "/home/hatch"

QUARK_DIR = os.path.expanduser("~/workspace/quark-share-watch")
QUARK_WATCH = os.path.join(QUARK_DIR, "watch.py")
QUARK_COOKIE = os.path.join(QUARK_DIR, "cookie.txt")
QUARK_CONFIG = os.path.join(QUARK_DIR, "config.json")
GUANGYA_DIR = os.path.expanduser("~/workspace/guangya-api")

sys.path.insert(0, QUARK_DIR)
import pan_search  # noqa: E402  已安装工具包的搜索客户端(仅标准库)

# 标题里出现这些字样时标记为疑似广告/加群条件,只标记、不自动采纳,
# 是否转存一律由管理员确认(与 SKILL 的候选验收规则一致)。
AD_TERMS = ["加群", "进群", "入群", "加微信", "加qq", "加QQ", "QQ群", "微信群",
            "公众号", "扫码", "引流", "推广", "广告", "付费", "代理", "开车",
            "飞机群", "电报", "telegram", "t.me", "联系方式", "客服微信"]

DRIVE_LABELS = {"quark": "夸克", "guangya": "光鸭", "ali": "阿里", "uc": "UC",
                "baidu": "百度", "xunlei": "迅雷", "123pan": "123网盘",
                "115": "115", "tianyi": "天翼", "yidong": "移动", "seed": "其他"}

_lock = threading.Lock()
_search_cache = {}
_worker_busy = False


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_tasks():
    if os.path.exists(TASKS_FILE):
        with open(TASKS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_tasks(tasks):
    atomic_write_json(TASKS_FILE, tasks)


def load_app_config():
    if os.path.exists(APP_CONFIG_FILE):
        with open(APP_CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def get_admin_key():
    if not os.path.exists(ADMIN_KEY_FILE):
        os.makedirs(DATA_DIR, exist_ok=True)
        key = secrets.token_urlsafe(12)
        with open(ADMIN_KEY_FILE, "w", encoding="utf-8") as f:
            f.write(key)
        os.chmod(ADMIN_KEY_FILE, 0o600)
        return key
    with open(ADMIN_KEY_FILE, encoding="utf-8") as f:
        return f.read().strip()


def flag_ads(name):
    text = name or ""
    return [t for t in AD_TERMS if t in text]


# ---- 第二搜索源:江江网盘搜索(pan2.xn--9swa.com) ----
# 接口为 SSE 流,结果 url 是加密串,需逐条调 save_url 换明文链接(本站前端同款)。
JJ_BASE = "https://pan2.xn--9swa.com"
JJ_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_alive_cache = {}

JJ_DRIVE_DOMAINS = [
    ("pan.quark.cn", "quark"), ("drive.uc.cn", "uc"),
    ("alipan.com", "ali"), ("aliyundrive.com", "ali"),
    ("pan.baidu.com", "baidu"), ("123pan.com", "123pan"),
    ("123865.com", "123pan"), ("115.com", "115"),
    ("cloud.189.cn", "tianyi"), ("yun.139.com", "yidong"),
    ("xunlei.com", "xunlei"), ("guangyapan.com", "guangya"),
]


def jj_classify(url):
    for dom, drv in JJ_DRIVE_DOMAINS:
        if dom in url:
            return drv
    return ""


def jj_decrypt(enc_url):
    import urllib.request as u, urllib.parse
    body = json.dumps({"url": urllib.parse.quote(enc_url, safe=""),
                       "title": ""}).encode()
    req = u.Request(JJ_BASE + "/api/other/save_url", data=body,
                    headers={**JJ_UA, "Content-Type": "application/json"})
    with u.urlopen(req, timeout=20) as r:
        d = json.loads(r.read().decode("utf-8", "ignore"))
    return (d.get("data") or {}).get("url") or ""


def jj_search(keyword):
    """江江搜索:返回统一结构 [{name, drive, date, links, source}]。"""
    import urllib.request as u, urllib.parse
    from concurrent.futures import ThreadPoolExecutor
    q = urllib.parse.urlencode({"title": keyword})
    req = u.Request(f"{JJ_BASE}/api/other/web_search?{q}", headers=JJ_UA)
    raw = []
    with u.urlopen(req, timeout=90) as r:
        for line in r:
            line = line.decode("utf-8", "ignore").strip()
            if not line.startswith("data:") or "[DONE]" in line:
                continue
            try:
                d = json.loads(line[5:])
            except Exception:
                continue
            if isinstance(d, dict) and d.get("title") and d.get("url"):
                raw.append(d)
            elif isinstance(d, dict) and isinstance(d.get("data"), dict) \
                    and d["data"].get("title"):
                raw.append(d["data"])
    raw = raw[:24]

    def resolve(it):
        try:
            url = jj_decrypt(it["url"])
        except Exception:
            return None
        if not url.startswith("http"):
            return None
        pwd = ""
        m = re.search(r"[?&]pwd=([A-Za-z0-9]+)", url)
        if m:
            pwd = m.group(1)
        return {"name": it.get("title") or "(无标题)",
                "drive": jj_classify(url), "date": "",
                "links": [(url, pwd)], "source": "江江"}

    out = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for item in ex.map(resolve, raw):
            if item:
                out.append(item)
    return out


def check_alive(urls):
    """批量校验夸克链接存活(带 15 分钟缓存)。返回 {url: bool}。"""
    now = time.time()
    todo = [u for u in urls
            if not (u in _alive_cache and now - _alive_cache[u][0] < 900)]
    if todo:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.expanduser(
            "~/workspace/vendor/pylibs:~/workspace/vendor/quarkpan")
        try:
            r = subprocess.run([sys.executable,
                                os.path.join(APP_DIR, "quark_alive.py")],
                               input=json.dumps(todo), capture_output=True,
                               text=True, timeout=120, env=env)
            got = json.loads(r.stdout.strip().splitlines()[-1])
            for u, ok in got.items():
                _alive_cache[u] = (now, bool(ok))
        except Exception:
            pass
    return {u: _alive_cache[u][1] for u in urls if u in _alive_cache}


def do_search(keyword, drive=""):
    cache_key = (keyword, drive)
    hit = _search_cache.get(cache_key)
    if hit and time.time() - hit[0] < 120:
        return hit[1]
    from concurrent.futures import ThreadPoolExecutor
    merged, seen = [], set()

    def from_main():
        total, items = pan_search.search(keyword, limit=50)
        return [{"name": it.get("name"), "drive": it.get("drive") or "",
                 "date": it.get("date") or "",
                 "links": list(it.get("links") or []), "source": "聚合"}
                for it in items]

    def from_jj():
        try:
            return jj_search(keyword)
        except Exception:
            return []

    jj_items = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_main = ex.submit(from_main)
        f_jj = ex.submit(from_jj)
        main_items = f_main.result()
        jj_items = f_jj.result()
    for it in main_items + jj_items:
        if not it["links"]:
            continue
        key = it["links"][0][0].split("?")[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        merged.append(it)
    # 夸克链接存活预检(最多验前 30 条,其余不标)
    quark_urls = [it["links"][0][0] for it in merged
                  if it["drive"] == "quark"][:30]
    alive_map = check_alive(quark_urls) if quark_urls else {}
    out = []
    for it in merged:
        if drive and it["drive"] != drive:
            continue
        flags = flag_ads(it.get("name"))
        first = it["links"][0][0] if it["links"] else ""
        out.append({
            "name": it.get("name") or "(无标题)",
            "drive": it.get("drive") or "",
            "drive_label": DRIVE_LABELS.get(it.get("drive") or "", it.get("drive") or ""),
            "date": it.get("date") or "",
            "links": [{"url": u, "password": p} for u, p in it["links"]],
            "flags": flags,
            "flagged": bool(flags),
            "source": it.get("source") or "",
            "alive": alive_map.get(first) if it["drive"] == "quark" else None,
        })
    result = {"total": len(out), "items": out, "keyword": keyword}
    _search_cache[cache_key] = (time.time(), result)
    return result


def public_task(t):
    return {k: t.get(k) for k in
            ("id", "name", "drive", "url", "requester", "status", "note",
             "flags", "created_at", "updated_at", "share_url")}


def update_task(task_id, **kw):
    with _lock:
        tasks = load_tasks()
        for t in tasks:
            if t["id"] == task_id:
                t.update(kw)
                t["updated_at"] = now_str()
                save_tasks(tasks)
                return t
    return None


def run_watch(args, timeout=1800):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.expanduser(
        "~/workspace/vendor/pylibs:~/workspace/vendor/quarkpan")
    r = subprocess.run([sys.executable, QUARK_WATCH] + args,
                       capture_output=True, text=True, timeout=timeout,
                       cwd=QUARK_DIR, env=env)
    return r.stdout + r.stderr


def snapshot_target(target_fid):
    """目标文件夹直接子项快照 {fid: name};失败返回 {}(不阻断转存)。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.expanduser(
        "~/workspace/vendor/pylibs:~/workspace/vendor/quarkpan")
    try:
        r = subprocess.run([sys.executable,
                            os.path.join(APP_DIR, "quark_ls.py"), target_fid],
                           capture_output=True, text=True, timeout=120, env=env)
        items = json.loads(r.stdout.strip().splitlines()[-1])
        return {it["fid"]: it["name"] for it in items}
    except Exception:
        return {}


def make_share(task):
    """为任务转存的顶层项生成自有分享链接(有效期跟保留期一致,默认2天)。
    返回 share_url 或 None。"""
    stored = task.get("stored_items") or []
    if not stored:
        return None
    cfg = load_app_config()
    days = max(1, round(float(cfg.get("retention_hours") or 48) / 24))
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.expanduser(
        "~/workspace/vendor/pylibs:~/workspace/vendor/quarkpan")
    try:
        r = subprocess.run([sys.executable,
                            os.path.join(APP_DIR, "quark_share.py"),
                            ",".join(it["fid"] for it in stored),
                            task.get("name") or "", str(days)],
                           capture_output=True, text=True, timeout=120, env=env)
        return json.loads(r.stdout.strip().splitlines()[-1]).get("share_url")
    except Exception:
        return None


def worker(task_id):
    """单任务 worker:前置检查 -> dry-run 预览 -> 正式运行(一次只跑一个)。"""
    global _worker_busy
    with _lock:
        if _worker_busy:
            return
        _worker_busy = True
    try:
        tasks = load_tasks()
        task = next((t for t in tasks if t["id"] == task_id), None)
        if not task:
            return
        update_task(task_id, status="checking", note="正在做前置检查")
        drive = task.get("drive")
        if drive == "quark":
            blockers = []
            if not (os.path.exists(QUARK_COOKIE) and os.path.getsize(QUARK_COOKIE) > 0):
                blockers.append("夸克账号尚未登录(缺 cookie.txt,需先扫码登录)")
            cfg = load_app_config()
            target_fid = cfg.get("quark_target_fid") or ""
            if not target_fid:
                blockers.append("尚未配置夸克目标文件夹(quark_target_fid)")
            if blockers:
                update_task(task_id, status="blocked",
                            note=";".join(blockers))
                return
            # 接入新分享:先把任务写进监控 config(排除项按默认广告规则),
            # 再 dry-run 预览,最后正式跑。config 与 baseline 由 watch.py 在
            # 同一次运行里按固定顺序处理,失败文件不会入基线(本地补丁)。
            entry = {"name": task["name"], "share_url": task["url"],
                     "target_fid": target_fid,
                     "exclude": ["海报/", "直接点我", "🔥直接点我"],
                     "dedup": True}
            if task.get("password"):
                entry["password"] = task["password"]
            with _lock:
                conf = []
                if os.path.exists(QUARK_CONFIG):
                    with open(QUARK_CONFIG, encoding="utf-8") as f:
                        conf = json.load(f)
                if not any(c.get("share_url") == task["url"] for c in conf):
                    conf.append(entry)
                    atomic_write_json(QUARK_CONFIG, conf)
            update_task(task_id, status="running", note="dry-run 预览中")
            before = snapshot_target(target_fid)
            preview = run_watch(["--dry-run"], timeout=900)
            update_task(task_id, note="dry-run 预览完成,正式转存中")
            output = run_watch([], timeout=1800)
            tail = (preview + "\n" + output)[-3000:]
            # 记录本次新增到目标文件夹的顶层项,供 48 小时到期清理精确定位
            # (只删这些记录过的 fid,不碰文件夹里原有的任何东西)。
            after = snapshot_target(target_fid)
            stored = [{"fid": fid, "name": name}
                      for fid, name in after.items() if fid not in before]
            t2 = update_task(task_id, status="done_pending_share",
                             note="转存流程已跑完,正在生成分享链接",
                             log_tail=tail, stored_items=stored,
                             completed_ts=int(time.time()))
            share_url = make_share(t2 or {})
            if share_url:
                update_task(task_id, status="done", share_url=share_url,
                            note="转存完成,分享链接已生成;内容保留2天,到期自动清理")
            else:
                update_task(task_id,
                            note="转存完成,但分享链接生成失败(可人工重试);内容保留2天,到期自动清理")
        elif drive == "guangya":
            blockers = []
            if not os.path.exists(os.path.join(GUANGYA_DIR, "refresh_token.txt")):
                blockers.append("光鸭尚未配置 refresh_token")
            if not load_app_config().get("guangya_target_fid"):
                blockers.append("尚未配置光鸭目标文件夹(guangya_target_fid)")
            update_task(task_id, status="blocked",
                        note=";".join(blockers) if blockers else
                        "光鸭任务接入为下一步(本版先跑通夸克链路)")
        else:
            update_task(task_id, status="blocked",
                        note=f"暂只支持夸克/光鸭的自动转存,该链接是 {drive or '未知网盘'},已记录待人工处理")
    except subprocess.TimeoutExpired:
        update_task(task_id, status="failed", note="转存超时,失败文件不会入基线,下轮会重试")
    except Exception as e:
        update_task(task_id, status="failed", note=f"worker 异常: {e}")
    finally:
        _worker_busy = False
        # 若还有排队任务,接着跑下一个
        tasks = load_tasks()
        nxt = next((t for t in tasks if t["status"] == "queued"), None)
        if nxt:
            threading.Thread(target=worker, args=(nxt["id"],), daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = "PanSearch/0.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (now_str(), fmt % args))

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/health":
            return self.send_json({"ok": True, "time": now_str(),
                                   "quark_login": os.path.exists(QUARK_COOKIE)})
        if u.path == "/api/search":
            qs = parse_qs(u.query)
            q = (qs.get("q") or [""])[0].strip()
            drive = (qs.get("drive") or [""])[0].strip()
            if not q:
                return self.send_json({"error": "缺少关键词 q"}, 400)
            try:
                return self.send_json(do_search(q, drive))
            except Exception as e:
                return self.send_json({"error": f"搜索失败: {e}"}, 502)
        if u.path == "/api/tasks":
            with _lock:
                tasks = load_tasks()
            return self.send_json({"tasks": [public_task(t) for t in
                                             sorted(tasks, key=lambda x: x["created_at"], reverse=True)]})
        # 静态文件
        rel = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
        if rel.startswith("static/"):
            rel = rel[len("static/"):]
        path = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not path.startswith(STATIC_DIR) or not os.path.isfile(path):
            return self.send_json({"error": "not found"}, 404)
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}.get(os.path.splitext(path)[1], "text/plain")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/tasks":
            d = self.read_json()
            url = (d.get("url") or "").strip()
            name = (d.get("name") or "").strip()
            drive = (d.get("drive") or "").strip()
            if not url.startswith(("http://", "https://")) or not name:
                return self.send_json({"error": "缺少名称或链接不合法"}, 400)
            with _lock:
                tasks = load_tasks()
                dup = next((t for t in tasks if t["url"] == url and
                            t["status"] not in ("rejected", "failed")), None)
                if dup:
                    return self.send_json({"task": public_task(dup), "duplicated": True})
                task = {"id": uuid.uuid4().hex[:10], "name": name, "drive": drive,
                        "url": url, "password": (d.get("password") or "").strip(),
                        "requester": (d.get("requester") or "").strip(),
                        "flags": d.get("flags") or flag_ads(name),
                        "status": "pending_admin", "note": "等待管理员确认",
                        "share_url": "", "created_at": now_str(), "updated_at": now_str()}
                tasks.append(task)
                save_tasks(tasks)
            return self.send_json({"task": public_task(task)}, 201)
        m = re.match(r"^/api/tasks/([0-9a-f]+)/(approve|reject)$", u.path)
        if m:
            d = self.read_json()
            if (d.get("admin_key") or "") != get_admin_key():
                return self.send_json({"error": "管理员口令不对"}, 403)
            task_id, action = m.group(1), m.group(2)
            if action == "reject":
                t = update_task(task_id, status="rejected", note="管理员已拒绝")
                return self.send_json({"task": public_task(t) if t else None})
            t = update_task(task_id, status="queued", note="已确认,排队等待转存")
            if not t:
                return self.send_json({"error": "任务不存在"}, 404)
            threading.Thread(target=worker, args=(task_id,), daemon=True).start()
            return self.send_json({"task": public_task(t)})
        return self.send_json({"error": "not found"}, 404)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    get_admin_key()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8787"))
    print(f"PanSearch v1 listening on http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
