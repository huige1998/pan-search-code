#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
夸克扫码登录: 生成二维码 -> 用户用夸克 App 扫码 -> Cookie 直接写入 cookie.txt (600权限)
Cookie 不经过聊天,不进日志。
用法: python3 qr_login.py   (前台运行,扫码后自动完成)
"""
import os, sys, time
# 清理 httpx 解析不了的 no_proxy IPv6 方括号写法
for _k in ("no_proxy", "NO_PROXY"):
    _v = os.environ.get(_k, "")
    if "[" in _v:
        os.environ[_k] = ",".join(x for x in _v.split(",") if "[" not in x)
sys.path.insert(0, os.path.expanduser("~/workspace/vendor/quarkpan"))

BASE = os.path.dirname(os.path.abspath(__file__))
QR_PNG = os.path.join(BASE, "login_qr.png")
COOKIE_TXT = os.path.join(BASE, "cookie.txt")

def main():
    from quark_client.auth.api_login import APILogin
    api = APILogin(timeout=300)
    print("正在获取登录二维码...", flush=True)
    qr_token, qr_url = api.get_qr_code()
    print(f"QR_TOKEN_OK", flush=True)

    # 生成二维码图片
    try:
        import qrcode
    except ImportError:
        os.system(f"{sys.executable} -m pip install -q qrcode pillow --break-system-packages 2>/dev/null")
        import qrcode
    img = qrcode.make(qr_url)
    img.save(QR_PNG)
    print(f"QR_PNG:{QR_PNG}", flush=True)

    print("等待扫码(5分钟有效)...", flush=True)
    ok = api.wait_for_login(qr_token)
    if not ok:
        print("LOGIN_TIMEOUT_OR_FAIL", flush=True)
        sys.exit(1)

    # 登录成功,api_login 内部已保存 login_result.json; 从本次 APILogin 的
    # client cookie jar 直接提取(原代码误用了不存在的 QuarkLogin 类,
    # 会在扫码成功后 ImportError 丢掉登录态,2026-10-29 已修)。
    cookies = []
    for c in api.client.cookies.jar:
        if c.domain and "quark.cn" in c.domain:
            cookies.append(f"{c.name}={c.value}")
    cookie_str = "; ".join(cookies)
    if "__pus" not in cookie_str or "__uid" not in cookie_str:
        print("LOGIN_OK_BUT_NO_COOKIE", flush=True)
        sys.exit(1)
    if not cookie_str:
        print("LOGIN_OK_BUT_NO_COOKIE", flush=True)
        sys.exit(1)
    with open(COOKIE_TXT, "w", encoding="utf-8") as f:
        f.write(cookie_str)
    os.chmod(COOKIE_TXT, 0o600)
    # 清理二维码图片
    if os.path.exists(QR_PNG):
        os.remove(QR_PNG)
    print("LOGIN_OK_COOKIE_SAVED", flush=True)

if __name__ == "__main__":
    main()
