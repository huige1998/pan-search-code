#!/usr/bin/env python3
"""光鸭全量清理(2026-10-20 用户授权): 对每个监控目标文件夹:
1) 同(子目录,短名)重复组: 同大小留短名删其余; 不同大小按中位数规则(同目录同类型为参考,无参考不动)
2) 跨格式同集: 留目录内数量最多的格式(票数持平看全文件夹,再持平不动); 含.zip整组豁免
3) 长名改纯数字短名
只处理带集数的文件; 每步打印日志。"""
import sys, os, re, json, time
sys.path.insert(0, '/home/hatch/workspace/guangya-api')
import importlib.util
spec = importlib.util.spec_from_file_location('gywatch', '/home/hatch/workspace/guangya-api/watch.py')
m = importlib.util.module_from_spec(spec)
sys.argv = ['watch.py']
spec.loader.exec_module(m)
from statistics import median

def get_bearer():
    return m.load_bearer()
bearer = get_bearer()
cfgs = json.load(open('/home/hatch/workspace/guangya-api/gy_config.json'))
if isinstance(cfgs, dict): cfgs = cfgs.get('shares', [])

def ep_of(short):
    mm = re.match(r'(\d{2,3})\.', short)
    return int(mm.group(1)) if mm else None

done = set()
try:
    _log = open('/home/hatch/workspace/guangya-api/cleanup_20261020.log').read().splitlines()
except Exception:
    _log = []
_cur = None
for _ln in _log:
    if _ln.startswith('## '): _cur = _ln[3:].strip()
    elif '小计' in _ln and _cur: done.add(_cur); _cur = None
print(f"已完成 {len(done)} 个文件夹，跳过", flush=True)
for cfg in cfgs:
    name = cfg.get('name'); tfid = cfg.get('target_fid')
    if not tfid: continue
    if name in done:
        print(f"## {name} (已完成，跳过)", flush=True); continue
    print(f"\n## {name}", flush=True)
    bearer = get_bearer()
    tree = None
    for attempt in range(3):
        try:
            tree = m.list_target_tree(bearer, tfid); break
        except Exception as e:
            err = str(e)[:80]
            if '401' in err or 'Unauthorized' in err:
                bearer = get_bearer(); time.sleep(2); continue
            print(f"  列目录失败: {err}", flush=True); break
    if tree is None:
        print(f"  列目录失败: 3 次重试仍失败", flush=True); continue
    # 内存模型: [rel, fid, fname, size, alive]
    files = [[rel, fid, fn, size, True] for rel, fid, fn, size in tree]
    def short(fn): return m.short_name_for(cfg, fn)
    def dkey(f): return (os.path.dirname(f[0]), short(f[2]))

    # 1) 同短名重复组
    groups = {}
    for f in files: groups.setdefault(dkey(f), []).append(f)
    for (d, s), g in sorted(groups.items()):
        if len(g) < 2: continue
        sizes = {f[3] for f in g}
        if len(sizes) == 1:
            g.sort(key=lambda f: len(f[2])); keep = g[0]; drop = g[1:]
            rule = '同大小'
        else:
            ext = os.path.splitext(s)[1].lower()
            refs = [f[3] for f in files if f[4] and os.path.dirname(f[0]) == d
                    and os.path.splitext(f[2])[1].lower() == ext and dkey(f) != (d, s) and f[3]]
            if not refs:
                print(f"  待人工 {d+'/' if d else ''}{s}: 多版本无参考 {[round(f[3]/1e9,2) for f in g]}", flush=True); continue
            med = median(refs)
            g.sort(key=lambda f: abs(f[3] - med)); keep = g[0]; drop = g[1:]
            rule = f'中位数{med/1e9:.2f}G'
        for f in drop:
            try:
                m.post("/userres/v1/file/delete_file", {"fileIds": [f[1]]}, bearer=bearer)
                f[4] = False
                print(f"  删重复({rule}): {f[0]} ({f[3]/1e9:.2f}G)", flush=True)
            except Exception as e:
                print(f"  删失败 {f[0]}: {str(e)[:60]}", flush=True)
            time.sleep(0.8)
    # 2) 跨格式同集
    byep = {}
    for f in files:
        if not f[4]: continue
        ep = ep_of(short(f[2]))
        if ep is not None: byep.setdefault((os.path.dirname(f[0]), ep), []).append(f)
    for (d, ep), g in sorted(byep.items()):
        exts = {}
        for f in g: exts.setdefault(os.path.splitext(f[2])[1].lower(), []).append(f)
        if len(exts) < 2 or '.zip' in exts: continue
        dir_cnt = {}
        for f in files:
            if f[4] and os.path.dirname(f[0]) == d:
                dir_cnt[os.path.splitext(f[2])[1].lower()] = dir_cnt.get(os.path.splitext(f[2])[1].lower(), 0) + 1
        win = max(exts, key=lambda e: dir_cnt.get(e, 0))
        if sum(1 for e in exts if dir_cnt.get(e, 0) == dir_cnt.get(win, 0)) > 1:
            all_cnt = {}
            for f in files:
                if f[4]: all_cnt[os.path.splitext(f[2])[1].lower()] = all_cnt.get(os.path.splitext(f[2])[1].lower(), 0) + 1
            win = max(exts, key=lambda e: all_cnt.get(e, 0))
            if sum(1 for e in exts if all_cnt.get(e, 0) == all_cnt.get(win, 0)) > 1:
                print(f"  跨格式待定 E{ep}: {sorted(exts)} 票数持平，不动", flush=True); continue
        for e, fs in exts.items():
            if e == win: continue
            for f in fs:
                try:
                    m.post("/userres/v1/file/delete_file", {"fileIds": [f[1]]}, bearer=bearer)
                    f[4] = False
                    print(f"  删跨格式: {f[0]} (留{win})", flush=True)
                except Exception as ex:
                    print(f"  删失败 {f[0]}: {str(ex)[:60]}", flush=True)
                time.sleep(0.8)
    # 3) 改名
    for f in files:
        if not f[4]: continue
        s = short(f[2])
        if s != f[2]:
            try:
                m.post("/userres/v1/file/rename", {"fileId": f[1], "newName": s}, bearer=bearer)
                print(f"  改名: {f[0]} -> {s}", flush=True)
                f[2] = s; f[0] = (os.path.dirname(f[0]) + '/' if os.path.dirname(f[0]) else '') + s
            except Exception as e:
                print(f"  改名失败 {f[0]}: {str(e)[:60]}", flush=True)
            time.sleep(0.8)
    alive = sum(1 for f in files if f[4])
    print(f"  小计: {len(files)} -> {alive} 个文件", flush=True)
print("\nALL DONE", flush=True)
