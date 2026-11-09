/* NAgent — /admin (§2/§16): server management for the owner.
   Dumb renderer too: every action is an HTTP call, the server decides. */

const $ = (id) => document.getElementById(id);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) {
    const err = new Error(data?.reason || data?.error || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtTime(ts) {
  const d = new Date((ts || 0) * 1000);
  return d.toLocaleString();
}

/* ------------------------------------------------------------------ boot */

async function boot() {
  try {
    const me = await api("/api/auth/me");
    if (me.user && me.user.role === "admin") {
      $("admin-login").classList.add("hidden");
      $("admin-panel").classList.remove("hidden");
      await loadOverview();
    } else {
      $("admin-error").textContent =
        me.user ? "нужны права администратора" : "";
    }
  } catch (e) {
    $("admin-error").textContent = e.message;
  }
}

async function loadOverview() {
  const o = await api("/api/admin/overview");
  const stats = $("admin-stats");
  stats.innerHTML = "";
  const items = [
    [o.mode === "setup" ? "setup (онбординг)" : o.mode, "режим сервера"],
    [o.version, "версия"],
    [o.users.length, "пользователей"],
    [o.providers, "провайдеров"],
    [o.active_turns + (o.turn_queue ? ` (+${o.turn_queue} в очереди)` : ""), "активных ходов"],
    [o.scopes.length, "скопов открыто"],
  ];
  for (const [b, s] of items) {
    const d = document.createElement("div");
    d.className = "admin-stat";
    d.innerHTML = `<b>${esc(b)}</b><span>${esc(s)}</span>`;
    stats.appendChild(d);
  }
  // mode buttons
  document.querySelectorAll("#admin-panel .onb-choice[data-mode]").forEach((c) => {
    c.classList.toggle("selected", c.dataset.mode === o.mode);
    c.onclick = async () => {
      if (!confirm(`Сменить режим на «${c.dataset.mode}»? Все сессии сбросятся.`)) return;
      try {
        const r = await api("/api/admin/mode", { method: "POST", body: { mode: c.dataset.mode } });
        const note = $("admin-note");
        note.classList.remove("hidden");
        note.textContent = r.note || `режим: ${r.mode}`;
        await loadOverview();
      } catch (e) { alert(e.message); }
    };
  });
  // users table
  const tbl = $("users-table");
  tbl.innerHTML = `<tr><th>имя</th><th>роль</th><th>создан</th><th>статус</th><th></th></tr>`;
  for (const u of o.users) {
    const tr = document.createElement("tr");
    const roleSel = `<select data-id="${esc(u.id)}" class="role-sel">
      <option value="member" ${u.role === "member" ? "selected" : ""}>участник</option>
      <option value="admin" ${u.role === "admin" ? "selected" : ""}>админ</option></select>`;
    tr.innerHTML = `<td>${esc(u.username)}</td><td>${roleSel}</td>
      <td>${esc(fmtTime(u.created_at))}</td>
      <td>${u.disabled ? "отключён" : "активен"}</td><td></td>`;
    const actions = document.createElement("td");
    const pw = document.createElement("button");
    pw.className = "btn-secondary";
    pw.textContent = "сбросить пароль";
    pw.onclick = async () => {
      const np = prompt("Новый пароль (минимум 4 символа):");
      if (!np) return;
      try {
        const r = await api(`/api/admin/users/${u.id}/password`, { method: "POST", body: { password: np } });
        alert(r.note || "пароль сброшен");
      } catch (e) { alert(e.message); }
    };
    const dis = document.createElement("button");
    dis.className = "btn-secondary";
    dis.textContent = u.disabled ? "включить" : "отключить";
    dis.onclick = async () => {
      await api(`/api/admin/users/${u.id}`, { method: "PATCH", body: { disabled: !u.disabled } });
      loadOverview();
    };
    const del = document.createElement("button");
    del.className = "btn-danger-sm";
    del.textContent = "—";
    del.onclick = async () => {
      if (!confirm(`Удалить ${u.username}? Его <user>.db и папка останутся на диске.`)) return;
      try {
        await api(`/api/admin/users/${u.id}`, { method: "DELETE" });
        loadOverview();
      } catch (e) { alert(e.message); }
    };
    actions.append(pw, dis, del);
    tr.appendChild(actions);
    tbl.appendChild(tr);
  }
  tbl.querySelectorAll(".role-sel").forEach((sel) => {
    sel.onchange = async () => {
      await api(`/api/admin/users/${sel.dataset.id}`, { method: "PATCH", body: { role: sel.value } });
      loadOverview();
    };
  });
  // audit toggle
  $("audit-enabled").checked = o.audit_enabled;
  // master password state
  const ms = $("master-state");
  if (o.master_configured && o.master_locked) {
    ms.innerHTML = `ключи запечатаны мастером-паролем — <b>заблокировано до разблокировки</b>`;
    $("admin-unlock-form").classList.remove("hidden");
  } else if (o.master_configured) {
    ms.textContent = "ключи запечатаны мастером-паролем — разблокировано";
    $("admin-unlock-form").classList.add("hidden");
  } else {
    ms.textContent = "ключи хранятся как есть (мастер-пароль не задан)";
    $("admin-unlock-form").classList.add("hidden");
  }
}

/* ------------------------------------------------------------------ actions */

async function adminLogin() {
  $("admin-error").textContent = "";
  try {
    const me = await api("/api/auth/login", {
      method: "POST",
      body: { username: $("admin-user").value.trim(), password: $("admin-pass").value },
    });
    if (me.user?.role !== "admin") {
      $("admin-error").textContent = "нужны права администратора";
      return;
    }
    location.reload();
  } catch (e) { $("admin-error").textContent = e.message; }
}

document.addEventListener("DOMContentLoaded", () => {
  $("btn-admin-login").onclick = adminLogin;
  $("admin-pass").addEventListener("keydown", (e) => { if (e.key === "Enter") adminLogin(); });
  $("btn-admin-refresh").onclick = () => loadOverview().catch((e) => alert(e.message));
  $("btn-admin-logout").onclick = async () => {
    await api("/api/auth/logout", { method: "POST" }).catch(() => {});
    location.reload();
  };

  $("admin-ws-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/admin/workspace", { method: "POST",
        body: { path: $("admin-ws").value.trim() } });
      alert(`рабочая папка: ${r.workspace}`);
    } catch (err) { alert(err.message); }
  });

  $("admin-master-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/admin/master-password", {
        method: "POST", body: { password: $("admin-master-pw").value } });
      alert(JSON.stringify(r));
      $("admin-master-pw").value = "";
      loadOverview();
    } catch (err) { alert(err.message); }
  });

  $("admin-unlock-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/admin/unlock", { method: "POST",
        body: { password: $("admin-unlock-pw").value } });
      alert("разблокировано");
      loadOverview();
    } catch (err) { alert(err.message); }
  });

  $("admin-user-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/admin/users", { method: "POST", body: {
        username: $("au-name").value.trim(),
        password: $("au-pass").value,
        role: $("au-role").value } });
      $("au-name").value = $("au-pass").value = "";
      loadOverview();
    } catch (err) { alert(err.message); }
  });

  $("audit-enabled").onchange = async () => {
    await api("/api/settings", { method: "PATCH",
      body: { audit_enabled: $("audit-enabled").checked ? "1" : "0" } });
    loadOverview();
  };

  $("btn-audit-refresh").onclick = async () => {
    const { entries } = await api("/api/admin/audit?limit=100");
    const tbl = $("audit-table");
    if (!entries.length) { tbl.innerHTML = `<tr><td class="hint">записей нет (аудит выключен или пусто)</td></tr>`; return; }
    tbl.innerHTML = `<tr><th>время</th><th>юзер</th><th>чат</th><th>модель</th><th>тулза</th><th>ок</th><th>мс</th></tr>`;
    for (const a of entries) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${esc(fmtTime(a.ts))}</td><td>${esc(a.user || "")}</td>
        <td class="mono">${esc((a.chat_id || "").slice(0, 8))}</td>
        <td>${esc((a.model_key || "").split("::").pop() || "")}</td>
        <td>${esc(a.tool)}</td><td>${a.ok ? "✓" : "✗"}</td><td>${Math.round(a.duration_ms || 0)}</td>`;
      tbl.appendChild(tr);
    }
  };

  boot();
});
