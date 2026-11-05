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

const TABS = [["./", "Replays"], ["workers.html", "Workers"]];

function showTabs() {
  const here = location.pathname.split("/").pop() || "./";
  $("tabs").replaceChildren(...TABS.map(([href, label]) =>
    el("a", href === here || (href === "./" && here === "index.html") ? { href, "aria-current": "page" } : { href }, label)));
}

function showAccount(me) {
  const menu = el("details", { class: "account" },
    el("summary", {}, me.name),
    el("nav", { class: "menu" }, el("a", { href: "api/logout" }, "Sign out")));
  document.addEventListener("click", e => { if (!menu.contains(e.target)) menu.open = false; });
  document.addEventListener("keydown", e => { if (e.key === "Escape") menu.open = false; });
  $("account").replaceChildren(menu);
}

function hideAccount() {
  $("account").replaceChildren();
}

async function showVersion(line) {
  try {
    const r = await fetch(`https://api.github.com/repos/${line.dataset.repo}/releases/latest`);
    if (!r.ok) return;
    const release = await r.json();
    line.textContent = `Version ${release.tag_name}, released ${new Date(release.published_at).toLocaleDateString(undefined, { dateStyle: "medium" })}`;
  } catch (e) {}
}

if ($("tabs")) showTabs();
if (location.pathname.split("/").pop().startsWith("download")) document.querySelector("header .nav").setAttribute("aria-current", "page");
for (const line of document.querySelectorAll("[data-repo]")) showVersion(line);
signedIn.then(me => { if (me) showAccount(me); });
