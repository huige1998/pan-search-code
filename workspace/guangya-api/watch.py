#!/usr/bin/env python3
"""
光鸭云盘分享增量转存监控 (对标 quark watch.py)
规则:
1. 只转存分享中新出现的文件
2. 按分享相对子目录归位到对应文件夹 (不存在自动创建)
3. 广告文件 (直接点我/🔥开头) 跳过
4. 视频改短名 (SxxExx; 斗破特例: 纯数字 001 式, config 设 naming=numeric)
5. 查重删重: 仅处理本轮转存涉及的 (子目录, 短名) 分组; 版本冲突留最接近
   同目录同类型文件大小中位数的一版, 删其余; 无参考文件时不动
"""
import sys, json, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify import post, list_all

DRY_RUN = "--dry-run" in sys.argv
_LOCK_FD = None

def acquire_lock():
    """运行锁: 同一脚本目录只允许一个实例,防止定时与手动运行互相覆盖 state/token。"""
    global _LOCK_FD
    import fcntl
    fd = os.open(os.path.join(API_DIR, ".watch.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        print("ERR: 已有另一个 watch.py 实例在运行,本次退出")
        sys.exit(3)
    _LOCK_FD = fd

def write_text_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)

def save_state(state):
    write_text_atomic(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2))

API_DIR = os.path.dirname(os.path.abspath(__file__))
BEARER_FILE = os.path.join(API_DIR, "bearer.txt")
CONFIG_FILE = os.path.join(API_DIR, "gy_config.json")
STATE_FILE = os.path.join(API_DIR, "gy_state.json")

def load_bearer():
    """读取 bearer.txt; 若 JWT 已过期(或 10 分钟内过期)则用 refresh_token 自动续期"""
    bearer = open(BEARER_FILE).read().strip()
    try:
        import base64, time as _t
        payload = bearer.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0)
        if exp - _t.time() > 600:
            return bearer
    except Exception:
        pass
    return refresh_bearer()

def refresh_bearer():
    """用 refresh_token.txt 换新的 access_token,写回 bearer.txt(refresh_token 若更新也写回)"""
    import urllib.request
    rt_file = os.path.join(API_DIR, "refresh_token.txt")
    rt = open(rt_file).read().strip()
    body = json.dumps({"grant_type": "refresh_token", "refresh_token": rt,
                       "client_id": "aMe-8VSlkrbQXpUR"}).encode()
    req = urllib.request.Request("https://account.guangyapan.com/v1/auth/token", data=body,
                                 headers={"Content-Type": "application/json"})
    data = json.load(urllib.request.urlopen(req, timeout=20))
    at = data.get("access_token", "")
    if not at:
        raise RuntimeError(f"续期失败: {str(data)[:200]}")
    write_text_atomic(BEARER_FILE, at)
    nrt = data.get("refresh_token", "")
    if nrt and nrt != rt:
        write_text_atomic(rt_file, nrt)
    print("access_token 已自动续期")
    return at

def shorten_name(name):
    """S01E01.2026.2160p... -> S01E01.mp4 (兼容旧调用)"""
    import re
    m = re.search(r'(S\d+E\d+)', name, re.I)
    if m:
        ext = os.path.splitext(name)[1]
        return m.group(1).upper() + ext
    return name

def short_name_for(cfg, name):
    """按配置返回短名: 默认纯数字 01.mp4(2位,>=100集用3位); 无集数模式原样返回"""
    import re
    m = re.search(r'S\d+E(\d+)', name, re.I)
    if not m:
        return name
    ext = os.path.splitext(name)[1]
    ep = int(m.group(1))
    width = 3 if ep >= 100 else 2
    return ("%0{}d%s".format(width) % (ep, ext))

def get_target_subdir(bearer, parent_id, subdir_name):
    """获取或创建目标子文件夹,返回fid(创建失败时回退到 parent_id,保持旧行为)"""
    r = post("/userres/v1/file/get_file_list",
             {"pageSize": 100, "parentId": parent_id}, bearer=bearer)
    for f in r["data"]["list"]:
        if f.get("resType") == 2 and f["fileName"] == subdir_name:
            return f["fileId"]
    if DRY_RUN:
        print(f"  dry-run 将创建子目录: {subdir_name}")
        return parent_id
    # 与同包 batch_process.py 的 get_or_create_subdir 相同的创建接口
    try:
        post("/userres/v1/file/create_dir",
             {"parentId": parent_id, "dirName": subdir_name}, bearer=bearer)
        time.sleep(1)
        r = post("/userres/v1/file/get_file_list",
                 {"pageSize": 100, "parentId": parent_id}, bearer=bearer)
        for f in r["data"]["list"]:
            if f.get("resType") == 2 and f["fileName"] == subdir_name:
                print(f"  创建子目录: {subdir_name}")
                return f["fileId"]
    except Exception as e:
        print(f"  创建子目录失败,暂存到根目录: {subdir_name} {str(e)[:60]}")
    return parent_id

# 图片后缀(2026-10-27 用户定:图片一律不转存);广告文件夹名关键字(整夹跳过)
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
AD_DIR_KEYS = ("海报", "广告", "直接点我", "点我", "推广", "更多资源")

def check_share(bearer, cfg):
    """检查单个分享,返回新增文件列表"""
    share_id = cfg["share_id"]
    target_fid = cfg["target_fid"]  # 如 伟-大的长-征 (2026) 文件夹id

    # 1. 拿分享token并列文件
    r = post("/userres/v1/get_share_access_token", {"shareId": share_id})
    token = r["data"]["accessToken"]
    files = list_all(token)  # [(fid, path, size)]

    # 2. 读基线
    state = {}
    if os.path.exists(STATE_FILE):
        state = json.load(open(STATE_FILE))
    old = set(state.get(share_id, []))

    # 3. 找新增 (排除广告/图片/广告文件夹)
    new = []
    for fid, path, sz in files:
        base = os.path.basename(path)
        if "直接点我" in base or base.startswith("🔥"):
            continue
        if "海报/" in path:
            continue
        if os.path.splitext(base)[1].lower() in IMAGE_EXTS:
            continue
        if any(k in d for d in path.split("/")[:-1] for k in AD_DIR_KEYS):
            continue
        if fid not in old:
            new.append((fid, path, sz))

    return token, new

def save_files(bearer, share_token, new_files, cfg):
    """转存新文件到对应子目录"""
    target_fid = cfg["target_fid"]
    # 建子目录映射: 分享相对路径 -> 目标fid
    subdir_map = {"": target_fid}

    # 先获取目标文件夹下的子目录
    r = post("/userres/v1/file/get_file_list",
             {"pageSize": 100, "parentId": target_fid}, bearer=bearer)
    for f in r["data"]["list"]:
        if f.get("resType") == 2:
            subdir_map[f["fileName"]] = f["fileId"]

    saved = []
    for fid, path, sz in new_files:
        parts = path.split("/")
        # 去掉顶层文件夹名,取子目录
        # 如 "伟-大的长-征 (2026)/4K.HDR/xxx.mkv" -> subdir="4K.HDR"
        subdir = parts[1] if len(parts) > 2 else ""
        target = subdir_map.get(subdir)
        if target is None and subdir:
            # 新子目录不再静默存到根目录: 先创建再转存,创建失败时
            # get_target_subdir 回退到 target_fid(旧行为)
            target = get_target_subdir(bearer, target_fid, subdir)
            subdir_map[subdir] = target
        elif target is None:
            target = target_fid

        r = post("/userres/v1/restore_share",
                 {"accessToken": share_token, "fileIds": [fid], "parentId": target},
                 bearer=bearer)
        if "data" in r and "taskId" in r["data"]:
            tid = r["data"]["taskId"]
            # 轮询
            for _ in range(30):
                time.sleep(3)
                s = post("/userres/v1/get_task_status", {"taskId": tid}, bearer=bearer)
                st = s["data"]["status"]
                if st == 2:
                    saved.append((fid, path, target))
                    print(f"  + {path}")
                    break
                if st == 3:
                    print(f"  x 失败: {path[:50]}")
                    break
    return saved

def list_target_tree(bearer, folder_id, prefix=""):
    """递归列出目标文件夹,返回 [(rel_path, fid, name, size)]

    注意: 该接口实际不支持分页(返回固定前 200 条),用 seen 去重防
    止 >=200 个文件的目录导致无限循环。"""
    out = []
    seen = set()
    page = 1
    while True:
        time.sleep(0.4)
        try:
            r = post("/userres/v1/file/get_file_list",
                     {"pageSize": 200, "parentId": folder_id}, bearer=bearer)
        except Exception:
            time.sleep(3)
            r = post("/userres/v1/file/get_file_list",
                     {"pageSize": 200, "parentId": folder_id}, bearer=bearer)
        lst = (r.get("data", {}) or {}).get("list", [])
        fresh = [it for it in lst if str(it.get("fileId")) not in seen]
        for it in fresh:
            seen.add(str(it.get("fileId")))
        if not fresh:
            break
        for it in fresh:
            if it.get("resType") == 2:
                out += list_target_tree(bearer, str(it["fileId"]),
                                        prefix + it["fileName"] + "/")
            elif it.get("resType") == 1:
                out.append((prefix + it["fileName"], str(it["fileId"]),
                            it["fileName"], it.get("fileSize", 0)))
        if len(lst) < 200:
            break
        page += 1
        if page > 50:
            break
    return out

def rename_saved(bearer, cfg, saved):
    """把本轮转存的文件改短名。返回本轮涉及的短名集合(用于限定查重范围)。"""
    shorts = set()
    for fid, path, dest_fid in saved:
        base = os.path.basename(path)
        short = short_name_for(cfg, base)
        shorts.add(short)
        if short == base:
            continue
        try:
            r = post("/userres/v1/file/get_file_list",
                     {"pageSize": 200, "parentId": dest_fid}, bearer=bearer)
            for f in (r.get("data", {}) or {}).get("list", []):
                if f.get("resType") == 1 and f.get("fileName") == base:
                    try:
                        if DRY_RUN:
                            print(f"  dry-run 将改名: {base[:45]} -> {short}")
                            break
                        post("/userres/v1/file/rename",
                             {"fileId": f["fileId"], "newName": short},
                             bearer=bearer)
                        print(f"  改名: {base[:45]} -> {short}")
                    except Exception as e:
                        print(f"  改名失败(留待查重): {short} {str(e)[:60]}")
                    time.sleep(0.8)
                    break
        except Exception as e:
            print(f"  列目录失败: {str(e)[:60]}")
        time.sleep(0.5)
    return shorts

def dedup_conflicts(bearer, cfg, shorts):
    """查重删重: 只处理本轮涉及的 (子目录, 短名) 分组。
    版本冲突 -> 留最接近同目录同类型文件大小中位数的一版, 删其余;
    无参考文件时不动。"""
    from statistics import median
    tree = list_target_tree(bearer, cfg["target_fid"])
    groups = {}
    for rel, fid, name, size in tree:
        key = (os.path.dirname(rel), short_name_for(cfg, name))
        groups.setdefault(key, []).append((fid, name, size, rel))
    for (subdir, short), flist in sorted(groups.items()):
        if len(flist) < 2 or short not in shorts:
            continue
        ext = os.path.splitext(short)[1].lower()
        ref = [s for (rp, rf, rn, s) in tree
               if os.path.dirname(rp) == subdir
               and os.path.splitext(rn)[1].lower() == ext
               and short_name_for(cfg, rn) != short and s]
        if not ref:
            print(f"  查重 {short}: 无参考文件，不动")
            continue
        med = median(ref)
        flist.sort(key=lambda x: abs((x[2] or 0) - med))
        keep = flist[0]
        print(f"  查重 {short}: 中位数 {med/1e9:.2f}GB，"
              f"留 {keep[1][:45]} ({(keep[2] or 0)/1e9:.2f}GB)")
        for fid, name, size, rel in flist[1:]:
            try:
                if DRY_RUN:
                    print(f"    dry-run 将删: {rel[:65]}")
                    continue
                post("/userres/v1/file/delete_file", {"fileIds": [fid]},
                     bearer=bearer)
                print(f"    删: {rel[:65]}")
                time.sleep(0.8)
            except Exception as e:
                print(f"    删失败: {str(e)[:60]}")

def main():
    acquire_lock()
    if DRY_RUN:
        print("dry-run 模式: 不转存、不改名、不删除、不写入 gy_state.json,只预览")
    bearer = load_bearer()
    if not os.path.exists(CONFIG_FILE):
        print(f"无配置文件: {CONFIG_FILE}")
        return

    cfg_list = json.load(open(CONFIG_FILE))
    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}

    for cfg in cfg_list:
        name = cfg["name"]
        print(f"--- 检查: {name}")
        try:
            token, new = check_share(bearer, cfg)
            print(f"    分享新增 {len(new)} 个")
            if new and DRY_RUN:
                for _fid, _path, _sz in new:
                    print(f"    dry-run 预览: 将转存 {_path}")
            elif new:
                saved = save_files(bearer, token, new, cfg)
                print(f"    转存成功 {len(saved)} 个")
                if saved:
                    shorts = rename_saved(bearer, cfg, saved)
                    dedup_conflicts(bearer, cfg, shorts)
                # 更新基线: 只把本轮转存成功的文件并入旧基线;
                # 失败/未转存的不要入基线,留到下一轮重试(旧代码会把分享全部文件写入基线)。
                saved_fids = {fid for fid, _p, _t in saved}
                state[cfg["share_id"]] = sorted(
                    set(state.get(cfg["share_id"], [])) | saved_fids, key=str)
                for _fid, _path, _sz in new:
                    if _fid not in saved_fids:
                        print(f"    未入基线(转存失败,下一轮重试): {_path}")
        except Exception as e:
            print(f"    ERR: {e}")

    if DRY_RUN:
        print("dry-run 完成: 未写入 gy_state.json")
    else:
        save_state(state)

if __name__ == "__main__":
    main()
