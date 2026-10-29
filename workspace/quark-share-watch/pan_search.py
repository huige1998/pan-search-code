#!/usr/bin/env python3
"""文析助手(github.com/z7786/wenxi)聚合网盘搜索 API 客户端。
默认端点 https://pan.dr7786.top,接口 GET /api/v1/search?keyword=&scope=all&limit=&format=object,
无需登录、无需鉴权(打开转存用的 open/file 接口另需 ticket,本脚本不涉及)。
用法: python3 pan_search.py 关键词 [网盘类型] [条数]
网盘类型如 quark/uc/baidu/ali/xunlei/123pan/guangya/115/tianyi/yidong/seed
"""
import sys
import json
import urllib.request
import urllib.parse

BASE = 'https://pan.dr7786.top'


def search(keyword, limit=50):
    q = urllib.parse.urlencode(
        {'keyword': keyword, 'scope': 'all', 'limit': limit, 'format': 'object'})
    req = urllib.request.Request(
        f'{BASE}/api/v1/search?{q}',
        headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.load(r)
    out = []
    for item in d.get('results', []) or []:
        # 按 App 的 ResourceSearchHit.fromRaw 取链接: links[0].url > share_link > url,
        # 再兼容 metadata.link / metadata.record.links 两种旧结构
        links = []
        for ln in (item.get('links') or [])[:8]:
            if isinstance(ln, dict) and ln.get('url'):
                links.append((ln['url'], ln.get('password', '') or ''))
        if not links:
            url = item.get('share_link') or item.get('url') or ''
            if url:
                links.append((url, item.get('password', '') or ''))
        if not links:
            meta = item.get('metadata') or {}
            rec = meta.get('record') or {}
            for ln in rec.get('links') or []:
                if ln.get('url'):
                    links.append((ln['url'], ln.get('password', '') or ''))
            if not links:
                lk = meta.get('link') or {}
                if lk.get('url'):
                    links.append((lk['url'], lk.get('password', '') or ''))
        out.append({'name': item.get('name') or item.get('title'),
                    'drive': item.get('drive_type'),
                    'date': item.get('datetime'), 'links': links})
    return d.get('total'), out


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('用法: pan_search.py 关键词 [网盘类型] [条数]')
        sys.exit(1)
    kw = sys.argv[1]
    drive = sys.argv[2] if len(sys.argv) > 2 else ''
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 50
    total, items = search(kw, limit)
    print(f'返回 {len(items)} 条（API total 字段：{total}）')
    for it in items:
        if drive and it['drive'] != drive:
            continue
        for url, pwd in it['links'] or [('', '')]:
            tail = f' 提取码:{pwd}' if pwd and 'pwd=' not in url else ''
            print(f"{it['drive']:8} | {it['name']} | {url or '(无链接)'}{tail}")
