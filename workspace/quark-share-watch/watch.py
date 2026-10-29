#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
夸克分享链接监控 + 增量自动转存 + 查重删重 + 错位纠正
- 轮询 config.json 里的分享链接,递归列出分享内全部文件
- 与 state.json 比对,只转存新增文件
- 新增文件按分享内的相对目录结构,转存到目标文件夹对应子目录(不存在则自动创建),并改短名
- 查重: 目标文件夹内与分享文件 (文件名+大小) 完全相同的重复项,只保留位置正确的,删多余
- 版本冲突(用户规则 2026-10-18): 同一集多个不同大小的版本,与同目录其他同类型文件
  比大小,留最接近大小中位数的一版;无参考文件时不动,留待人工处理
- 跨格式去重(用户规则 2026-10-19 嫁金钗事件): 同一集在同一目录有多个扩展名版本
  (如 09.mkv vs 09.mp4)时,不看大小新旧,留该目录中数量最多的格式,删其余;
  目录内票数持平看整个目标目录,还持平则不动,留待人工处理
- 纠错: 与分享文件 (文件名+大小) 匹配但放错子目录的,移动到正确目录
- 无法匹配分享的文件一律不动(可能是用户自己的文件)
- 跟删(可选,默认关闭): 分享里连续多轮消失的文件,从目标目录删除(只删本管线转存过的);
  分享读取异常/被清空时不删,连续缺失 MISS_GRACE 轮才删,批量超阈值转人工确认,支持 preview 预览模式
- Cookie 从 cookie.txt 读取(扫码登录获取,不进聊天/仓库)
"""
import json, os, re, sys

DRY_RUN = "--dry-run" in sys.argv
_LOCK_FD = None

def acquire_lock():
    """运行锁: 同一脚本目录只允许一个实例,防止定时与手动运行互相覆盖 state/token。"""
    global _LOCK_FD
    import fcntl
    fd = os.open(os.path.join(BASE, ".watch.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        print("ERR: 已有另一个 watch.py 实例在运行,本次退出")
        sys.exit(3)
    _LOCK_FD = fd

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.expanduser("~/workspace/vendor/quarkpan"))
sys.path.insert(0, os.path.expanduser("~/workspace/vendor/pylibs"))
for _k in ("no_proxy", "NO_PROXY"):
    _v = os.environ.get(_k, "")
    if "[" in _v:
        os.environ[_k] = ",".join(x for x in _v.split(",") if "[" not in x)

def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default

def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    os.chmod(path, 0o600)

def delete_files(client, fids):
    if DRY_RUN:
        return {"code": 0}
    return client.files.delete_files(fids)

def move_files(client, fids, dest):
    if DRY_RUN:
        return {"code": 0}
    return client.files.move_files(fids, dest)

def rename_file(client, fid, new_name):
    if DRY_RUN:
        return {"code": 0}
    return client.files.rename_file(fid, new_name)

def shorten_name(name):
    """S01E01.2026.2160p.60fps.WEB-DL...mp4 -> 01.mp4; 纯数字2位零填充, 无集数模式则原样返回"""
    m = re.search(r"[Ss]\d{1,2}[Ee](\d{1,3})", name)
    if m:
        return f"{int(m.group(1)):02d}" + os.path.splitext(name)[1]
    return name

def fmt_size(n):
    n = n or 0
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.2f}{u}" if u != "B" else f"{int(n)}B"
        n /= 1024

def list_share_page(shares, share_id, token, pdir_fid="0", page=1, size=50):
    """手动翻页获取分享目录单页(库的 get_share_info 写死 page=1)。"""
    from quark_client.config import Config
    params = {"pwd_id": share_id, "stoken": token, "pdir_fid": pdir_fid, "force": "0",
              "_page": page, "_size": size, "_fetch_total": "1",
              "_sort": "file_type:asc,file_name:asc"}
    r = shares.client.get("share/sharepage/detail", params=params, base_url=Config.SHARE_BASE_URL)
    if isinstance(r, dict):
        return r.get("data", {}).get("list", [])
    return []

def list_share_files(shares, share_id, token, pdir_fid="0", prefix=""):
    """递归列出分享内所有文件,返回 {fid: (显示路径, size, share_fid_token)}。手动翻页,无50条限制。"""
    out = {}
    page = 1
    while True:
        lst = list_share_page(shares, share_id, token, pdir_fid, page)
        for item in lst:
            fid = str(item.get("fid", ""))
            name = item.get("file_name", "")
            size = item.get("size", 0)
            ftoken = item.get("share_fid_token", "")
            is_dir = item.get("dir", False) or item.get("file_type") == 0
            disp = f"{prefix}{name}"
            if is_dir:
                out.update(list_share_files(shares, share_id, token, pdir_fid=fid, prefix=disp + "/"))
            else:
                out[fid] = (disp, size, ftoken)
        if len(lst) < 50:
            break
        page += 1
    return out

def save_share_files_fixed(shares, share_id, token, file_ids, id_tokens, target_fid, timeout=300):
    """修复版转存: 库的 save_shared_files 有 bug(fid_token_list 传空数组导致 403),
    这里手动带上每个文件的 share_fid_token。返回 (成功 fid 列表, 失败 fid 列表),
    调用方只能把成功列表写入基线,失败的留到下一轮重试。"""
    import time as _time
    from quark_client.config import Config
    ok_ids, fail_ids = [], []
    # 分批(每批最多50个)
    for i in range(0, len(file_ids), 50):
        batch_ids = file_ids[i:i+50]
        batch_tokens = id_tokens[i:i+50]
        data = {
            "fid_list": batch_ids,
            "fid_token_list": batch_tokens,
            "to_pdir_fid": target_fid,
            "pwd_id": share_id,
            "stoken": token,
            "pdir_fid": "0",
            "scene": "link",
        }
        try:
            resp = shares.client.post("share/sharepage/save", json_data=data,
                                      base_url=Config.SHARE_BASE_URL)
        except Exception as e:
            print(f"    转存请求失败: {e}")
            fail_ids.extend(batch_ids)
            continue
        if not isinstance(resp, dict) or resp.get("status") != 200 or resp.get("code") != 0:
            print(f"    转存失败: {str(resp)[:200]}")
            fail_ids.extend(batch_ids)
            continue
        task_id = (resp.get("data") or {}).get("task_id")
        if not task_id:
            ok_ids.extend(batch_ids)
            continue
        # 等待任务完成(status 2=成功,兼容字符串"finish")
        done, start = False, _time.time()
        while _time.time() - start < timeout:
            try:
                tr = shares.client.get("task", params={"task_id": task_id, "retry_index": 0})
                td = (tr.get("data") or {}) if isinstance(tr, dict) else {}
                st = td.get("status")
                if st == "finish" or st == 2 or td.get("finished_at"):
                    done = True
                    break
            except Exception:
                pass
            _time.sleep(2)
        if done:
            ok_ids.extend(batch_ids)
        else:
            print(f"    转存任务超时: {task_id}")
            fail_ids.extend(batch_ids)
    return ok_ids, fail_ids

def ensure_subfolder(client, parent_fid, sub_path):
    """确保 parent_fid 下存在 sub_path(如 '高码率'),不存在则逐级创建,返回最深层 fid。"""
    cur_fid = parent_fid
    for part in [p for p in sub_path.split("/") if p]:
        res = client.files.list_files(folder_id=cur_fid, page=1, size=200)
        found = None
        for it in (res.get("data", {}) or {}).get("list", []):
            if it.get("dir") and it.get("file_name") == part:
                found = str(it.get("fid"))
                break
        if found:
            cur_fid = found
        elif DRY_RUN:
            print(f"    dry-run 预览: 将创建子目录: {part}")
        else:
            cr = client.files.create_folder(part, parent_id=cur_fid)
            cur_fid = str((cr.get("data", {}) or {}).get("fid") or cr.get("fid"))
            print(f"    创建子目录: {part} -> {cur_fid}")
    return cur_fid

def list_target_tree(client, folder_id, prefix=""):
    """递归列出目标文件夹,返回 [{fid,name,size,rel_dir}]。rel_dir 为相对目标根的目录(''表示根)。"""
    out = []
    res = client.files.list_files(folder_id=folder_id, page=1, size=200)
    for it in (res.get("data", {}) or {}).get("list", []):
        fid = str(it.get("fid", ""))
        name = it.get("file_name", "")
        if it.get("dir"):
            out += list_target_tree(client, fid, prefix + name + "/")
        else:
            out.append({"fid": fid, "name": name, "size": it.get("size", 0),
                        "rel_dir": prefix.rstrip("/")})
    return out

def rel_of(w, path):
    """分享内相对路径:strip_first_dir=true 时剥掉第一层目录(顶层文件夹名含集数、会变);否则按 strip_prefix 剥。"""
    if w.get("strip_first_dir"):
        return path.split("/", 1)[1] if "/" in path else path
    sp = w.get("strip_prefix", "")
    return path[len(sp):] if sp and path.startswith(sp) else path


# 图片后缀(2026-10-27 用户定:图片一律不转存);广告文件夹名关键字(整夹跳过)
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
AD_DIR_KEYS = ("海报", "广告", "直接点我", "点我", "推广", "更多资源")

def is_excluded(w, rel_path):
    """相对路径命中 exclude 前缀则排除(如 海报/)；文件名以 直接点我/🔥直接点我 开头视为广告自动跳过；图片后缀不转存；目录名含广告关键字则整夹跳过"""
    name = rel_path.rsplit("/", 1)[-1]
    if name.startswith("直接点我") or name.startswith("🔥直接点我"):
        return True
    if os.path.splitext(name)[1].lower() in IMAGE_EXTS:
        return True
    for d in rel_path.split("/")[:-1]:
        if any(k in d for k in AD_DIR_KEYS):
            return True
    for ex in w.get("exclude", []):
        if rel_path == ex.rstrip("/") or rel_path.startswith(ex):
            return True
    return False

def expected_subdir(w, share_subdir, size):
    """期望子目录:配置了 dir_by_size 则按文件大小分目录,否则沿用分享原目录。"""
    rule = w.get("dir_by_size") or {}
    if not rule:
        return share_subdir
    thr = int(rule.get("threshold_bytes", 3 * 1024**3))
    return rule.get("big_dir", "高码率") if (size or 0) >= thr else rule.get("small_dir", "普通码率")

def dedup_and_fix(client, w, cur):
    """查重删重 + 错位纠正。cur: {share_fid: (显示路径, size)}。返回 (删除数, 移动数, 明细)。"""
    target_fid = w.get("target_fid", "0")
    strip_prefix = w.get("strip_prefix", "")
    # 分享索引: (短文件名, size) -> 期望相对目录
    share_idx = {}
    for fid, (path, size, _ft) in cur.items():
        rel = rel_of(w, path)
        if is_excluded(w, rel):
            continue
        parts = rel.split("/")
        short = shorten_name(parts[-1])
        exp_dir = expected_subdir(w, "/".join(parts[:-1]), size)
        share_idx[(short, size)] = exp_dir
    tree = list_target_tree(client, target_fid)
    # 只处理能匹配到分享的文件(用短文件名匹配,能识别长名/(1)后缀的重复项)
    matched = [t for t in tree if (shorten_name(t["name"]), t["size"]) in share_idx]
    # 统一换算成短名 key,方便后续分组
    for t in matched:
        t["_key"] = (shorten_name(t["name"]), t["size"])
    details, del_count, move_count = [], 0, 0
    # 1. 查重: 同 (name,size) 多个 -> 保留位置正确的,删其余
    groups = {}
    for t in matched:
        groups.setdefault(t["_key"], []).append(t)
    deleted_fids = set()
    for key, items in groups.items():
        if len(items) < 2:
            continue
        exp_dir = share_idx[key]
        def _rank(t):
            short_ok = 1 if t["name"] == shorten_name(t["name"]) else 0
            dir_ok = 1 if t["rel_dir"] == exp_dir else 0
            return (dir_ok, short_ok)
        keep = max(items, key=_rank)
        for t in items:
            if t["fid"] == keep["fid"]:
                continue
            r = delete_files(client, [t["fid"]])
            ok = isinstance(r, dict) and r.get("code", 0) == 0
            loc = f"{t['rel_dir']}/" if t["rel_dir"] else ""
            details.append(f"删除重复: {loc}{t['name']} -> {'OK' if ok else 'FAIL'}")
            if ok:
                del_count += 1
                deleted_fids.add(t["fid"])
    # 1b. 版本冲突: 同短名不同大小(用户规则 2026-10-18):
    #   与同目录下其他同类型文件比大小,留最接近大小中位数的一版,删其余。
    #   无参考文件时不动,留待人工处理。
    share_shorts = set()
    for _fid, (_path, _size, _ft) in cur.items():
        _rel = rel_of(w, _path)
        if is_excluded(w, _rel):
            continue
        share_shorts.add(shorten_name(_rel.split("/")[-1]))
    vg = {}
    for t in tree:
        if t["fid"] in deleted_fids:
            continue
        _short = shorten_name(t["name"])
        if _short not in share_shorts:
            continue
        vg.setdefault((t["rel_dir"], _short), []).append(t)
    for (rel_dir, short), vitems in vg.items():
        if len({t["size"] for t in vitems}) < 2:
            continue
        member_fids = {t["fid"] for t in vitems}
        ext = os.path.splitext(short)[1].lower()
        ref = sorted(t["size"] for t in tree
                     if t["rel_dir"] == rel_dir and t["fid"] not in member_fids
                     and os.path.splitext(t["name"])[1].lower() == ext
                     and (t["size"] or 0) > 0)
        loc = f"{rel_dir}/" if rel_dir else ""
        if not ref:
            details.append(f"版本冲突待定: {loc}{short} 有 {len(vitems)} 个不同大小的版本,目录无同类型参考文件,需人工处理")
            continue
        median = ref[len(ref) // 2]
        def _vrank(t):
            matched = 1 if (shorten_name(t["name"]), t["size"]) in share_idx else 0
            short_ok = 1 if t["name"] == shorten_name(t["name"]) else 0
            return (-abs((t["size"] or 0) - median), matched, short_ok)
        keep = max(vitems, key=_vrank)
        for t in vitems:
            if t["fid"] == keep["fid"]:
                continue
            r = delete_files(client, [t["fid"]])
            ok = isinstance(r, dict) and r.get("code", 0) == 0
            details.append(f"版本冲突删: {loc}{t['name']}({fmt_size(t['size'])}) "
                           f"留 {keep['name']}({fmt_size(keep['size'])},目录中位数{fmt_size(median)}) -> {'OK' if ok else 'FAIL'}")
            if ok:
                del_count += 1
                deleted_fids.add(t["fid"])
        if keep["name"] != short and keep["fid"] not in deleted_fids:
            try:
                rename_file(client, str(keep["fid"]), short)
                details.append(f"版本冲突改名: {loc}{keep['name']} -> {short}")
            except Exception as ve:
                details.append(f"版本冲突改名失败 {loc}{keep['name']}: {ve}")
    # 1b2. 跨格式去重 (用户规则 2026-10-19 嫁金钗事件):
    #   同一集在同一目录下有多个扩展名版本(如 09.mkv vs 09.mp4)时, 不看大小新旧,
    #   留该目录中数量最多的格式, 删其余。目录内票数持平看整个目标目录, 还持平则不动。
    share_bases = set()
    for _fid, (_path, _size, _ft) in cur.items():
        _rel = rel_of(w, _path)
        if is_excluded(w, _rel):
            continue
        share_bases.add(os.path.splitext(shorten_name(_rel.split("/")[-1]))[0].lower())
    fmt_groups = {}
    for t in tree:
        if t["fid"] in deleted_fids:
            continue
        base = os.path.splitext(shorten_name(t["name"]))[0].lower()
        if base not in share_bases:
            continue
        fmt_groups.setdefault((t["rel_dir"], base), []).append(t)
    for (rel_dir, base), fitems in fmt_groups.items():
        exts = {os.path.splitext(shorten_name(t["name"]))[1].lower() for t in fitems}
        if len(exts) < 2:
            continue
        if ".zip" in exts:
            continue  # zip 是防和谐备份, 与视频同集共存不删 (用户规则 2026-10-20)
        def _ext_count(ext, same_dir):
            return sum(1 for t in tree
                       if t["fid"] not in deleted_fids
                       and (not same_dir or t["rel_dir"] == rel_dir)
                       and os.path.splitext(shorten_name(t["name"]))[1].lower() == ext)
        counts_dir = {e: _ext_count(e, True) for e in sorted(exts)}
        counts = counts_dir
        if len(set(counts_dir.values())) == 1:
            # 目录内票数持平, 扩大到整个目标目录再比一次
            counts = {e: _ext_count(e, False) for e in sorted(exts)}
        loc = f"{rel_dir}/" if rel_dir else ""
        best = max(counts.values())
        winners = [e for e, c in counts.items() if c == best]
        if len(winners) != 1:
            details.append(f"跨格式待定: {loc}{base} 格式票数持平{counts}, 需人工处理")
            continue
        keep_ext = winners[0]
        for t in fitems:
            te = os.path.splitext(shorten_name(t["name"]))[1].lower()
            if te == keep_ext:
                continue
            r = delete_files(client, [t["fid"]])
            ok = isinstance(r, dict) and r.get("code", 0) == 0
            details.append(f"跨格式删: {loc}{t['name']} (留{keep_ext or '(无扩展名)'}格式, "
                           f"票数{counts[keep_ext]}>{counts[te]}) -> {'OK' if ok else 'FAIL'}")
            if ok:
                del_count += 1
                deleted_fids.add(t["fid"])
    # 1c. 统一命名 (用户规则 2026-10-18): 转存后把目标目录下所有旧 SxxExx 命名改成纯数字,
    #   与新转存文件的 01.mp4 格式统一。目标名已存在时跳过,避免冲突。
    import re as _re
    existing_names = {t["name"] for t in tree if t["fid"] not in deleted_fids}
    for t in tree:
        if t["fid"] in deleted_fids:
            continue
        m = _re.match(r'^[Ss]\d{1,2}[Ee](\d{1,3})(\.[^.]+)$', t["name"])
        if not m:
            continue
        ep = int(m.group(1))
        new = "%02d%s" % (ep, m.group(2)) if ep < 100 else "%03d%s" % (ep, m.group(2))
        if new == t["name"] or new in existing_names:
            continue
        try:
            rename_file(client, str(t["fid"]), new)
            loc = f"{t['rel_dir']}/" if t["rel_dir"] else ""
            details.append(f"统一命名: {loc}{t['name']} -> {new}")
            existing_names.discard(t["name"])
            existing_names.add(new)
            t["name"] = new
        except Exception as ue:
            details.append(f"统一命名失败 {t['name']}: {ue}")
    # 2. 纠错: 位置不对的移动到正确目录(跳过刚删除的)
    for t in matched:
        if t["fid"] in deleted_fids:
            continue
        exp_dir = share_idx[t["_key"]]
        if t["rel_dir"] == exp_dir:
            continue
        dest = ensure_subfolder(client, target_fid, exp_dir) if exp_dir else target_fid
        r = move_files(client, [t["fid"]], dest)
        ok = isinstance(r, dict) and r.get("code", 0) == 0
        src = f"{t['rel_dir']}/" if t["rel_dir"] else "(根)/"
        dst = f"{exp_dir}/" if exp_dir else "(根)/"
        details.append(f"移动纠错: {src}{t['name']} -> {dst} {'OK' if ok else 'FAIL'}")
        if ok:
            move_count += 1
    return del_count, move_count, details

# ---------- 跟删(镜像同步的子集): 分享里消失的文件 -> 目标目录删除 ----------
MISS_GRACE = 2            # 连续缺失多少轮才删
MAX_AUTO_DELETE = 10      # 单轮自动删除上限(个数)
MAX_AUTO_DELETE_PCT = 0.3  # 单轮自动删除上限(占管理文件比例)

def _managed(state, share_id):
    """本管线转存过的文件清单: {share_fid: {tfid, rel, miss}}。只删这里面的文件。"""
    bucket = state.setdefault("_managed", {})
    m = bucket.get(share_id)
    if m is None:
        m = {}
        bucket[share_id] = m
    return m

def sync_manifest(client, w, share_id, cur, new_fids, state):
    """同步 manifest: 新转存的按(短名,大小)定位目标文件记入; 存量条目刷新 rel,
    目标已不存在的条目清理(被查重/版本规则删掉或手动删掉)。返回 (managed, tree)。"""
    managed = _managed(state, share_id)
    strip_prefix = w.get("strip_prefix", "")
    target_fid = w.get("target_fid", "0")
    tree = list_target_tree(client, target_fid)
    # 新转存的记入 manifest
    share_idx = {}
    for fid in new_fids:
        path, size, _ft = cur[fid]
        rel = rel_of(w, path)
        if is_excluded(w, rel):
            continue
        share_idx[(shorten_name(rel.split("/")[-1]), size)] = fid
    for t in tree:
        key = (shorten_name(t["name"]), t["size"])
        if key in share_idx:
            sfid = share_idx[key]
            rel = (t["rel_dir"] + "/" if t["rel_dir"] else "") + t["name"]
            e = managed.get(sfid, {})
            e.update({"tfid": t["fid"], "rel": rel, "miss": 0})
            managed[sfid] = e
    # 存量条目: 刷新 rel / 清理目标已消失的
    by_fid = {t["fid"]: t for t in tree}
    for sfid in list(managed.keys()):
        e = managed[sfid]
        t = by_fid.get(e.get("tfid"))
        if t:
            e["rel"] = (t["rel_dir"] + "/" if t["rel_dir"] else "") + t["name"]
        else:
            managed.pop(sfid, None)
    return managed, tree

def follow_delete(client, w, share_id, cur, managed, tree, dry_preview):
    """跟删执行。返回 (删除数, 明细)。
    安全规则: 分享列表为空(读取异常/被清空)不删; 连续缺失 MISS_GRACE 轮才删;
    单轮超量转人工确认; 只删 manifest 里的文件。"""
    details = []
    if not cur:
        details.append("跟删跳过: 本轮分享文件列表为空(疑似读取异常或被清空),不删除任何文件")
        return 0, details
    for sfid, e in managed.items():
        e["miss"] = 0 if sfid in cur else e.get("miss", 0) + 1
    cands = [(sfid, e) for sfid, e in managed.items() if e.get("miss", 0) >= MISS_GRACE]
    if not cands:
        return 0, details
    total = len(managed)
    bulk = len(cands) > MAX_AUTO_DELETE or (total and len(cands) / total > MAX_AUTO_DELETE_PCT)
    if dry_preview:
        for _sfid, e in sorted(cands, key=lambda x: x[1]["rel"]):
            details.append(f"跟删预览: {e['rel']} (已连续 {e['miss']} 轮不在分享中)")
        if bulk:
            details.append(
                f"跟删预览注意: {len(cands)} 个候选超过批量保护阈值"
                f"(>{MAX_AUTO_DELETE}个或>{int(MAX_AUTO_DELETE_PCT*100)}%),自动模式下需人工确认")
        return 0, details
    if bulk:
        details.append(
            f"跟删待确认: {len(cands)} 个文件已连续 {MISS_GRACE} 轮不在分享中,"
            f"超过批量保护阈值(>{MAX_AUTO_DELETE}个或>{int(MAX_AUTO_DELETE_PCT*100)}%),未自动删除")
        for _sfid, e in sorted(cands, key=lambda x: x[1]["rel"]):
            details.append(f"  待确认删除: {e['rel']}")
        return 0, details
    by_fid = {t["fid"]: t for t in tree}
    n = 0
    for sfid, e in sorted(cands, key=lambda x: x[1]["rel"]):
        if e.get("tfid") not in by_fid:
            managed.pop(sfid, None)
            continue
        r = delete_files(client, [e["tfid"]])
        ok = isinstance(r, dict) and r.get("code", 0) == 0
        details.append(f"跟删删除: {e['rel']} -> {'OK' if ok else 'FAIL'}")
        if ok:
            n += 1
            managed.pop(sfid, None)
    return n, details

def main():
    acquire_lock()
    if DRY_RUN:
        print("dry-run 模式: 不转存、不改名、不删除、不移动、不写入 state.json,只预览")
    cfg_path = os.path.join(BASE, "config.json")
    state_path = os.path.join(BASE, "state.json")
    cookie_path = os.path.join(BASE, "cookie.txt")

    if not os.path.exists(cookie_path):
        print("ERR: 缺少 cookie.txt,请先扫码登录")
        sys.exit(2)
    with open(cookie_path, encoding="utf-8") as f:
        cookie = f.read().strip()
    if not cookie:
        print("ERR: cookie.txt 为空")
        sys.exit(2)

    config = load_json(cfg_path, [])
    state = load_json(state_path, {})
    if not config:
        print("ERR: config.json 为空")
        sys.exit(2)

    from quark_client import create_client
    client = create_client(cookies=cookie, auto_login=False)
    shares = client.shares

    total_new = 0
    saved_names = []
    report_lines = []
    for w in config:
        name = w.get("name", w["share_url"])
        print(f"--- 检查: {name}")
        try:
            share_id, pwd = shares.parse_share_url(w["share_url"])
            password = w.get("password") or pwd
            token = shares.get_share_token(share_id, password)
            cur = list_share_files(shares, share_id, token)
            known = state.get(share_id, {})
            new_fids = [fid for fid in cur if fid not in known]
            print(f"    分享内 {len(cur)} 个文件,新增 {len(new_fids)} 个")
            target_fid = w.get("target_fid", "0")
            strip_prefix = w.get("strip_prefix", "")
            groups = {}
            for fid in new_fids:
                path = cur[fid][0]
                rel = rel_of(w, path)
                if is_excluded(w, rel):
                    print(f"    跳过排除: {rel}")
                    known[fid] = path
                    continue
                share_subdir = "/".join(rel.split("/")[:-1])
                subdir = expected_subdir(w, share_subdir, cur[fid][1])
                groups.setdefault(subdir, []).append(fid)
            for subdir, fids in sorted(groups.items()):
                label = subdir or "(根目录)"
                if DRY_RUN:
                    for fid in fids:
                        print(f"    dry-run 预览: 将转存 {cur[fid][0]} -> {label}")
                        report_lines.append(f"dry-run 将转存: {label}/{os.path.basename(cur[fid][0])}")
                    continue
                dest_fid = ensure_subfolder(client, target_fid, subdir) if subdir else target_fid
                tokens = [cur[fid][2] for fid in fids]
                ok_ids, fail_ids = save_share_files_fixed(shares, share_id, token, fids, tokens, dest_fid)
                ok_set = set(ok_ids)
                print(f"    转存 {label}: 成功 {len(ok_ids)} 个,失败 {len(fail_ids)} 个")
                saved_basenames = {os.path.basename(cur[fid][0]) for fid in fids if fid in ok_set}
                try:
                    lst = client.files.list_files(folder_id=dest_fid, page=1, size=200)
                    for it in (lst.get("data", {}) or {}).get("list", []):
                        oname = it.get("file_name", "")
                        if oname in saved_basenames and not it.get("dir"):
                            short = shorten_name(oname)
                            if short != oname:
                                try:
                                    rename_file(client, str(it.get("fid")), short)
                                    print(f"    重命名: {oname} -> {short}")
                                    report_lines.append(f"转存+改名: {label}/{short}")
                                except Exception as re_e:
                                    print(f"    重命名失败 {oname}: {re_e} (查重阶段按版本规则处理)")
                except Exception as le:
                    print(f"    列目录改名跳过: {le}")
                for fid in fids:
                    if fid not in ok_set:
                        print(f"    未入基线(转存失败,下一轮重试): {cur[fid][0]}")
                        continue
                    known[fid] = cur[fid][0]
                    saved_names.append(cur[fid][0])
                total_new += len(ok_ids)
            state[share_id] = known
            # 查重删重 + 错位纠正(可按配置关闭)
            if w.get("dedup", True):
                dc, mc, details = dedup_and_fix(client, w, cur)
            else:
                dc, mc, details = 0, 0, []
                print("    查重已跳过(配置关闭)")
            for d in details:
                print(f"    {'dry-run 预览: ' if DRY_RUN else ''}{d}")
                report_lines.append(d)
            if dc or mc:
                print(f"    查重: {'将删除' if DRY_RUN else '删除'} {dc} 个重复,{'将纠正' if DRY_RUN else '纠正'} {mc} 个错位")
            # 跟删(默认关闭): follow_delete=true 自动删, "preview" 只预览不上手
            fd_mode = w.get("follow_delete")
            if fd_mode:
                transferred_fids = [fid for _s, _f in sorted(groups.items()) for fid in _f]
                managed, _tree = sync_manifest(client, w, share_id, cur, transferred_fids, state)
                fdc, fdetails = follow_delete(client, w, share_id, cur, managed, _tree,
                                              dry_preview=(fd_mode == "preview" or DRY_RUN))
                for d in fdetails:
                    print(f"    {d}")
                    report_lines.append(d)
                if fdc:
                    print(f"    跟删: 删除 {fdc} 个分享已移除的文件")
        except Exception as e:
            print(f"    ERR: {e}")
    if DRY_RUN:
        print("dry-run 完成: 未写入 state.json")
    else:
        save_json(state_path, state)
    print(f"完成,本次新增转存 {total_new} 个文件")
    for n in saved_names:
        print(f"  + {n}")
    if report_lines:
        print("REPORT-BEGIN")
        for r in report_lines:
            print(r)
        print("REPORT-END")

if __name__ == "__main__":
    main()
