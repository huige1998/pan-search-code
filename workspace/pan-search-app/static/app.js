let drive = "";
const $ = (s) => document.querySelector(s);

const STATUS = {
  pending_admin: "待管理员确认", queued: "已确认·排队中", checking: "前置检查中",
  running: "转存中", blocked: "已暂停（缺前置条件）", done_pending_share: "已转存·待生成分享",
  failed: "失败·待重试", rejected: "已拒绝", done: "已完成·可下载", cleaned: "已到期清理"
};

async function search() {
  const q = $("#q").value.trim();
  const st = $("#search-status");
  if (!q) { st.textContent = "先输入关键词"; return; }
  st.textContent = "搜索中…（聚合搜索可能要几秒）";
  $("#results").innerHTML = "";
  try {
    const r = await fetch(`/api/search?q=${encodeURIComponent(q)}&drive=${encodeURIComponent(drive)}`);
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.status);
    st.textContent = `共 ${d.items.length} 条结果` + (drive ? "" : "（全部网盘）");
    renderResults(d.items);
  } catch (e) {
    st.textContent = "搜索失败：" + e.message;
  }
}

function renderResults(items) {
  const box = $("#results");
  if (!items.length) { box.innerHTML = '<p class="hint">没有结果，换个关键词试试</p>'; return; }
  box.innerHTML = "";
  for (const it of items) {
    const div = document.createElement("div");
    div.className = "item";
    let linksHtml = "";
    for (const ln of it.links) {
      linksHtml += `<div class="link"><a href="${ln.url}" target="_blank" rel="noopener">${ln.url}</a>${ln.password ? " 提取码：" + ln.password : ""}</div>`;
    }
    div.innerHTML = `
      <div class="name">${escapeHtml(it.name)}</div>
      <div class="meta"><span class="badge">${it.drive_label || it.drive}</span>${it.source ? `<span class="badge">${it.source}</span>` : ""}${it.alive === true ? '<span class="badge ok">存活</span>' : ""}${it.date || ""}</div>
      ${it.alive === false ? `<div class="warn">❌ 该分享链接已失效，无法转存</div>` : ""}
      ${it.flagged ? `<div class="warn">⚠️ 标题含疑似广告/加群字样：${it.flags.join("、")}，提交后也需管理员确认</div>` : ""}
      ${linksHtml || '<div class="hint">无可用链接</div>'}
      <div class="actions"></div>`;
    const act = div.querySelector(".actions");
    for (const ln of it.links) {
      if (!["quark", "guangya"].includes(it.drive)) continue;
      if (it.alive === false) continue;
      const b = document.createElement("button");
      b.className = "primary";
      b.textContent = "提交转存（管理员确认后自动转）";
      b.onclick = () => submitTask(it, ln);
      act.appendChild(b);
    }
    if (!act.children.length && it.links.length && it.alive !== false) {
      const p = document.createElement("span");
      p.className = "hint";
      p.textContent = "该网盘暂不支持自动转存，可先复制链接手动保存";
      act.appendChild(p);
    }
    box.appendChild(div);
  }
}

async function submitTask(it, ln) {
  const requester = prompt("你的昵称（可留空）", "") || "";
  const r = await fetch("/api/tasks", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: it.name, drive: it.drive, url: ln.url, password: ln.password, flags: it.flags, requester })
  });
  const d = await r.json();
  if (!r.ok) { alert("提交失败：" + (d.error || r.status)); return; }
  alert(d.duplicated ? "这个链接已经有任务在处理了" : "已提交，等管理员确认后自动转存");
  loadTasks();
}

async function loadTasks() {
  try {
    const r = await fetch("/api/tasks");
    const d = await r.json();
    const box = $("#tasks");
    if (!d.tasks.length) { box.innerHTML = '<p class="hint">暂无任务</p>'; return; }
    box.innerHTML = "";
    for (const t of d.tasks) {
      const div = document.createElement("div");
      div.className = "task";
      div.innerHTML = `
        <div><span class="status">${STATUS[t.status] || t.status}</span> <b>${escapeHtml(t.name)}</b></div>
        <div class="meta">${t.drive_label || t.drive || ""} · ${t.created_at}${t.requester ? " · 提交人 " + escapeHtml(t.requester) : ""}</div>
        <div class="link"><a href="${t.url}" target="_blank" rel="noopener">${t.url}</a></div>
        ${t.note ? `<div class="note">${escapeHtml(t.note)}</div>` : ""}
        ${t.share_url ? `<div class="link">分享链接：<a href="${t.share_url}" target="_blank" rel="noopener">${t.share_url}</a></div>` : ""}
        <div class="actions"></div>`;
      const act = div.querySelector(".actions");
      if (t.status === "pending_admin") {
        const ok = document.createElement("button");
        ok.textContent = "管理员确认转存";
        ok.onclick = () => adminAct(t.id, "approve");
        const no = document.createElement("button");
        no.textContent = "拒绝";
        no.onclick = () => adminAct(t.id, "reject");
        act.append(ok, no);
      }
      box.appendChild(div);
    }
  } catch (e) { /* 刷新失败静默 */ }
}

async function adminAct(id, action) {
  let key = sessionStorage.getItem("admin_key") || "";
  if (!key) {
    key = prompt("输入管理员口令（在本机 data/admin_key.txt 里）", "") || "";
    if (!key) return;
    sessionStorage.setItem("admin_key", key);
  }
  const r = await fetch(`/api/tasks/${id}/${action}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ admin_key: key })
  });
  const d = await r.json();
  if (!r.ok) {
    sessionStorage.removeItem("admin_key");
    alert("操作失败：" + (d.error || r.status));
    return;
  }
  loadTasks();
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

$("#btn-search").onclick = search;
$("#q").addEventListener("keydown", (e) => { if (e.key === "Enter") search(); });
document.querySelectorAll("#drive-chips .chip").forEach((c) => {
  c.onclick = () => {
    document.querySelectorAll("#drive-chips .chip").forEach((x) => x.classList.remove("on"));
    c.classList.add("on");
    drive = c.dataset.drive;
    if ($("#q").value.trim()) search();
  };
});
loadTasks();
setInterval(loadTasks, 8000);
