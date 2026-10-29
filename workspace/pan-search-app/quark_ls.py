#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""列出夸克某文件夹的直接子项(JSON 输出:fid/name/dir)。
供 worker 转存前后快照对比、cleanup 到期清理核验使用。
cookie 只从 quark-share-watch/cookie.txt 读,不打印、不外传。
用法: python3 quark_ls.py <folder_fid>
"""
import json, os, sys

sys.path.insert(0, os.path.expanduser("~/workspace/vendor/pylibs"))
sys.path.insert(0, os.path.expanduser("~/workspace/vendor/quarkpan"))
for _k in ("no_proxy", "NO_PROXY"):
    _v = os.environ.get(_k, "")
    if "[" in _v:
        os.environ[_k] = ",".join(x for x in _v.split(",") if "[" not in x)


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else "0"
    cookie = open(os.path.expanduser("~/workspace/quark-share-watch/cookie.txt"),
                  encoding="utf-8").read().strip()
    from quark_client import create_client
    client = create_client(cookies=cookie, auto_login=False)
    items, page = [], 1
    while True:
        res = client.files.list_files(folder_id=folder, page=page, size=100)
        lst = ((res.get("data") or {}).get("list") or []) if isinstance(res, dict) else []
        for it in lst:
            items.append({"fid": str(it.get("fid")), "name": it.get("file_name"),
                          "dir": bool(it.get("dir"))})
        if len(lst) < 100:
            break
        page += 1
    print(json.dumps(items, ensure_ascii=False))


if __name__ == "__main__":
    main()
