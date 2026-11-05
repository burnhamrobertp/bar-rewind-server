const $ = id => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) k === "class" ? e.className = v : e.setAttribute(k, v);
  for (const c of children) if (c != null) e.append(c);
  return e;
}

function ago(t) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - t));
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

const signedIn = fetch("api/me", { credentials: "same-origin" }).then(r => r.ok ? r.json() : null).catch(() => null);

function showAccount(me) {
  const menu = el("details", { class: "account" },
    el("summary", {}, me.name),
    el("nav", { class: "menu" },
      el("a", { href: "workers.html" }, "Workers", el("span", { class: "hint" }, me.admin ? "Every worker, and the releases they run" : "Machines you parse replays on")),
      el("hr"),
      el("a", { href: "api/logout" }, "Sign out")));
  document.addEventListener("click", e => { if (!menu.contains(e.target)) menu.open = false; });
  document.addEventListener("keydown", e => { if (e.key === "Escape") menu.open = false; });
  $("account").replaceChildren(menu);
}

function hideAccount() {
  $("account").replaceChildren();
}

signedIn.then(me => { if (me) showAccount(me); });
