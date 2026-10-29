#!/usr/bin/env python3
"""Reverse Heaven 170-file transfer (one-off)"""
import sys, json, os, time, re
sys.path.insert(0, '/home/hatch/workspace/vendor/pylibs')
sys.path.insert(0, '/home/hatch/workspace/guangya-api')
from verify import post
from watch import load_bearer

API_DIR = '/home/hatch/workspace/guangya-api'
SHARE_ID = "1927019840765010016_aeWc_JwocgzRgB78"
TARGET_ROOT = "1958907227955953711"
TARGET_S1 = "1958907292850237486"
TARGET_MOVIE = "1958907306884374572"

def short_name_for(name):
    m = re.search(r'S\d+E(\d+)', name, re.I)
    if not m:
        return name
    ext = os.path.splitext(name)[1]
    ep = int(m.group(1))
    width = 3 if ep >= 100 else 2
    return ("%0{}d%s".format(width) % (ep, ext))

def list_page(token, parent_id):
    items, cursor, first = [], 0, True
    while True:
        b = {"pageSize": 20, "accessToken": token, "orderBy": 0, "sortType": 0}
        if parent_id:
            b["parentId"] = parent_id
        if not first:
            b["cursor"] = cursor
        first = False
        d = post("/userres/v1/get_share_page_files_list", b)
        lst = d["data"].get("list", [])
        if not lst:
            break
        items.extend(lst)
        cursor = d["data"].get("cursor", cursor + 20)
        if len(lst) < 20:
            break
        time.sleep(1.0)
    return items

def robust_list(token, parent_id=None, prefix=""):
    out = []
    for attempt in range(5):
        try:
            time.sleep(1.5)
            items = list_page(token, parent_id)
            break
        except Exception as e:
            print("  list retry %d: %s" % (attempt + 1, e), flush=True)
            time.sleep(5 + attempt * 5)
    else:
        return []
    for f in items:
        fid = str(f["fileId"])
        name = f["fileName"]
        if f.get("resType") == 2:
            out.extend(robust_list(token, fid, prefix + name + "/"))
        elif f.get("resType") == 1:
            out.append((fid, prefix + name, f.get("fileSize", 0)))
    return out

def wait_task(bearer, tid, timeout=150):
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(4)
        try:
            s = post("/userres/v1/get_task_status", {"taskId": tid}, bearer=bearer)
            st = s["data"]["status"]
            if st == 2:
                return True
            if st == 3:
                return False
        except Exception as e:
            print("  task poll err: %s" % e, flush=True)
    return False

def find_new_file_id(bearer, target_fid, orig_name):
    time.sleep(1)
    for _ in range(3):
        try:
            r = post("/userres/v1/file/get_file_list",
                     {"pageSize": 200, "parentId": target_fid}, bearer=bearer)
            for f in (r.get("data", {}) or {}).get("list", []):
                if f.get("resType") == 1 and f.get("fileName") == orig_name:
                    return str(f["fileId"])
            time.sleep(2)
        except Exception as e:
            print("  find new file err: %s" % e, flush=True)
            time.sleep(3)
    return None

def main():
    bearer = load_bearer()
    print("bearer OK", flush=True)
    r = post("/userres/v1/get_share_access_token", {"shareId": SHARE_ID})
    token = r["data"]["accessToken"]
    print("share token OK", flush=True)
    print("listing share files...", flush=True)
    files = robust_list(token)
    print("total %d files" % len(files), flush=True)
    jobs = []
    for fid, path, sz in files:
        base = os.path.basename(path)
        if "直接点我" in base or base.startswith("🔥"):
            print("skip ad: %s" % base[:30], flush=True)
            continue
        if "Season 1 1-145/" in path:
            jobs.append((fid, path, sz, TARGET_S1))
        elif "剧场" in path:
            jobs.append((fid, path, sz, TARGET_MOVIE))
        else:
            jobs.append((fid, path, sz, TARGET_ROOT))
    print("to transfer: %d" % len(jobs), flush=True)
    ok, fail, renamed, rename_fail = 0, 0, 0, 0
    saved_fids = []
    for i, (fid, path, sz, target) in enumerate(jobs):
        base = os.path.basename(path)
        print("[%d/%d] %s -> %s" % (i + 1, len(jobs), base[:50], target[-4:]), flush=True)
        try:
            time.sleep(1.5)
            r = post("/userres/v1/restore_share",
                     {"accessToken": token, "fileIds": [fid], "parentId": target},
                     bearer=bearer)
            tid = r.get("data", {}).get("taskId")
            if not tid:
                print("  no taskId: %s" % str(r)[:100], flush=True)
                fail += 1
                continue
            if wait_task(bearer, tid):
                ok += 1
                saved_fids.append(fid)
                short = short_name_for(base)
                if short != base:
                    new_id = find_new_file_id(bearer, target, base)
                    if new_id:
                        try:
                            time.sleep(1)
                            post("/userres/v1/file/rename",
                                 {"fileId": new_id, "newName": short}, bearer=bearer)
                            renamed += 1
                            print("  renamed %s" % short, flush=True)
                        except Exception as e:
                            print("  rename failed: %s" % e, flush=True)
                            rename_fail += 1
                    else:
                        print("  new file not found, skip rename", flush=True)
                        rename_fail += 1
            else:
                print("  transfer failed/timeout", flush=True)
                fail += 1
        except Exception as e:
            print("  error: %s" % e, flush=True)
            fail += 1
        time.sleep(1.5)
    print("DONE: ok=%d fail=%d renamed=%d rename_fail=%d" % (ok, fail, renamed, rename_fail), flush=True)
    state_file = os.path.join(API_DIR, "gy_state.json")
    state = json.load(open(state_file)) if os.path.exists(state_file) else {}
    state[SHARE_ID] = saved_fids
    json.dump(state, open(state_file, "w"))
    print("gy_state.json updated (%d fids)" % len(saved_fids), flush=True)
    cfg_file = os.path.join(API_DIR, "gy_config.json")
    cfg = json.load(open(cfg_file))
    if not any(c.get("share_id") == SHARE_ID for c in cfg):
        cfg.append({"name": "逆-天邪神", "share_id": SHARE_ID,
                    "target_fid": TARGET_ROOT, "strip_prefix": "X.【4K.10bit已更最新集】逆/"})
        json.dump(cfg, open(cfg_file, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("gy_config.json added", flush=True)
    else:
        print("gy_config.json already has it", flush=True)

if __name__ == "__main__":
    main()
