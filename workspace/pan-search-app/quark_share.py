#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为指定 fid 列表生成夸克分享链接(JSON 输出:share_url 等)。
用法: python3 quark_share.py <fid1,fid2,...> <标题> [过期天数,默认2]
cookie 只从 quark-share-watch/cookie.txt 读,不打印、不外传。
"""
import json, os, sys

sys.path.insert(0, os.path.expanduser("~/workspace/vendor/pylibs"))
sys.path.insert(0, os.path.expanduser("~/workspace/vendor/quarkpan"))
for _k in ("no_proxy", "NO_PROXY"):
    _v = os.environ.get(_k, "")
    if "[" in _v:
        os.environ[_k] = ",".join(x for x in _v.split(",") if "[" not in x)


def main():
    fids = [x for x in sys.argv[1].split(",") if x]
    title = sys.argv[2] if len(sys.argv) > 2 else ""
    days = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    cookie = open(os.path.expanduser("~/workspace/quark-share-watch/cookie.txt"),
                  encoding="utf-8").read().strip()
    from quark_client import create_client
    client = create_client(cookies=cookie, auto_login=False)
    r = client.create_share(fids, title=title, expire_days=days)
    print(json.dumps({"share_url": r.get("share_url"),
                      "expired_at": r.get("expired_at"),
                      "title": r.get("title")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
