#!/usr/bin/env python3
import sys, time, os, re, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify import post, list_all

bearer = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bearer.txt")).read().strip()
API_DIR = os.path.dirname(os.path.abspath(__file__))

def shorten(name):
    m = re.search(r'(S\d+E\d+)', name, re.I)
    if m:
        return m.group(1).upper() + os.path.splitext(name)[1]
    return name

def list_target(pid, prefix="", depth=0):
    if depth > 4: return {}
    time.sleep(0.4)
    try:
        r = post("/userres/v1/file/get_file_list", {"pageSize": 100, "parentId": pid}, bearer=bearer)
    except:
        time.sleep(3)
        r = post("/userres/v1/file/get_file_list", {"pageSize": 100, "parentId": pid}, bearer=bearer)
    out = {}
    for f in r["data"]["list"]:
        p = prefix + f["fileName"]
        if f.get("resType") == 2:
            out.update(list_target(f["fileId"], p + "/", depth+1))
        else:
            out[p] = (f["fileId"], f.get("fileSize", 0))
    return out

def get_or_create_subdir(parent_id, name):
    time.sleep(0.4)
    r = post("/userres/v1/file/get_file_list", {"pageSize": 100, "parentId": parent_id}, bearer=bearer)
    for f in r["data"]["list"]:
        if f.get("resType") == 2 and f["fileName"] == name:
            return f["fileId"]
    r = post("/userres/v1/file/create_dir", {"parentId": parent_id, "dirName": name}, bearer=bearer)
    time.sleep(1)
    r = post("/userres/v1/file/get_file_list", {"pageSize": 100, "parentId": parent_id}, bearer=bearer)
    for f in r["data"]["list"]:
        if f.get("resType") == 2 and f["fileName"] == name:
            return f["fileId"]
    return parent_id

def wait_task(tid, timeout=120):
    for _ in range(timeout // 3):
        time.sleep(3)
        try:
            s = post("/userres/v1/get_task_status", {"taskId": tid}, bearer=bearer)
            st = s["data"]["status"]
            if st == 2: return True
            if st == 3: return False
        except:
            time.sleep(2)
    return False

def process_share(name, sid, target_fid):
    print(f"\n=== {name} ===", flush=True)
    # 1. 分享文件
    r = post("/userres/v1/get_share_access_token", {"shareId": sid})
    token = r["data"]["accessToken"]
    files = list_all(token)
    print(f"分享 {len(files)} 文件", flush=True)
    # 2. 已有文件
    have = list_target(target_fid)
    have_names = {os.path.basename(p) for p in have}
    have_shorts = {shorten(os.path.basename(p)) for p in have}
    print(f"已有 {len(have)} 文件", flush=True)
    # 3. 找新增
    new = []
    for fid, p, sz in files:
        base = os.path.basename(p)
        if "直接点我" in base or base.startswith("🔥") or "海报/" in p:
            continue
        if base not in have_names and shorten(base) not in have_shorts:
            rel = "/".join(p.split("/")[1:])
            new.append((fid, rel, sz))
    print(f"新增 {len(new)} 个", flush=True)
    if not new:
        return {"saved": 0, "renamed": 0, "dedup": 0, "total": len(files)}
    # 4. 按子目录分组批量转存
    from collections import defaultdict
    by_dir = defaultdict(list)
    for fid, rel, sz in new:
        by_dir[os.path.dirname(rel)].append((fid, rel, sz))
    # 建子目录映射
    time.sleep(0.4)
    r = post("/userres/v1/file/get_file_list", {"pageSize": 100, "parentId": target_fid}, bearer=bearer)
    subdir_map = {}
    for f in r["data"]["list"]:
        if f.get("resType") == 2:
            subdir_map[f["fileName"]] = f["fileId"]
    saved = 0
    saved_ids = []  # (new_fid, short_name) for rename
    for d, flist in sorted(by_dir.items()):
        tgt = subdir_map.get(d) if d else target_fid
        if d and d not in subdir_map:
            tgt = get_or_create_subdir(target_fid, d)
            subdir_map[d] = tgt
            print(f"  创建子目录: {d}", flush=True)
        fids = [f for f, _, _ in flist]
        # 分批,每批10个
        for i in range(0, len(fids), 10):
            batch = fids[i:i+10]
            try:
                r = post("/userres/v1/restore_share",
                         {"accessToken": token, "fileIds": batch, "parentId": tgt},
                         bearer=bearer)
                tid = r["data"]["taskId"]
                if wait_task(tid):
                    saved += len(batch)
                    print(f"  + {d or '(根)'}: {len(batch)}个成功", flush=True)
                else:
                    print(f"  x {d or '(根)'}: {len(batch)}个失败", flush=True)
            except Exception as e:
                print(f"  x ERR: {str(e)[:50]}", flush=True)
            time.sleep(1)
    # 5. 改名:查目标目录新文件并改短名
    time.sleep(2)
    have2 = list_target(target_fid)
    renamed = 0
    for p, (fid2, sz2) in have2.items():
        base = os.path.basename(p)
        short = shorten(base)
        if short != base:
            # 检查是否本次新增的
            if base in [os.path.basename(rel) for _, rel, _ in new]:
                try:
                    post("/userres/v1/file/rename", {"fileId": fid2, "newName": short}, bearer=bearer)
                    renamed += 1
                    time.sleep(0.8)
                except:
                    pass
    print(f"改名 {renamed} 个", flush=True)
    # 6. 查重删重
    time.sleep(1)
    have3 = list_target(target_fid)
    by_short = defaultdict(list)
    for p, (fid2, sz2) in have3.items():
        by_short[shorten(os.path.basename(p))].append((fid2, os.path.basename(p), p))
    dedup = 0
    for short, flist in by_short.items():
        if len(flist) > 1:
            keep = None
            for fid2, base2, p2 in flist:
                if base2 == short:
                    keep = fid2
                    break
            if not keep:
                keep = flist[0][0]
            for fid2, base2, p2 in flist:
                if fid2 != keep:
                    try:
                        post("/userres/v1/file/delete_file", {"fileIds": [fid2]}, bearer=bearer)
                        print(f"  删重复: {p2[:50]}", flush=True)
                        dedup += 1
                        time.sleep(0.8)
                    except:
                        pass
    print(f"删重复 {dedup} 个", flush=True)
    return {"saved": saved, "renamed": renamed, "dedup": dedup, "total": len(files)}

shares = [
    ("斗-破苍穹 年番 4K", "1927025326025396263_aeWc_JwocgzRgB78", "1956012219942637651"),
    ("蒸-蒸日-上", "1956007221670182960_aerDgoULFG4aU1H0", "1956012830692012123"),
    ("醉梦", "1958151616523821113_aerDgoULFG4aU1H0", "1958160507181543437"),
    ("触摸之神奇宝贝第二季", "1952703292584476681_aerDgoULFG4aU1H0", "1953105867989151773"),
    ("美人余", "1955539662529364007_aerDgoULFG4aU1H0", "1956347196072833083"),
]

results = {}
# 更新配置
cfg_path = os.path.join(API_DIR, "gy_config.json")
state_path = os.path.join(API_DIR, "gy_state.json")
cfg = json.load(open(cfg_path)) if os.path.exists(cfg_path) else []
state = json.load(open(state_path)) if os.path.exists(state_path) else {}
existing_sids = {c["share_id"] for c in cfg}

for name, sid, tfid in shares:
    try:
        results[name] = process_share(name, sid, tfid)
        # 加监控
        if sid not in existing_sids:
            cfg.append({"name": name, "share_id": sid, "target_fid": tfid})
        # 建基线
        r = post("/userres/v1/get_share_access_token", {"shareId": sid})
        tok = r["data"]["accessToken"]
        fl = list_all(tok)
        state[sid] = [f for f, _, _ in fl]
        time.sleep(1)
    except Exception as e:
        print(f"!!! {name} 失败: {e}", flush=True)
        results[name] = {"error": str(e)[:80]}

json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
json.dump(state, open(state_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
os.chmod(cfg_path, 0o600); os.chmod(state_path, 0o600)

print("\n===== 汇总 =====")
for n, r in results.items():
    print(f"{n}: {r}")
