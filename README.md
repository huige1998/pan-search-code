---
title: Pan Search
emoji: 🔍
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# 网盘搜索 + 自动转存（试用版）

群成员搜索网盘资源 → 提交转存 → 管理员确认 → 自动转存到夸克「自动转存」文件夹（去广告、验存活）→ 生成分享链接，内容保留 2 天到期自动清理。

运行需要两个 Secrets（在 Space 的 Settings → Variables and secrets 里设置）：
- `QUARK_COOKIE`：夸克登录 cookie
- `ADMIN_KEY`：管理员确认口令
