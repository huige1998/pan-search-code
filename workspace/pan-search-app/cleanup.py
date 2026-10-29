#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「自动转存」到期清理(cai 2026-10-29 定:别人搜的转存内容只留 2 天)。

规则:
- 只处理 pan-search 任务记录里 status=done_pending_share、带 completed_ts
  且带 stored_items 的任务;到期 = completed_ts + retention_hours(默认 48)。
- 删除前逐个核验:该 fid 必须仍然是目标文件夹(quark_target_fid)的直接
  子项,才调夸克接口删除;不在目标文件夹里的直接跳过并记录,绝不删别处。
- 删完把任务标记 cleaned(保留 name/url/share_url 等记录),写 cleaned_ts。
- 没有 stored_items 的任务(转存没产生新顶层项)到期只标记,不删任何东西。
- --dry-run 只打印将删什么,不真删、不写任务文件。
cookie 只从 quark-share-watch/cookie.txt 读,不打印、不外传。
"""
import json, os, subprocess, sys, time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DRY = "--dry-run" in sys.argv


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def main():
    cfg = load_json(os.path.join(APP_DIR, "app_config.json"), {})
    retention_h = float(cfg.get("retention_hours") or 48)
    target_fid = cfg.get("quark_target_fid") or ""
    tasks_path = os.path.join(APP_DIR, "data", "tasks.json")
    tasks = load_json(tasks_path, [])
    now = int(time.time())
    due = [t for t in tasks
           if t.get("status") == "done_pending_share"
           and t.get("completed_ts")
           and now - int(t["completed_ts"]) >= retention_h * 3600]
    if not due:
        print("no due tasks")
        return
    if not target_fid:
        print("quark_target_fid not configured; skip")
        return

    # 当前目标文件夹的直接子项(核验删除范围)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.expanduser(
        "~/workspace/vendor/pylibs:~/workspace/vendor/quarkpan")
    r = subprocess.run([sys.executable, os.path.join(APP_DIR, "quark_ls.py"),
                        target_fid], capture_output=True, text=True,
                       timeout=120, env=env)
    children = {it["fid"] for it in json.loads(r.stdout.strip().splitlines()[-1])}

    sys.path.insert(0, os.path.expanduser("~/workspace/vendor/pylibs"))
    sys.path.insert(0, os.path.expanduser("~/workspace/vendor/quarkpan"))
    for _k in ("no_proxy", "NO_PROXY"):
        _v = os.environ.get(_k, "")
        if "[" in _v:
            os.environ[_k] = ",".join(x for x in _v.split(",") if "[" not in x)
    cookie = open(os.path.expanduser("~/workspace/quark-share-watch/cookie.txt"),
                  encoding="utf-8").read().strip()
    from quark_client import create_client
    client = create_client(cookies=cookie, auto_login=False)

    changed = False
    for t in due:
        items = t.get("stored_items") or []
        for it in items:
            fid, name = it.get("fid"), it.get("name")
            if fid not in children:
                print(f"skip (not in target folder): task={t['id']} {name}")
                continue
            print(f"{'[dry-run] would delete' if DRY else 'delete'}: "
                  f"task={t['id']} fid={fid} name={name}")
            if not DRY:
                client.files.delete_files([fid])
        if not DRY:
            t["status"] = "cleaned"
            t["cleaned_ts"] = now
            t["note"] = (t.get("note") or "") + f";已到期清理(保留 {retention_h:g} 小时)"
            changed = True
    if changed:
        tmp = tasks_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
        os.replace(tmp, tasks_path)
        os.chmod(tasks_path, 0o600)
        print("tasks updated")


if __name__ == "__main__":
    main()
