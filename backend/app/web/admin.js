// The portal is a client of /api/admin and holds no rules of its own.
//
// Every decision it appears to make -- who may do what, which settings
// exist, what a valid value is -- is made by the API and merely
// rendered here. That is deliberate: an assistant driving the same
// instance over MCP later has to be able to do everything this page
// can, and anything implemented in JavaScript would have to be
// implemented a second time to make that work.

const $ = (s) => document.querySelector(s);
const show = (el, on) => el.classList.toggle("hide", !on);

// Session storage, not local: closing the tab ends the session. This
// page configures bank credentials, and it is often opened on a
// machine that is not the owner's own.
const TOKEN_KEY = "nnf.admin.token";
let token = sessionStorage.getItem(TOKEN_KEY) || "";

async function api(path, opts = {}) {
  const r = await fetch("/api" + path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: "Bearer " + token } : {}),
      ...(opts.headers || {}),
    },
  });
  const text = await r.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = { detail: text }; }
  if (!r.ok) {
    // FastAPI puts the readable part in `detail`; validation errors
    // arrive as a list. Either way the user gets a sentence.
    const d = body && body.detail;
    const msg = Array.isArray(d)
      ? d.map((e) => e.msg || JSON.stringify(e)).join("; ")
      : (typeof d === "string" ? d : r.status + " " + r.statusText);
    throw new Error(msg);
  }
  return body;
}

function say(el, message, kind = "bad") {
  el.innerHTML = "";
  if (!message) return;
  const p = document.createElement("div");
  p.className = "note " + kind;
  p.textContent = message;
  el.appendChild(p);
}

// ---------------------------------------------------------------- //
// which screen
// ---------------------------------------------------------------- //

async function route() {
  const state = await api("/admin/state");
  if (!state.configured) {
    show($("#setup"), true); show($("#login"), false); show($("#app"), false);
    try { $("#su-tz").value = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch {}
    return;
  }
  if (!token) {
    show($("#setup"), false); show($("#login"), true); show($("#app"), false);
    return;
  }
  try {
    await loadAll();
    show($("#setup"), false); show($("#login"), false); show($("#app"), true);
  } catch {
    // A token that no longer works is the same as no token.
    token = ""; sessionStorage.removeItem(TOKEN_KEY);
    show($("#login"), true); show($("#app"), false);
  }
}

async function signIn(email, password, errEl) {
  say(errEl, "");
  try {
    const out = await api("/auth/login", {
      method: "POST", body: JSON.stringify({ email, password }),
    });
    token = out.access_token;
    sessionStorage.setItem(TOKEN_KEY, token);
    await route();
  } catch (e) { say(errEl, e.message); }
}

$("#li-go").onclick = () => signIn($("#li-email").value.trim(), $("#li-pw").value, $("#li-err"));
$("#li-pw").onkeydown = (e) => { if (e.key === "Enter") $("#li-go").click(); };

$("#su-go").onclick = async () => {
  const btn = $("#su-go"); btn.disabled = true;
  say($("#su-err"), "");
  try {
    const email = $("#su-email").value.trim();
    const password = $("#su-pw").value;
    await api("/auth/register", {
      method: "POST",
      body: JSON.stringify({
        email, password,
        timezone: $("#su-tz").value.trim() || "UTC",
      }),
    });
    await signIn(email, password, $("#su-err"));
  } catch (e) { say($("#su-err"), e.message); }
  finally { btn.disabled = false; }
};

$("#signout").onclick = () => {
  token = ""; sessionStorage.removeItem(TOKEN_KEY); route();
};

// ---------------------------------------------------------------- //
// tabs
// ---------------------------------------------------------------- //

document.querySelectorAll("nav button").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("nav button").forEach((x) =>
      x.setAttribute("aria-current", String(x === b)));
    document.querySelectorAll(".tab").forEach((t) =>
      show(t, t.id === "tab-" + b.dataset.tab));
  };
});

// ---------------------------------------------------------------- //
// settings
// ---------------------------------------------------------------- //

// Which settings belong on which tab, and how to label them. The API
// is the authority on what EXISTS; this only decides presentation, and
// anything it does not know about still renders with its raw key
// rather than silently disappearing.
const LABELS = {
  plaid_env: ["Environment", "sandbox for testing; production once it all works"],
  plaid_client_id: ["Client ID", "from your Plaid dashboard"],
  plaid_secret: ["Secret", "stored encrypted; never shown again"],
  plaid_webhook_url: ["Webhook URL", "needs https, or transactions only arrive on the schedule"],
  plaid_redirect_uri: ["iOS redirect URI", "only for linking from an iPhone"],
  plaid_android_package_name: ["Android package", "only for linking from Android"],
  plaid_sync_enabled: ["Pull on a schedule", "keeps working without webhooks"],
  plaid_sync_interval_minutes: ["Pull every (minutes)", "hourly by default; two minutes is the floor"],
  ai_provider: ["Provider",
    "none · managed (your machine, woken on demand) · custom "
    + "(your machine, always on) · openai (a hosted service)"],
  ai_host_url: ["Self-hosted address", "your own Ollama host"],
  ai_api_base_url: ["Hosted API address",
    "e.g. https://api.openai.com/v1 — also Gemini, OpenRouter, vLLM"],
  ai_api_key: ["API key", "stored encrypted; sent only to that service"],
  ai_model_text: ["Text model", ""],
  ai_model_vision: ["Vision model", "for reading receipts"],
  ai_confidence_threshold: ["Confidence threshold", "below this it asks you instead"],
  ai_request_timeout_seconds: ["Request timeout (seconds)", ""],
  ai_quiet_enabled: ["Quiet hours", "hold AI work overnight"],
  ai_quiet_timezone: ["Quiet hours time zone", "where the AI machine physically is"],
  ai_quiet_start_hour: ["Quiet from (hour)", ""],
  ai_quiet_end_hour: ["Quiet until (hour)", ""],
  allow_open_registration: ["Allow public sign-ups", "leave off unless you mean it"],
};
const PLAID_KEYS = (k) => k.startsWith("plaid_");
let settings = [];

function field(s) {
  const [label, hint] = LABELS[s.key] || [s.key, ""];
  const wrap = document.createElement("div");
  const id = "set-" + s.key;

  const lab = document.createElement("label");
  lab.setAttribute("for", id);
  lab.textContent = label;
  wrap.appendChild(lab);

  let input;
  if (s.choices) {
    // The API says which values are valid; the page only renders them.
    input = document.createElement("select");
    for (const c of s.choices) {
      const o = document.createElement("option");
      o.value = c; o.textContent = c; input.appendChild(o);
    }
    input.value = s.value ?? s.choices[0];
  } else if (s.kind === "bool") {
    input = document.createElement("select");
    for (const [v, t] of [["true", "On"], ["false", "Off"]]) {
      const o = document.createElement("option");
      o.value = v; o.textContent = t; input.appendChild(o);
    }
    input.value = String(Boolean(s.value));
  } else {
    input = document.createElement("input");
    input.type = s.kind === "secret" ? "password" : (s.kind === "int" || s.kind === "float" ? "number" : "text");
    if (s.kind === "float") input.step = "0.01";
    // A secret is never sent to the browser, so the box starts empty
    // and an empty box means "leave it alone", not "erase it".
    input.value = s.kind === "secret" ? "" : (s.value ?? "");
    if (s.kind === "secret") {
      input.placeholder = s.is_set ? "•••••••• (set — type to replace)" : "not set";
    }
  }
  input.id = id;
  input.dataset.key = s.key;
  input.dataset.kind = s.kind;
  wrap.appendChild(input);

  const bits = [];
  if (hint) bits.push(hint);
  if (s.from_database) bits.push("set here");
  if (bits.length) {
    const h = document.createElement("p");
    h.className = "hint";
    h.textContent = bits.join(" · ");
    wrap.appendChild(h);
  }
  return wrap;
}

function renderSettings() {
  const plaid = $("#plaid-fields"), ai = $("#ai-fields");
  plaid.innerHTML = ""; ai.innerHTML = "";
  for (const s of settings) (PLAID_KEYS(s.key) ? plaid : ai).appendChild(field(s));
}

async function saveGroup(which) {
  const root = which === "plaid" ? $("#plaid-fields") : $("#ai-fields");
  const msg = which === "plaid" ? $("#plaid-msg") : $("#ai-msg");
  const values = {};
  root.querySelectorAll("[data-key]").forEach((el) => {
    // Untouched secret boxes are omitted entirely. Sending "" would
    // mean "clear it", which is the opposite of leaving it alone.
    if (el.dataset.kind === "secret" && el.value === "") return;
    values[el.dataset.key] = el.value;
  });
  say(msg, "");
  try {
    const out = await api("/admin/settings", {
      method: "PUT", body: JSON.stringify({ values }),
    });
    settings = await api("/admin/settings");
    renderSettings();
    say(msg, out.changed.length ? "Saved." : "Nothing changed.", "ok");
    loadHealth();
  } catch (e) { say(msg, e.message); }
}

document.querySelectorAll("[data-save]").forEach((b) => {
  b.onclick = () => saveGroup(b.dataset.save);
});

$("#rewebhook").onclick = async () => {
  say($("#plaid-msg"), "");
  try {
    const out = await api("/plaid/rewebhook", { method: "POST" });
    const parts = [`Re-pointed ${out.updated.length}.`];
    if (out.failed.length) parts.push(`Could not reach: ${out.failed.join(", ")}.`);
    say($("#plaid-msg"), parts.join(" "), out.failed.length ? "warn" : "ok");
  } catch (e) { say($("#plaid-msg"), e.message); }
};

// ---------------------------------------------------------------- //
// accounts
// ---------------------------------------------------------------- //

async function loadPeople() {
  const rows = await api("/admin/users");
  const body = $("#people-rows");
  body.innerHTML = "";
  for (const u of rows) {
    const tr = document.createElement("tr");
    const email = document.createElement("td");
    email.textContent = u.email;
    const role = document.createElement("td");
    if (u.role === "owner") {
      const tag = document.createElement("span");
      tag.className = "tag"; tag.textContent = "owner";
      role.appendChild(tag);
    } else { role.textContent = u.role; }
    const when = document.createElement("td");
    when.className = "muted";
    when.textContent = (u.created_at || "").slice(0, 10);
    tr.append(email, role, when);
    body.appendChild(tr);
  }
}

$("#nu-go").onclick = async () => {
  say($("#nu-msg"), "");
  try {
    await api("/admin/users", {
      method: "POST",
      body: JSON.stringify({
        email: $("#nu-email").value.trim(),
        password: $("#nu-pw").value,
        role: $("#nu-role").value,
      }),
    });
    $("#nu-email").value = ""; $("#nu-pw").value = "";
    await loadPeople();
    say($("#nu-msg"), "Account created.", "ok");
  } catch (e) { say($("#nu-msg"), e.message); }
};

// ---------------------------------------------------------------- //
// overview
// ---------------------------------------------------------------- //

function stat(value, caption) {
  const d = document.createElement("div");
  d.className = "stat";
  const b = document.createElement("b"); b.textContent = value;
  const s = document.createElement("span"); s.textContent = caption;
  d.append(b, s);
  return d;
}

function ago(iso) {
  if (!iso) return "never";
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 2) return "just now";
  if (mins < 90) return mins + " min ago";
  const h = Math.round(mins / 60);
  return h < 36 ? h + " hours ago" : Math.round(h / 24) + " days ago";
}

async function loadHealth() {
  const h = await api("/admin/health");
  const grid = $("#health-grid");
  grid.innerHTML = "";
  grid.append(
    stat(h.plaid.environment, "Plaid environment"),
    stat(`${h.plaid.healthy}/${h.plaid.connections}`, "banks connected and healthy"),
    stat(ago(h.plaid.last_webhook_at), "last update from the bank"),
    stat(h.plaid.scheduled_pull_minutes ? `every ${h.plaid.scheduled_pull_minutes} min` : "off",
         "scheduled pull"),
    stat(h.accounts, "accounts"),
    stat(h.ai.provider === "none" ? "off"
         : (h.ai.reachable === true ? "reachable" : "not answering"), "AI"),
  );

  const notes = $("#health-notes");
  notes.innerHTML = "";
  const add = (text, kind) => {
    const d = document.createElement("div");
    d.className = "note " + kind; d.textContent = text;
    notes.appendChild(d);
  };
  if (!h.plaid.webhook_url) {
    add("No webhook address is set, so transactions only arrive when the " +
        "scheduled pull runs. That needs an https address reachable from " +
        "the internet.", "warn");
  }
  if (h.plaid.environment === "production" && h.plaid.connections > 0) {
    add("Connected to real banks. Every new connection spends one of a " +
        "small permanent allowance.", "warn");
  }
  if (h.ai.provider !== "none" && h.ai.reachable === false) {
    add("The AI computer is not answering. Categorising will wait for you " +
        "instead, and nothing else is affected.", "warn");
  }
  const stuck = (h.jobs && (h.jobs.failed || 0)) || 0;
  if (stuck > 0) add(`${stuck} AI job${stuck === 1 ? "" : "s"} failed recently.`, "warn");
}

async function loadAll() {
  settings = await api("/admin/settings");
  renderSettings();
  await Promise.all([loadPeople(), loadHealth()]);
  const me = settings.length ? "Signed in as owner." : "";
  $("#whoami").textContent = me;
}

route().catch((e) => {
  document.body.innerHTML =
    '<div class="center"><div class="note bad">Could not reach this server: ' +
    e.message + "</div></div>";
});
