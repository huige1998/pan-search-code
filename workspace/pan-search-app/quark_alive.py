#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量校验夸克分享链接是否存活。
stdin 读 JSON 数组(链接字符串),stdout 输出 JSON 对象 {链接: true/false}。
原理:能成功换到 stoken 即存活;分享不存在(41006)等异常即失效。
cookie 只从 quark-share-watch/cookie.txt 读,不打印、不外传。
"""
import json, os, sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.expanduser("~/workspace/vendor/pylibs"))
sys.path.insert(0, os.path.expanduser("~/workspace/vendor/quarkpan"))
for _k in ("no_proxy", "NO_PROXY"):
    _v = os.environ.get(_k, "")
    if "[" in _v:
        os.environ[_k] = ",".join(x for x in _v.split(",") if "[" not in x)


def main():
    urls = json.load(sys.stdin)
    cookie = open(os.path.expanduser("~/workspace/quark-share-watch/cookie.txt"),
                  encoding="utf-8").read().strip()
    from quark_client import create_client
    client = create_client(cookies=cookie, auto_login=False)

    def check(u):
        try:
            sid, pwd = client.shares.parse_share_url(u)
            client.shares.get_share_token(sid, pwd or None)
            return u, True
        except Exception:
            return u, False

    out = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for u, ok in ex.map(check, urls):
            out[u] = ok
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
