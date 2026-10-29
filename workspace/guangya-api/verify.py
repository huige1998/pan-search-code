#!/usr/bin/env python3
"""
光鸭云盘 API 验证脚本
用法: python3 verify.py <用户BearerToken>
- 不带 token: 只验证分享读取 (get_share_access_token + get_share_page_files_list)
- 带 token: 额外验证转存流程 (restore_share + get_task_status)，只转存一个最小的 .md 文件做演示
"""
import sys, json, time
import urllib.request
import urllib.error
import http.client

API = "https://api.guangyapan.com"
SHARE_URL_ID = "1955634744808972344_aeWc_JwocgzRgB78"  # 伟大的长征

# 只对网络层错误重试(HTTP 错误码是有明确答复,不重试,避免 restore 等非幂等接口重复执行)
_NET_ERRS = (urllib.error.URLError, http.client.IncompleteRead,
             http.client.RemoteDisconnected, ConnectionError, TimeoutError)

def post(path, body, bearer=None, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                API + path,
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "User-Agent": "Mozilla/5.0",
                         "Origin": "https://www.guangyapan.com",
                         "Referer": "https://www.guangyapan.com/"},
                method="POST")
            if bearer:
                req.add_header("Authorization", "Bearer " + bearer)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except _NET_ERRS as e:
            last = e
            time.sleep(2 + i * 2)
    raise last

def list_all(token, parent_id=None, prefix="", _seen=None, page_size=20):
    """递归列出分享内所有文件
    2026-10-18 修复: pageSize=100 时服务端经常掐断连接(IncompleteRead),
    改用小分页 + cursor 翻页; 空页(无 list 键)视为结束。
    """
    _seen = _seen or set()
    out, cursor, first = [], 0, True
    while True:
        body = {"pageSize": page_size, "accessToken": token, "orderBy": 0, "sortType": 0}
        if parent_id:
            body["parentId"] = parent_id
        if not first:
            body["cursor"] = cursor
        first = False
        d = post("/userres/v1/get_share_page_files_list", body)
        items = d["data"].get("list", [])
        for f in items:
            fid = f["fileId"]
            if fid in _seen:
                continue
            _seen.add(fid)
            p = prefix + f["fileName"]
            if f.get("resType") == 2:  # resType=2才是文件夹, resType=1是文件(dirType不可靠)
                out += list_all(token, fid, p + "/", _seen, page_size)
            else:
                out.append((fid, p, f.get("fileSize", 0)))
        if len(items) < page_size:
            break
        cursor = d["data"].get("cursor", cursor + page_size)
        time.sleep(0.4)
    return out

def main():
    bearer = sys.argv[1] if len(sys.argv) > 1 else None

    # 1. 拿分享 token
    r = post("/userres/v1/get_share_access_token", {"shareId": SHARE_URL_ID})
    token = r["data"]["accessToken"]
    print("1. 分享 accessToken OK")

    # 2. 列文件
    files = list_all(token)
    print(f"2. 分享共 {len(files)} 个文件")
    for fid, p, sz in files[:5]:
        print(f"   - {p} ({sz/1e6:.1f}MB)")

    if not bearer:
        print("\n未提供 Bearer，仅验证读取。用法: python3 verify.py <BearerToken>")
        return

    # 3. 找最小文件做转存演示
    files.sort(key=lambda x: x[2])
    fid, path, sz = files[0]
    print(f"3. 演示转存最小文件: {path} ({sz}B)")

    # 4. 转存 (parentId 需换成用户真实目标文件夹 id，这里先用根目录占位)
    # 注意: parentId="0" 可能无效，需先调 file/get_file_list 查"来自：分享"文件夹 id
    r = post("/userres/v1/restore_share",
             {"accessToken": token, "fileIds": [fid], "parentId": "0"},
             bearer=bearer)
    task_id = r["data"]["taskId"]
    print(f"4. 转存任务已创建: {task_id}")

    # 5. 轮询
    for _ in range(30):
        time.sleep(2)
        r = post("/userres/v1/get_task_status", {"taskId": task_id}, bearer=bearer)
        st = r["data"]["status"]
        if st == 2:
            print("5. 转存成功!")
            return
        if st == 3:
            print(f"5. 转存失败: {r['data'].get('detail')}")
            return
    print("5. 轮询超时")

if __name__ == "__main__":
    main()
