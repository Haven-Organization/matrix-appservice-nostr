"""The widget's page (``GET /widget``): one HTML document with its CSS and
script inline, served by ``bridge.widget``.

Its shell, components, styles and helpers are the same as the sibling
bridges' widgets (matrix-appservice-activitypub's is the reference), so
the three look and behave alike; only the accent color (purple here) and
the sections that depend on the network differ.
"""

PAGE = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nostr Bridge</title>
<style>
  :root {
    --bg: #ffffff; --card: #ffffff; --border: #e1e6ec;
    --text: #1b1d22; --muted: #656d77; --muted2: #818a95;
    --accent: #662482; --accent-text: #ffffff; --hover: #f0f2f5;
    --danger: #dc2626; --danger-bg: #fef2f2; --ok-bg: #f0fdf4; --ok-text: #15803d;
    --radius-card: 12px; --radius-inner: 8px; --radius-chip: 6px;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #101317; --card: #101317; --border: #26282d; --text: #ebeef2;
      --muted: #808994; --muted2: #656c76; --accent: #b77ad6; --accent-text: #101317;
      --hover: #1d1f24; --danger: #f87171; --danger-bg: #2a1618; --ok-bg: #12241a; --ok-text: #4ade80;
    }
  }
  :root[data-theme="dark"] {
    --bg: #101317; --card: #101317; --border: #26282d; --text: #ebeef2;
    --muted: #808994; --muted2: #656c76; --accent: #b77ad6; --accent-text: #101317;
    --hover: #1d1f24; --danger: #f87171; --danger-bg: #2a1618; --ok-bg: #12241a; --ok-text: #4ade80;
  }
  :root[data-theme="light"] {
    --bg: #ffffff; --card: #ffffff; --border: #e1e6ec; --text: #1b1d22;
    --muted: #656d77; --muted2: #818a95; --accent: #662482; --accent-text: #ffffff;
    --hover: #f0f2f5; --danger: #dc2626; --danger-bg: #fef2f2; --ok-bg: #f0fdf4; --ok-text: #15803d;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 0; background: var(--bg); color: var(--text);
    font: 16px/1.5 Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    min-height: 100vh;
  }
  #content { padding: 16px; }
  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius-card);
    padding: 16px; margin-bottom: 12px;
  }
  h1 { font-size: 15px; margin: 0 0 4px; display: flex; align-items: center; gap: 8px; }
  h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); margin: 0 0 10px; }
  .sub { color: var(--muted); font-size: 13px; margin: 0 0 12px; word-break: break-word; }
  .badge {
    display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 999px;
    background: var(--accent); color: var(--accent-text); font-weight: 600;
  }
  .badge.plain { background: var(--hover); color: var(--text); }
  .row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; align-items: center; }
  .row:last-child { margin-bottom: 0; }
  button {
    font: inherit; font-size: 13px; font-weight: 600; border: 1px solid var(--border); background: var(--card);
    color: var(--text); border-radius: var(--radius-inner); padding: 8px 14px; cursor: pointer;
  }
  button.primary { background: var(--accent); color: var(--accent-text); border-color: var(--accent); }
  button.danger { color: var(--danger); border-color: var(--danger); }
  button:disabled { opacity: .5; cursor: default; }
  button:not(:disabled):hover { background: var(--hover); }
  button.primary:not(:disabled):hover { filter: brightness(1.08); background: var(--accent); }
  button:not(:disabled):active { transform: translateY(1px); }
  input[type=text], input[type=number], input[type=password], select {
    font: inherit; font-size: 13px; border: 1px solid var(--border); background: var(--bg); color: var(--text);
    border-radius: var(--radius-inner); padding: 8px 10px; flex: 1; min-width: 120px;
  }
  .toast {
    position: fixed; left: 16px; right: 16px; bottom: 16px; padding: 10px 14px; border-radius: var(--radius-inner);
    font-size: 13px; font-weight: 600; display: none; z-index: 60;
  }
  .toast.ok { background: var(--ok-bg); color: var(--ok-text); display: block; }
  .toast.err { background: var(--danger-bg); color: var(--danger); display: block; }
  .kv { display: flex; justify-content: space-between; font-size: 13px; padding: 4px 0; gap: 12px; }
  .kv span:first-child { color: var(--muted); }
  .kv span:last-child { overflow-wrap: anywhere; text-align: right; }
  .list { display: flex; flex-direction: column; gap: 6px; }
  .list-item {
    display: flex; justify-content: space-between; align-items: center; padding: 8px 10px;
    border: 1px solid var(--border); border-radius: var(--radius-inner); font-size: 13px; gap: 10px;
  }
  .list-item.clickable { cursor: pointer; }
  .list-item.clickable:hover { background: var(--hover); }
  .checkbox-label { display: flex; align-items: center; gap: 6px; font-size: 13px; cursor: pointer; }
  .muted { color: var(--muted); }
  .spinner { text-align: center; color: var(--muted); padding: 40px 0; }
  .confirm-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 6px; }
  .confirm-row .muted { font-size: 13px; }
  a { color: var(--accent); }
  .toggle-on { background: var(--ok-bg); color: var(--ok-text); border-color: var(--ok-bg); }
  .warn { background: var(--danger-bg); color: var(--danger); border-radius: var(--radius-inner); padding: 8px 10px; font-size: 13px; margin-bottom: 8px; }
  .secret { font-family: ui-monospace, monospace; word-break: break-all; background: var(--hover); padding: 8px; border-radius: var(--radius-inner); font-size: 13px; }
  .mono { font-family: ui-monospace, monospace; font-size: 11px; word-break: break-all; }
  details summary { cursor: pointer; font-weight: 600; font-size: 13px; margin-bottom: 8px; }

  #shell { display: flex; min-height: 100vh; align-items: stretch; }
  #sidebar {
    display: flex; flex-direction: column; gap: 2px; padding: 12px 8px; width: 176px; flex-shrink: 0;
    border-right: 1px solid var(--border); background: var(--card);
    position: sticky; top: 0; align-self: flex-start; height: 100vh; overflow-y: auto;
    transition: width .15s ease;
  }
  #shell.collapsed #sidebar { width: 56px; }
  .nav-item {
    display: flex; align-items: center; gap: 10px; font: inherit; font-size: 13px; font-weight: 600;
    color: var(--muted); background: none; border: none; border-radius: var(--radius-inner);
    padding: 10px; cursor: pointer; white-space: nowrap; text-align: left; width: 100%; overflow: hidden;
  }
  .nav-item:hover { background: var(--hover); color: var(--text); }
  .nav-item.active { background: var(--hover); color: var(--accent); }
  .nav-item .icon { flex-shrink: 0; width: 20px; height: 20px; display: flex; align-items: center; justify-content: center; font-size: 16px; }
  .nav-item .label { overflow: hidden; text-overflow: ellipsis; }
  #shell.collapsed .nav-item .label { display: none; }
  #shell.collapsed .nav-item { justify-content: center; }
  .mobile-menu-btn {
    display: none; position: fixed; top: 12px; left: 12px; width: 34px; height: 34px; border-radius: 50%;
    border: 1px solid var(--border); background: var(--card); color: var(--text); font-size: 15px;
    align-items: center; justify-content: center; cursor: pointer;
    box-shadow: 0 2px 8px rgba(0,0,0,.15); z-index: 40;
  }
  .mobile-menu-btn:hover { background: var(--hover); }
  @media (max-width: 720px) {
    #sidebar { display: none; }
    #shell.mobile-menu-open #sidebar {
      display: flex; position: fixed; inset: 0; z-index: 50; width: 100%; height: 100vh; padding: 16px 12px;
    }
    #shell.mobile-menu-open .nav-item { justify-content: flex-start; padding: 14px 12px; font-size: 15px; }
    #shell.mobile-menu-open .nav-item .label { display: inline; }
    #shell.mobile-menu-open #sidebar-toggle { display: none; }
    #shell.mobile-menu-open #content { display: none; }
    .mobile-menu-btn { display: flex; }
    body #content { padding-top: 56px; }
  }
  #content { padding: 16px; flex: 1; min-width: 0; max-width: 700px; }
  .placeholder { text-align: center; color: var(--muted); padding: 48px 16px; font-size: 13px; }
  .browse-source-notice { padding: 16px; border-bottom: 1px solid var(--border); }

  .post-card { display: flex; gap: 12px; padding: 16px; position: relative; }
  .post-card + .post-card { border-top: 1px solid var(--border); }
  .avatar { width: 40px; height: 40px; border-radius: 50%; flex-shrink: 0; background: var(--hover); object-fit: cover; }
  .avatar.sm { width: 24px; height: 24px; }
  .avatar-fallback {
    display: flex; align-items: center; justify-content: center; background: var(--accent); color: var(--accent-text);
    font-weight: 700; font-size: 1.1em;
  }
  .post-body { flex: 1; min-width: 0; }
  .post-header { display: flex; align-items: baseline; gap: 6px; font-size: 14px; }
  .post-header-left { display: flex; align-items: baseline; gap: 6px; min-width: 0; overflow: hidden; }
  .post-header-right { margin-left: auto; flex-shrink: 0; display: flex; align-items: center; gap: 2px; }
  .post-header .name { font-weight: 600; color: var(--text); }
  .post-header .handle, .post-header .time { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .post-header .time { text-decoration: none; }
  .post-header .time:hover { text-decoration: underline; }
  .view-icon-btn { border: none; background: none; color: var(--muted); font-size: 13px; padding: 0 0 0 4px; cursor: pointer; flex-shrink: 0; line-height: 1; }
  .view-icon-btn:hover { color: var(--text); }
  .post-timestamp-full { font-size: 12px; color: var(--muted); margin: 6px 0; }
  .boosted-by { font-size: 12px; color: var(--muted); margin-bottom: 4px; }
  .boosted-by img.emoji, .post-header .name img.emoji { width: 1em; height: 1em; }
  .reply-to-line { font-size: 12px; color: var(--muted); margin-bottom: 4px; cursor: pointer; }
  .post-card.chain-link + .post-card { border-top: none; }
  .post-card.chain-link::after {
    content: ""; position: absolute; left: 35px; top: 56px; bottom: -17px; width: 2px; background: var(--border);
  }
  .reply-to-line:hover { color: var(--accent); }
  .post-content { font-size: 16px; line-height: 1.5; margin: 4px 0; overflow-wrap: break-word; }
  .post-content p { margin: 0 0 8px; }
  .post-content p:last-child { margin-bottom: 0; }
  .post-content a { color: var(--accent); }
  .post-content .hashtag { color: var(--accent); }
  img.emoji { width: 1.2em; height: 1.2em; vertical-align: -0.2em; object-fit: contain; }
  .post-media { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; margin: 8px 0; border-radius: var(--radius-inner); overflow: hidden; }
  .post-media img, .post-media video { width: 100%; max-height: 360px; object-fit: cover; display: block; background: var(--hover); }
  .post-media-audio { width: 100%; grid-column: 1 / -1; }
  .post-media.single { grid-template-columns: 1fr; }
  .post-media.single img, .post-media.single video { max-height: 560px; object-fit: contain; }
  .post-counts { display: flex; gap: 14px; margin-top: 8px; font-size: 13px; color: var(--muted); }
  .post-counts .count-item { display: flex; align-items: center; gap: 4px; }
  .post-actions { display: flex; gap: 4px; margin-top: 8px; }
  .post-actions button, .post-actions a {
    border: none; background: none; padding: 6px 8px; font-size: 12px; color: var(--muted);
    display: flex; align-items: center; gap: 4px; border-radius: var(--radius-chip);
    text-decoration: none; cursor: pointer;
  }
  .post-actions button:hover, .post-actions a:hover { background: var(--hover); color: var(--text); }
  .post-actions button.imported, .post-actions a.imported { color: var(--ok-text); }
  a.link-button {
    font: inherit; font-size: 13px; font-weight: 600; border: 1px solid var(--border); background: var(--card);
    color: var(--text); border-radius: var(--radius-inner); padding: 8px 14px; cursor: pointer;
    display: inline-block; text-decoration: none;
  }
  a.link-button:hover { background: var(--hover); }
  .post-card.open-thread { cursor: pointer; }
  .post-card.thread-ancestor { opacity: .8; }
  .post-card.thread-focus { background: var(--hover); border-radius: var(--radius-card); cursor: default; }
  .thread-chain { margin-bottom: 12px; }
  .skel { background: var(--border); opacity: .5; border-radius: 4px; height: 12px; }
  .skel-card { display: flex; gap: 12px; padding: 16px; }
  .skel-card + .skel-card { border-top: 1px solid var(--border); }
  .skel-lines { flex: 1; display: flex; flex-direction: column; gap: 8px; }
  .account-card { display: flex; align-items: center; gap: 10px; padding: 10px; border: 1px solid var(--border); border-radius: var(--radius-inner); }
  .account-card .meta { flex: 1; min-width: 0; }
  .account-card .name { font-weight: 600; font-size: 13px; }
  .account-card .handle { color: var(--muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .scope-toggle { display: flex; gap: 2px; background: var(--hover); border-radius: var(--radius-inner); padding: 2px; }
  .scope-toggle button { border: none; background: none; }
  .scope-toggle button.active { background: var(--card); box-shadow: 0 1px 2px rgba(0,0,0,.08); }

  .identity-card {
    display: flex; align-items: center; gap: 8px; padding: 8px; margin-top: auto;
    border-radius: var(--radius-inner); cursor: pointer; position: relative; width: 100%;
    border: none; background: none; text-align: left; color: var(--text); font: inherit;
  }
  .identity-card:hover { background: var(--hover); }
  .identity-card .avatar { width: 28px; height: 28px; }
  .identity-meta { flex: 1; min-width: 0; overflow: hidden; }
  .identity-meta .name { font-weight: 600; font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .identity-meta .handle { font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #shell.collapsed .identity-meta, #shell.collapsed .identity-dropdown-btn { display: none; }
  #shell.collapsed .identity-card { justify-content: center; }
  .identity-dropdown-btn {
    border: none; background: none; color: var(--muted); padding: 4px; cursor: pointer; font-size: 12px; flex-shrink: 0;
  }
  .form-error {
    color: #e5484d; font-size: 13px; margin-top: 8px; padding: 8px 10px;
    border: 1px solid #e5484d; border-radius: var(--radius-inner); background: rgba(229,72,77,.08);
  }
  .form-textarea {
    font: inherit; font-size: 13px; border: 1px solid var(--border); background: var(--bg); color: var(--text);
    border-radius: var(--radius-inner); padding: 8px 10px; width: 100%; resize: vertical;
  }

  .modal-backdrop {
    position: fixed; inset: 0; background: rgba(0,0,0,.5); display: flex; align-items: center;
    justify-content: center; z-index: 50; padding: 16px;
  }
  .modal-backdrop .card { max-width: 480px; width: 100%; max-height: 85vh; overflow-y: auto; margin-bottom: 0; }
  .modal-close { border: none; background: none; color: var(--muted); font-size: 16px; padding: 4px 8px; cursor: pointer; }
  .modal-actions { justify-content: flex-end; }
  .clickable { cursor: pointer; }

  .back-link {
    border: none; background: none; color: var(--text); font-weight: 600; font-size: 13px;
    cursor: pointer; padding: 4px 0 12px; display: block;
  }
  .back-link:hover { color: var(--accent); background: none; }
  .profile-page { max-width: 700px; }
  .profile-banner-wrap {
    position: relative; width: 100%; height: 180px; border-radius: var(--radius-card);
    overflow: hidden; background: var(--hover); margin-bottom: 44px;
  }
  .profile-banner-empty { background: linear-gradient(135deg, var(--hover), var(--border)); }
  .profile-banner-img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .banner-edit-btn {
    position: absolute; top: 10px; right: 10px; width: 32px; height: 32px; border-radius: 50%;
    border: none; background: rgba(0,0,0,.55); color: #fff; cursor: pointer; font-size: 14px;
    display: flex; align-items: center; justify-content: center; padding: 0;
  }
  .profile-header-row { display: flex; align-items: flex-end; justify-content: space-between; gap: 12px; margin-bottom: 8px; }
  .profile-avatar-xl {
    width: 88px; height: 88px; font-size: 32px; border: 4px solid var(--bg);
    margin-top: -66px; position: relative; z-index: 1; flex-shrink: 0;
  }
  .profile-stats { display: flex; flex-direction: column; align-items: flex-end; gap: 2px; padding-bottom: 8px; }
  .profile-stat { color: var(--muted); font-size: 13px; }
  .profile-name { font-size: 20px; font-weight: 700; margin: 4px 0 0; }
  .profile-handle { display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: 13px; margin: 2px 0 10px; overflow-wrap: anywhere; }
  .copy-btn { border: none; background: none; color: var(--muted); cursor: pointer; padding: 2px; font-size: 12px; }
  .copy-btn:hover { color: var(--text); }
  .profile-bio { font-size: 14px; line-height: 1.5; margin-bottom: 10px; overflow-wrap: break-word; }
  .profile-bio p { margin: 0 0 8px; }

  .scroll-top-btn {
    position: fixed; top: 16px; right: 16px; width: 36px; height: 36px; border-radius: 50%;
    border: 1px solid var(--border); background: var(--card); color: var(--text); font: inherit; font-weight: 700;
    cursor: pointer; display: none; align-items: center; justify-content: center;
    box-shadow: 0 2px 8px rgba(0,0,0,.15); z-index: 40;
  }
  .scroll-top-btn.visible { display: flex; }
  .scroll-top-btn:hover { background: var(--hover); }
</style>
</head>
<body>
  <div id="shell">
    <div id="sidebar"></div>
    <div id="content"><div id="app"><div class="spinner">Connecting&hellip;</div></div></div>
  </div>
  <div id="toast" class="toast"></div>
  <button id="scroll-top-btn" class="scroll-top-btn" title="Scroll to top">^</button>
  <button id="mobile-menu-btn" class="mobile-menu-btn" title="Menu" aria-label="Menu">&#9776;</button>

<script>
(function () {
  "use strict";
  const params = new URLSearchParams(location.search);
  const widgetId = params.get("widgetId") || "";
  const roomId = params.get("matrix_room_id") || "";
  const theme = params.get("theme") || "";
  if (theme.indexOf("dark") !== -1) document.documentElement.setAttribute("data-theme", "dark");
  else if (theme.indexOf("light") !== -1) document.documentElement.setAttribute("data-theme", "light");

  // -- Matrix Widget API (postMessage) ---------------------------------------
  // Every bridge action goes through the bridge's own API, signed in with the verified OpenID identity. The one
  // capability asked for, MSC2931 navigate, only lets the client jump to a
  // matrix.to link for the user. Signing in needs the client around the
  // widget, so it also asks not to be popped out.
  const NAVIGATE_CAPABILITY = "org.matrix.msc2931.navigate";
  let reqCounter = 0;
  const pending = {};

  window.addEventListener("message", (event) => {
    const msg = event.data;
    if (!msg || typeof msg !== "object") return;
    if (msg.widgetId && widgetId && msg.widgetId !== widgetId) return;

    if (msg.api === "fromWidget" && msg.requestId && pending[msg.requestId] && "response" in msg) {
      const resolve = pending[msg.requestId];
      delete pending[msg.requestId];
      resolve(msg.response);
      return;
    }
    if (msg.api === "toWidget" && msg.action === "capabilities") {
      window.parent.postMessage(Object.assign({}, msg, { response: { capabilities: ["io.element.requires_client", NAVIGATE_CAPABILITY] } }), "*");
      return;
    }
    if (msg.api === "toWidget" && msg.action === "openid_credentials") {
      window.parent.postMessage(Object.assign({}, msg, { response: {} }), "*");
      if (openIdResolve) {
        const resolve = openIdResolve;
        openIdResolve = null;
        resolve(msg.data);
      }
      return;
    }
    if (msg.api === "toWidget" && msg.requestId) {
      window.parent.postMessage(Object.assign({}, msg, { response: {} }), "*");
    }
  });

  // Resolves null on timeout, so a client that never answers can't leave
  // the widget waiting forever.
  function sendFromWidget(action, data, timeoutMs) {
    return new Promise((resolve) => {
      const requestId = "req-" + (reqCounter++) + "-" + Date.now();
      let settled = false;
      pending[requestId] = (response) => {
        if (settled) return;
        settled = true;
        resolve(response);
      };
      window.parent.postMessage({ api: "fromWidget", widgetId, requestId, action, data: data || {} }, "*");
      if (timeoutMs) {
        setTimeout(() => {
          if (settled) return;
          settled = true;
          delete pending[requestId];
          resolve(null);
        }, timeoutMs);
      }
    });
  }

  function navigateViaWidgetApi(uri) {
    return sendFromWidget(NAVIGATE_CAPABILITY, { uri }, 2000);
  }

  // Every matrix.to link asks the client to jump there (MSC2931), falling
  // back to a new tab. Mentions and post references in post text open in
  // the widget instead.
  document.addEventListener("click", (e) => {
    const inWidget = e.target.closest("a[data-pubkey], a[data-ref]");
    if (inWidget) {
      e.preventDefault();
      e.stopPropagation();
      if (inWidget.dataset.pubkey) openAccountProfile(inWidget.dataset.pubkey);
      else openThreadView(inWidget.dataset.ref);
      return;
    }
    const anchor = e.target.closest('a[href^="https://matrix.to/#"]');
    if (!anchor) return;
    e.preventDefault();
    const uri = anchor.href;
    navigateViaWidgetApi(uri).then((response) => {
      if (!response || response.error) window.open(uri, "_blank", "noopener");
    });
  }, true);

  let openIdResolve = null;

  async function getOpenIdToken() {
    const first = await sendFromWidget("get_openid", {}, 15000);
    if (first === null) throw new Error("Timed out waiting for the client to respond. Try Cancel below, or closing and reopening this widget.");
    if (first.state === "allowed") return first;
    if (first.state === "blocked") throw new Error("Your client refused to tell the widget who you are.");
    return new Promise((resolve, reject) => {
      openIdResolve = resolve;
      setTimeout(() => {
        if (openIdResolve) { openIdResolve = null; reject(new Error("Timed out waiting for approval.")); }
      }, 60000);
    });
  }

  sendFromWidget("content_loaded", {});

  // -- bridge API -----------------------------------------------------------

  let sessionId = null;

  async function establishSession() {
    const creds = await getOpenIdToken();
    const session = await api("/widget/api/session", {
      method: "POST",
      body: { access_token: creds.access_token, matrix_server_name: creds.matrix_server_name },
    });
    sessionId = session.session_id;
  }

  // One re-sign-in shared by every call that finds the session gone.
  let sessionRefreshPromise = null;
  function refreshSession() {
    if (!sessionRefreshPromise) {
      sessionRefreshPromise = establishSession().finally(() => { sessionRefreshPromise = null; });
    }
    return sessionRefreshPromise;
  }

  // Every call says which room the widget is in: the bridge checks the user
  // is in it.
  function withRoom(path) {
    if (path === "/widget/api/session") return path;
    return path + (path.indexOf("?") === -1 ? "?" : "&") + "room_id=" + encodeURIComponent(roomId);
  }

  async function api(path, opts, _isRetry) {
    opts = opts || {};
    const headers = Object.assign(opts.raw ? {} : { "Content-Type": "application/json" }, opts.headers || {});
    if (sessionId) headers["Authorization"] = "Bearer " + sessionId;
    let body;
    if (opts.raw) body = opts.raw;
    else if (opts.body) body = JSON.stringify(path === "/widget/api/session" ? opts.body : Object.assign({ room_id: roomId }, opts.body));
    const res = await fetch(withRoom(path), { method: opts.method || "GET", headers, body });
    if (res.status === 401 && !_isRetry && path !== "/widget/api/session") {
      try {
        await refreshSession();
        return api(path, opts, true);
      } catch (e) { /* the error below says what's wrong */ }
    }
    let data = {};
    try { data = await res.json(); } catch (e) {}
    if (!res.ok) throw new Error(errorMessage(data, res.status));
    return data;
  }

  function errorMessage(data, status) {
    const detail = data && data.detail;
    if (typeof detail === "string" && detail) return detail;
    if (Array.isArray(detail) && detail.length) {
      return detail.map((d) => (d && typeof d === "object" && d.msg) ? d.msg : JSON.stringify(d)).join("; ");
    }
    if (detail && typeof detail === "object") {
      try { return JSON.stringify(detail); } catch (e) {}
    }
    return "Request failed (" + status + ")";
  }

  function toast(message, ok) {
    const node = document.getElementById("toast");
    node.textContent = message;
    node.className = "toast " + (ok ? "ok" : "err");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { node.className = "toast"; }, 4000);
  }

  // Runs an action, then reloads the context. Actions that are commands
  // answer in the room too.
  async function doAction(path, body, busyBtn) {
    if (busyBtn) busyBtn.disabled = true;
    try {
      const data = await api(path, { method: "POST", body: body || {} });
      toast((data && data.message) || "Done.", true);
      await loadContext();
      return data;
    } catch (e) {
      toast(e.message, false);
    } finally {
      if (busyBtn) busyBtn.disabled = false;
    }
  }

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.keys(attrs || {}).forEach((k) => {
      if (k === "text") node.textContent = attrs[k];
      else if (k === "class") node.className = attrs[k];
      else if (k.indexOf("on") === 0) node.addEventListener(k.slice(2), attrs[k]);
      else node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach((c) => { if (c) node.appendChild(c); });
    return node;
  }

  // An image, or an initial-letter circle when there's none or it fails.
  function avatarEl(url, label, extraClass, style, onclick) {
    const cls = "avatar" + (extraClass ? " " + extraClass : "");
    const initial = ((label || "").trim().charAt(0) || "?").toUpperCase();
    const attrs = { class: cls + " avatar-fallback", text: initial, style: style || "" };
    if (onclick) attrs.onclick = onclick;
    if (!url) return el("div", attrs);
    const imgAttrs = { class: cls, src: url, alt: "", style: style || "" };
    if (onclick) imgAttrs.onclick = onclick;
    const img = el("img", imgAttrs);
    img.addEventListener("error", () => { img.replaceWith(el("div", attrs)); }, { once: true });
    return img;
  }

  function nameNode(tag, cls, plain, nameHtml, onclick) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (nameHtml) node.innerHTML = nameHtml; else node.textContent = plain;
    if (onclick) node.addEventListener("click", onclick);
    return node;
  }

  function matrixToUrl(roomId, eventId) {
    let url = "https://matrix.to/#/" + encodeURIComponent(roomId);
    if (eventId) url += "/" + encodeURIComponent(eventId);
    return url;
  }

  function linkButton(icon, title, href) {
    return el("a", { class: "link-button", text: title, title: title, "aria-label": title, href: href, target: "_blank", rel: "noopener" });
  }

  function copyText(text) {
    try { navigator.clipboard.writeText(text); toast("Copied.", true); } catch (e) { toast("Couldn't copy.", false); }
  }

  let ctx = null;
  const NAV_ITEMS = [
    { id: "home", label: "Home", icon: "\u{1F3E0}" },
    { id: "communities", label: "Communities", icon: "\u{1F465}" },
    { id: "profile", label: "Profile", icon: "\u{1F464}" },
    { id: "settings", label: "Settings", icon: "\u{2699}\u{FE0F}" },
    { id: "admin", label: "Admin", icon: "\u{1F6E0}\u{FE0F}" },
  ];
  let currentTab = "home";
  // Pages opened over a section; "Back" returns to the section as it was.
  let profileView = null;
  let threadView = null;
  let createIdentityView = false;
  let communityView = null;
  let adminSubTab = "access";
  let sidebarCollapsed = (function () {
    try { return localStorage.getItem("nostr-bridge-sidebar-collapsed") === "1"; } catch (e) { return false; }
  })();
  let mobileMenuOpen = false;

  function clearOverlays() {
    profileView = null; threadView = null; createIdentityView = false; communityView = null;
  }

  // -- modal ------------------------------------------------------------------

  function openModal(contentNode) {
    closeModal();
    const backdrop = el("div", {
      id: "modal-backdrop", class: "modal-backdrop",
      onclick: (e) => { if (e.target.id === "modal-backdrop") closeModal(); },
    }, [contentNode]);
    document.body.appendChild(backdrop);
  }
  function closeModal() {
    const existing = document.getElementById("modal-backdrop");
    if (existing) existing.remove();
  }
  function modalHeader(title, extra) {
    return el("div", { class: "row" }, [
      el("h2", { text: title, style: "margin:0;" }),
      el("div", { style: "margin-left:auto;display:flex;gap:4px;" }, (extra || []).concat([
        el("button", { class: "modal-close", text: "✕", onclick: closeModal }),
      ])),
    ]);
  }

  // -- identity (sidebar card) ------------------------------------------------

  function activeId() {
    return (ctx && ctx.ids.find((i) => i.active)) || (ctx && ctx.ids[0]) || null;
  }

  function canCreateMore() {
    return ctx.is_admin || ctx.ids.length < ctx.max_ids;
  }

  function renderIdentityCard(bar) {
    if (!ctx) return;
    if (!ctx.has_profile) {
      bar.appendChild(el("button", {
        class: "identity-card", title: "Create your Nostr identity",
        onclick: (e) => { e.stopPropagation(); openCreateIdentity(); },
      }, [
        el("span", { class: "icon", text: "\u{2795}" }),
        el("div", { class: "identity-meta" }, [el("div", { class: "name", text: "Create profile" })]),
      ]));
      return;
    }
    const active = activeId();
    const others = ctx.ids.filter((i) => !i.active);
    const hasDropdownContent = others.length > 0 || canCreateMore();
    const children = [
      avatarEl(active && active.avatar_url, active && (active.display_name || active.handle)),
      el("div", { class: "identity-meta" }, [
        el("div", { class: "name", text: active ? (active.display_name || active.handle) : "…" }),
        el("div", { class: "handle muted", text: active ? active.handle : "" }),
      ]),
    ];
    if (hasDropdownContent) children.push(el("span", { class: "identity-dropdown-btn", text: "⌄" }));
    bar.appendChild(el("button", {
      class: "identity-card", title: active ? active.handle : "",
      onclick: (e) => {
        if (!hasDropdownContent) return;
        e.stopPropagation();
        openIdentitySwitchModal(others);
      },
    }, children));
  }

  function openIdentitySwitchModal(others) {
    const body = el("div", { class: "card profile-modal" });
    body.appendChild(modalHeader("Switch identity"));
    const list = el("div", { class: "list" });
    others.forEach((i) => {
      list.appendChild(el("button", {
        class: "nav-item", text: (i.display_name || i.name) + " (" + i.handle + ")",
        onclick: () => {
          closeModal();
          api("/widget/api/switch", { method: "POST", body: { id: String(i.number) } })
            .then(() => loadContext()).catch((e2) => toast(e2.message, false));
        },
      }));
    });
    if (canCreateMore()) {
      list.appendChild(el("button", { class: "nav-item", text: "+ New identity", onclick: () => { closeModal(); openCreateIdentity(); } }));
    }
    body.appendChild(list);
    openModal(body);
  }

  function openCreateIdentity() {
    clearOverlays();
    createIdentityView = true;
    mobileMenuOpen = false;
    render();
  }

  function renderCreateIdentityPage(app) {
    app.appendChild(el("button", { class: "back-link", text: "← Back", onclick: () => { createIdentityView = false; render(); } }));
    const page = el("div", { class: "profile-page" });
    page.appendChild(el("h2", { text: ctx.has_profile ? "New identity" : "Create your Nostr identity" }));
    page.appendChild(el("div", { class: "sub", text: "Pick a name: it becomes your Nostr handle, name@" + ctx.domain + ". " +
      (ctx.has_profile ? "" : "Leave it empty to use your Matrix username. ") +
      "To bring a key you already have, use Profile, Keys instead." }));
    const errorBox = el("div", { class: "form-error", style: "display:none" });
    const input = el("input", { type: "text", placeholder: ctx.has_profile ? "name" : "name (optional)",
      onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); submitBtn.click(); } } });
    const submitBtn = primaryButton("\u{2713}", "Create", () => {
      errorBox.style.display = "none";
      if (ctx.has_profile && !input.value.trim()) { errorBox.textContent = "Choose a name first."; errorBox.style.display = ""; return; }
      submitBtn.disabled = true;
      const before = ctx.ids.length;
      api("/widget/api/create", { method: "POST", body: { name: input.value.trim() } })
        .then(() => loadContext())
        .then(() => {
          if (ctx.ids.length > before) { toast("Identity created.", true); createIdentityView = false; render(); }
          else { errorBox.textContent = "It wasn't created: the bot says why in the room."; errorBox.style.display = ""; }
        })
        .catch((e) => { errorBox.textContent = e.message; errorBox.style.display = ""; })
        .finally(() => { submitBtn.disabled = false; });
    });
    page.appendChild(el("div", { class: "row" }, [input, submitBtn]));
    page.appendChild(errorBox);
    app.appendChild(page);
    setTimeout(() => input.focus(), 0);
  }

  function renderSidebar() {
    const shell = document.getElementById("shell");
    shell.classList.toggle("collapsed", sidebarCollapsed);
    shell.classList.toggle("mobile-menu-open", mobileMenuOpen);
    const bar = document.getElementById("sidebar");
    bar.innerHTML = "";
    if (!ctx) return;
    if (mobileMenuOpen) {
      bar.appendChild(el("button", { class: "nav-item", title: "Close menu", onclick: () => { mobileMenuOpen = false; render(); } }, [
        el("span", { class: "icon", text: "✕" }),
        el("span", { class: "label", text: "Close" }),
      ]));
    }
    const anyOverlayShowing = !!(profileView || threadView || createIdentityView || communityView);
    NAV_ITEMS.forEach((t) => {
      if (t.id === "admin" && !ctx.is_admin) return;
      bar.appendChild(el("button", {
        class: "nav-item" + (!anyOverlayShowing && t.id === currentTab ? " active" : ""),
        title: t.label,
        onclick: () => { currentTab = t.id; clearOverlays(); mobileMenuOpen = false; render(); },
      }, [
        el("span", { class: "icon", text: t.icon }),
        el("span", { class: "label", text: t.label }),
      ]));
    });
    renderIdentityCard(bar);
    bar.appendChild(el("button", {
      id: "sidebar-toggle", class: "nav-item", title: sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar",
      onclick: () => {
        sidebarCollapsed = !sidebarCollapsed;
        try { localStorage.setItem("nostr-bridge-sidebar-collapsed", sidebarCollapsed ? "1" : "0"); } catch (e) {}
        renderSidebar();
      },
    }, [
      el("span", { class: "icon", text: sidebarCollapsed ? "»" : "«" }),
      el("span", { class: "label", text: "Collapse" }),
    ]));
  }

  function render() {
    renderSidebar();
    const app = document.getElementById("app");
    app.innerHTML = "";
    if (!ctx) return;
    const goingToHome = !profileView && !threadView && !createIdentityView && !communityView && currentTab === "home";
    if (!goingToHome && window.__homeScrollHandler) {
      window.removeEventListener("scroll", window.__homeScrollHandler);
      window.__homeScrollHandler = null;
    }
    if (createIdentityView) { renderCreateIdentityPage(app); return; }
    if (profileView) { renderProfilePage(app, profileView); return; }
    if (threadView) { renderThreadPage(app, threadView); return; }
    if (communityView) { renderCommunityPage(app, communityView); return; }
    if (currentTab === "home") renderHomeTimeline(app);
    else if (currentTab === "communities") renderCommunitiesTab(app);
    else if (currentTab === "profile") renderOwnProfileTab(app);
    else if (currentTab === "settings") renderSettingsTab(app);
    else if (currentTab === "admin") renderAdminTab(app);
  }

  // -- Home: browsing Nostr -----------------------------------------------------

  function timeAgo(iso) {
    if (!iso) return "";
    const diff = (Date.now() - new Date(iso).getTime()) / 1000;
    if (diff < 60) return "now";
    if (diff < 3600) return Math.floor(diff / 60) + "m";
    if (diff < 86400) return Math.floor(diff / 3600) + "h";
    if (diff < 86400 * 7) return Math.floor(diff / 86400) + "d";
    return new Date(iso).toLocaleDateString();
  }

  function fullDateTime(iso) {
    if (!iso) return "";
    try {
      return new Date(iso).toLocaleString(undefined, { dateStyle: "long", timeStyle: "short" });
    } catch (e) {
      return new Date(iso).toString();
    }
  }

  function renderSkeletonCard() {
    return el("div", { class: "skel-card" }, [
      el("div", { class: "avatar skel" }),
      el("div", { class: "skel-lines" }, [
        el("div", { class: "skel", style: "width:40%" }),
        el("div", { class: "skel", style: "width:90%" }),
        el("div", { class: "skel", style: "width:65%" }),
      ]),
    ]);
  }

  function importPost(post, btn, onDone) {
    if (btn) btn.disabled = true;
    api("/widget/api/import", { method: "POST", body: { reference: post.nevent || post.id } })
      .then((data) => {
        toast(data.already ? "Already in Matrix." : "Imported.", true);
        post.already_imported = true;
        post.matrix_room_id = data.room_id;
        post.matrix_event_id = data.event_id;
        if (onDone) onDone();
      })
      .catch((e) => { toast(e.message, false); })
      .finally(() => { if (btn) btn.disabled = false; });
  }

  // A card that redraws itself when it changes (an import).
  function renderLiveCard(post, opts) {
    const holder = { node: null };
    function onChange() {
      const fresh = renderLiveCard(post, opts);
      holder.node.replaceWith(fresh);
      holder.node = fresh;
    }
    holder.node = renderPostCard(post, onChange, opts);
    return holder.node;
  }

  function renderPostCard(post, onChange, opts) {
    const focused = !!(opts && opts.focused);
    const children = [];
    if (post.boosted_by) {
      const b = post.boosted_by;
      const line = document.createElement("div");
      line.className = "boosted-by clickable";
      line.appendChild(document.createTextNode("\u{1F501} "));
      line.appendChild(nameNode("span", "", b.display_name || b.handle, null, null));
      line.appendChild(document.createTextNode(" reposted"));
      line.addEventListener("click", (e) => { e.stopPropagation(); openAccountProfile(b.pubkey); });
      children.push(line);
    }
    if (post.in_reply_to) {
      const hint = post.reply_hint_display_name;
      children.push(el("div", {
        class: "reply-to-line",
        text: "↩ Reply to " + (hint ? hint + "'s post" : "a post"),
        onclick: (e) => { e.stopPropagation(); openThreadView(post.in_reply_to); },
      }));
    }
    const openAuthor = (e) => { if (e) e.stopPropagation(); openAccountProfile(post.author.pubkey); };
    const headerLeft = el("div", { class: "post-header-left" }, [
      nameNode("span", "name clickable", post.author.display_name, null, openAuthor),
      el("span", { class: "handle clickable", text: post.author.handle, onclick: openAuthor }),
    ]);
    const headerRightChildren = [];
    if (!focused) {
      headerRightChildren.push(el("a", {
        class: "time", text: timeAgo(post.created_at), title: fullDateTime(post.created_at),
        href: post.url, target: "_blank", rel: "noopener",
      }));
    }
    headerRightChildren.push(el("button", {
      class: "view-icon-btn", text: "↗", title: "Open on the web",
      onclick: (e) => { e.stopPropagation(); window.open(post.url, "_blank", "noopener"); },
    }));
    const header = el("div", { class: "post-header" }, [headerLeft, el("div", { class: "post-header-right" }, headerRightChildren)]);
    const content = el("div", { class: "post-content" });
    content.innerHTML = post.content_html || "";
    children.push(header, content);
    if (post.media && post.media.length) {
      const mediaItems = post.media.slice(0, 4);
      const mediaClass = "post-media" + (mediaItems.length === 1 ? " single" : "");
      children.push(el("div", { class: mediaClass }, mediaItems.map((m) => {
        if (m.type === "video") {
          const attrs = { src: m.url, controls: "", preload: "metadata" };
          if (m.thumb) attrs.poster = m.thumb;
          return el("video", attrs);
        }
        if (m.type === "audio") return el("audio", { class: "post-media-audio", src: m.url, controls: "" });
        return el("a", { href: m.full_url || m.url, target: "_blank", rel: "noopener" }, [
          el("img", { src: m.url, alt: m.description || "", loading: "lazy" }),
        ]);
      })));
    }
    if (focused) children.push(el("div", { class: "post-timestamp-full", text: fullDateTime(post.created_at) }));
    const counts = post.counts || { replies: 0, reposts: 0, reactions: 0 };
    children.push(el("div", { class: "post-counts" }, [
      el("span", { class: "count-item", text: "\u{1F4AC} " + counts.replies, title: "Replies" }),
      el("span", { class: "count-item", text: "\u{1F501} " + counts.reposts, title: "Reposts" }),
      el("span", { class: "count-item", text: "\u{2764}\u{FE0F} " + counts.reactions, title: "Reactions" }),
    ]));
    const actions = el("div", { class: "post-actions" });
    if (post.already_imported) {
      actions.appendChild(el("a", {
        class: "imported", text: "View on Matrix",
        href: matrixToUrl(post.matrix_room_id, post.matrix_event_id), target: "_blank", rel: "noopener",
      }));
    } else {
      actions.appendChild(el("button", {
        text: "⬇ Import", title: "Import", "aria-label": "Import",
        onclick: (e) => { e.stopPropagation(); importPost(post, e.target, onChange); },
      }));
    }
    children.push(actions);
    const card = el("div", { class: "post-card" }, [
      avatarEl(post.author.avatar_url, post.author.display_name, "clickable", "", openAuthor),
      el("div", { class: "post-body" }, children),
    ]);
    card.addEventListener("click", (e) => {
      if (e.target.closest("a, button, .clickable, video, audio")) return;
      openThreadView(post.nevent || post.id);
    });
    card.classList.add("open-thread");
    return card;
  }

  let homeState = null;

  // The relay picker: the bridge's relays and the ones the user added,
  // kept in this browser only.
  const BROWSE_RELAYS_STORAGE_KEY = "nostr-bridge-browse-relays";
  const BROWSE_CHOICE_STORAGE_KEY = "nostr-bridge-browse-source";

  function loadUserRelays() {
    try {
      const raw = localStorage.getItem(BROWSE_RELAYS_STORAGE_KEY);
      const arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr.filter((d) => typeof d === "string" && d) : [];
    } catch (e) { return []; }
  }
  function saveUserRelays(list) {
    try { localStorage.setItem(BROWSE_RELAYS_STORAGE_KEY, JSON.stringify(list)); } catch (e) {}
  }
  function normalizeRelay(raw) {
    let url = (raw || "").trim();
    if (!url) return "";
    if (!/^wss?:\/\//i.test(url)) url = "wss://" + url.replace(/^https?:\/\//i, "");
    return url.replace(/\/+$/, "");
  }
  function loadBrowseChoice() {
    try { return localStorage.getItem(BROWSE_CHOICE_STORAGE_KEY) || ""; } catch (e) { return ""; }
  }
  function saveBrowseChoice(value) {
    try { localStorage.setItem(BROWSE_CHOICE_STORAGE_KEY, value); } catch (e) {}
  }

  function browseOptions(selected) {
    const seen = new Set();
    const options = [{ value: "following", label: "Following" }, { value: "global", label: "Global" }];
    function addRelay(url, suffix) {
      if (!url || seen.has(url)) return;
      seen.add(url);
      options.push({ value: "relay:" + url, label: url.replace(/^wss:\/\//, "") + (suffix || "") });
    }
    (ctx.relays || []).forEach((r) => addRelay(r));
    loadUserRelays().forEach((r) => addRelay(r));
    if (selected && selected.indexOf("relay:") === 0) addRelay(selected.slice(6));
    options.push({ value: "__add__", label: "+ Add relay…" });
    return options;
  }

  function openAddRelayModal(onAdded) {
    const body = el("div", { class: "card profile-modal" });
    const input = el("input", { type: "text", placeholder: "wss://relay.example.org" });
    body.appendChild(modalHeader("Add a relay to browse"));
    body.appendChild(el("div", { class: "row" }, [input]));
    function submit() {
      const url = normalizeRelay(input.value);
      if (!url) return;
      const list = loadUserRelays();
      if (list.indexOf(url) === -1) { list.push(url); saveUserRelays(list); }
      closeModal();
      onAdded(url);
    }
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
    body.appendChild(el("div", { class: "row modal-actions" }, [primaryButton("\u{2795}", "Add", submit)]));
    openModal(body);
    setTimeout(() => input.focus(), 0);
  }

  function renderHomeTimeline(app) {
    if (!homeState) {
      const initial = loadBrowseChoice() || (activeId() ? "following" : "global");
      homeState = { source: initial, until: null, posts: [], loading: false, done: false, notice: "", scrollY: 0 };
    }
    const state = homeState;

    const searchInput = el("input", { type: "text", placeholder: "Search people or posts, or paste an npub, name@domain or post link" });
    const searchResults = el("div", { class: "list" });
    function switchTo(value) {
      state.source = value;
      state.posts = []; state.until = null; state.done = false; state.notice = ""; state.scrollY = 0;
      saveBrowseChoice(value);
      render();
    }
    const sourceSelect = el("select", {
      onchange: () => {
        const v = sourceSelect.value;
        if (v === "__add__") {
          openAddRelayModal((url) => switchTo("relay:" + url));
          sourceSelect.value = state.source;
          return;
        }
        switchTo(v);
      },
    }, browseOptions(state.source).map((o) => {
      const attrs = { value: o.value, text: o.label };
      if (o.value === state.source) attrs.selected = "selected";
      return el("option", attrs);
    }));

    function accountRow(a) {
      return el("div", { class: "account-card clickable", onclick: () => openAccountProfile(a.pubkey) }, [
        avatarEl(a.avatar_url, a.display_name, "sm"),
        el("div", { class: "meta" }, [
          el("div", { class: "name", text: a.display_name }),
          el("div", { class: "handle", text: a.handle }),
        ]),
      ]);
    }

    async function runSearch() {
      const q = searchInput.value.trim();
      if (!q) return;
      searchResults.innerHTML = "";
      searchResults.appendChild(el("div", { class: "muted", text: "Searching…" }));
      try {
        const data = await api("/widget/api/browse/search?q=" + encodeURIComponent(q));
        searchResults.innerHTML = "";
        if (data.open) { searchInput.value = ""; openThreadView(data.open); return; }
        if (!data.accounts.length && !data.posts.length) { searchResults.appendChild(el("div", { class: "muted", text: "Nothing found." })); return; }
        data.accounts.forEach((a) => searchResults.appendChild(accountRow(a)));
        if (data.posts.length) {
          const postsBox = el("div", { class: "card", style: "padding:0;margin:8px 0 0;" });
          data.posts.forEach((p) => postsBox.appendChild(renderLiveCard(p)));
          searchResults.appendChild(postsBox);
        }
      } catch (err) { searchResults.innerHTML = ""; toast(err.message, false); }
    }
    searchInput.addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(); });

    const browseRow = [sourceSelect];
    if (state.source.indexOf("relay:") === 0 && loadUserRelays().indexOf(state.source.slice(6)) !== -1) {
      browseRow.push(dangerButton("\u{1F5D1}\u{FE0F}", "Remove", () => {
        saveUserRelays(loadUserRelays().filter((r) => r !== state.source.slice(6)));
        switchTo("global");
      }));
    }
    const description = state.source === "following"
      ? "Posts from the accounts your Active identity follows."
      : state.source === "global" ? "Recent posts on this bridge's relays."
      : "Recent posts on " + state.source.slice(6) + ".";
    app.appendChild(el("div", { class: "card" }, [
      el("h2", { text: "Browse Nostr" }),
      el("div", { class: "row" }, [searchInput, primaryButton("\u{1F50D}", "Go", runSearch)]),
      searchResults,
      el("div", { class: "row" }, browseRow),
      el("div", { class: "sub", text: description, style: "margin:0;" }),
    ]));

    const feed = el("div", { class: "card", style: "padding:0;" });
    app.appendChild(feed);
    const sentinel = el("div");
    app.appendChild(sentinel);

    function renderFeedList() {
      feed.innerHTML = "";
      if (state.notice && !state.posts.length && !state.loading) {
        feed.appendChild(el("div", { class: "placeholder" }, [el("div", { text: state.notice })]));
        return;
      }
      if (!state.posts.length && !state.loading) {
        feed.appendChild(el("div", { class: "placeholder", text: "Nothing to show yet." }));
        return;
      }
      state.posts.forEach((p) => feed.appendChild(renderPostCard(p, renderFeedList)));
      if (state.loading) { for (let i = 0; i < 3; i++) feed.appendChild(renderSkeletonCard()); }
    }

    async function loadMore() {
      if (state.loading || state.done) return;
      state.loading = true;
      renderFeedList();
      try {
        let path = "/widget/api/browse/feed?source=";
        if (state.source.indexOf("relay:") === 0) path += "relay&relay=" + encodeURIComponent(state.source.slice(6));
        else path += encodeURIComponent(state.source);
        if (state.until) path += "&until=" + state.until;
        const data = await api(path);
        if (data.needs_id) state.notice = "Create a Nostr identity to see the accounts you follow.";
        else if (data.no_follows) state.notice = "Your Active identity doesn't follow anyone yet. Search for people above.";
        state.until = data.next_until || null;
        state.done = !data.next_until;
        const seen = new Set(state.posts.map((p) => p.id));
        state.posts = state.posts.concat(data.posts.filter((p) => !seen.has(p.id)));
      } catch (e) {
        state.done = true;
        toast(e.message, false);
      } finally {
        state.loading = false;
        renderFeedList();
      }
    }

    renderFeedList();
    function restoreScroll() {
      if (!state.scrollY) return;
      requestAnimationFrame(() => requestAnimationFrame(() => window.scrollTo(0, state.scrollY)));
    }
    if (!state.posts.length) loadMore().then(restoreScroll);
    else restoreScroll();

    if (window.__homeScrollHandler) window.removeEventListener("scroll", window.__homeScrollHandler);
    window.__homeScrollHandler = () => { state.scrollY = window.scrollY; };
    window.addEventListener("scroll", window.__homeScrollHandler);

    if (window.__timelineObserver) window.__timelineObserver.disconnect();
    window.__timelineObserver = new IntersectionObserver((entries) => { if (entries[0].isIntersecting) loadMore(); });
    window.__timelineObserver.observe(sentinel);
  }

  // -- profiles (own, or any account) -----------------------------------------

  function profileBanner(bannerUrl, onUpload) {
    const wrap = el("div", { class: "profile-banner-wrap" + (bannerUrl ? "" : " profile-banner-empty") });
    if (bannerUrl) wrap.appendChild(el("img", { class: "profile-banner-img", src: bannerUrl, alt: "" }));
    if (onUpload) {
      const fileInput = el("input", { type: "file", accept: "image/*", style: "display:none" });
      fileInput.addEventListener("change", () => { if (fileInput.files[0]) onUpload(fileInput.files[0]); });
      wrap.appendChild(el("button", { class: "banner-edit-btn", text: "✎", title: "Change banner", onclick: () => fileInput.click() }));
      wrap.appendChild(fileInput);
    }
    return wrap;
  }

  function appendProfileIdentity(page, avatarUrl, name, handle, actionsNode, onAvatarUpload) {
    let avatar = avatarEl(avatarUrl, name, "profile-avatar-xl");
    if (onAvatarUpload) {
      const fileInput = el("input", { type: "file", accept: "image/*", style: "display:none" });
      fileInput.addEventListener("change", () => { if (fileInput.files[0]) onAvatarUpload(fileInput.files[0]); });
      avatar.classList.add("clickable");
      avatar.title = "Change avatar";
      avatar.addEventListener("click", () => fileInput.click());
      avatar = el("div", { style: "position:relative;" }, [avatar, fileInput]);
    }
    page.appendChild(el("div", { class: "profile-header-row" }, [avatar, actionsNode || el("div", {})]));
    page.appendChild(el("div", { class: "profile-name", text: name }));
    page.appendChild(el("div", { class: "profile-handle" }, [
      el("span", { text: handle }),
      el("button", { class: "copy-btn", text: "⧉", title: "Copy", onclick: () => copyText(handle) }),
    ]));
  }

  function renderProfilePostFeed(page, pubkey) {
    page.appendChild(el("h2", { text: "Posts" }));
    const feed = el("div", { class: "card", style: "padding:0;" }, [el("div", { class: "placeholder", text: "Loading…" })]);
    const sentinel = el("div");
    page.appendChild(feed);
    page.appendChild(sentinel);
    let until = null, loading = false, done = false, first = true;
    function loadMorePosts() {
      if (loading || done) return;
      loading = true;
      api("/widget/api/browse/account/posts?pubkey=" + encodeURIComponent(pubkey) + (until ? "&until=" + until : ""))
        .then((data) => {
          if (first) { feed.innerHTML = ""; first = false; }
          if (!data.posts.length && !feed.children.length) feed.appendChild(el("div", { class: "placeholder", text: "No posts found." }));
          data.posts.forEach((post) => feed.appendChild(renderLiveCard(post)));
          until = data.next_until || null;
          done = !until;
        })
        .catch(() => { done = true; if (first) { feed.innerHTML = ""; feed.appendChild(el("div", { class: "placeholder", text: "Couldn't load posts." })); } })
        .finally(() => { loading = false; });
    }
    loadMorePosts();
    const observer = new IntersectionObserver((entries) => { if (entries[0].isIntersecting) loadMorePosts(); });
    observer.observe(sentinel);
  }

  function openAccountProfile(pubkey) {
    if (!pubkey) return;
    clearOverlays();
    profileView = { pubkey: pubkey };
    mobileMenuOpen = false;
    render();
  }

  function openThreadView(ref) {
    if (!ref) return;
    clearOverlays();
    threadView = { ref: ref };
    render();
  }

  function renderProfilePage(app, view) {
    app.appendChild(el("button", { class: "back-link", text: "← Back", onclick: () => { profileView = null; render(); } }));
    const page = el("div", { class: "profile-page" }, [el("div", { class: "muted", text: "Loading…" })]);
    app.appendChild(page);
    function act(path, body, btn) {
      if (btn) btn.disabled = true;
      api(path, { method: "POST", body: body })
        .then(() => { toast("Done.", true); return refresh(); })
        .catch((e) => toast(e.message, false))
        .finally(() => { if (btn) btn.disabled = false; });
    }
    function renderAccount(p) {
      page.innerHTML = "";
      page.appendChild(profileBanner(p.banner_url, null));
      const buttons = [];
      if (p.is_local) {
        if (p.room_id) buttons.push(linkButton("", "Open Profile Room", matrixToUrl(p.room_id)));
      } else if (activeId()) {
        buttons.push(p.is_following
          ? dangerButton("\u{2796}", "Unfollow", (e) => act("/widget/api/unfollow", { account: p.npub }, e.target))
          : primaryButton("\u{2795}", "Follow", (e) => act("/widget/api/follow", { account: p.npub }, e.target)));
        buttons.push(plainButton("\u{2709}\u{FE0F}", "DM", (e) => act("/widget/api/dm", { account: p.npub }, e.target)));
        if (p.room_id) buttons.push(linkButton("", "Open room", matrixToUrl(p.room_id)));
      }
      appendProfileIdentity(page, p.avatar_url, p.display_name, p.handle, el("div", { class: "row" }, buttons));
      page.appendChild(el("div", { class: "profile-handle" }, [
        el("span", { class: "mono", text: p.npub }),
        el("button", { class: "copy-btn", text: "⧉", title: "Copy npub", onclick: () => copyText(p.npub) }),
      ]));
      if (p.bio_html) { const bio = el("div", { class: "profile-bio" }); bio.innerHTML = p.bio_html; page.appendChild(bio); }
      if (p.website) page.appendChild(el("div", { class: "sub" }, [el("a", { href: p.website, target: "_blank", rel: "noopener", text: p.website })]));
      if (!p.is_local && activeId()) {
        page.appendChild(el("div", { class: "row" }, [
          p.is_blocked
            ? plainButton("\u{1F513}", "Unblock", (e) => act("/widget/api/block", { account: p.npub, block: false }, e.target))
            : dangerButton("\u{1F6AB}", "Block", withConfirm("Block this account? This also unfollows it.",
                (btn) => act("/widget/api/block", { account: p.npub, block: true }, btn))),
        ]));
      }
      page.appendChild(el("div", { class: "sub" }, [el("a", { href: p.url, target: "_blank", rel: "noopener", text: "View on the web →" })]));
      renderProfilePostFeed(page, p.pubkey);
    }
    function refresh() {
      return api("/widget/api/browse/account?pubkey=" + encodeURIComponent(view.pubkey)).then(renderAccount);
    }
    refresh().catch((e) => {
      page.innerHTML = "";
      page.appendChild(el("div", { class: "muted", text: e.message || "Couldn't load that profile." }));
    });
  }

  function renderThreadPage(app, view) {
    app.appendChild(el("button", { class: "back-link", text: "← Back", onclick: () => { threadView = null; render(); } }));
    const page = el("div", { class: "profile-page thread-page" }, [el("div", { class: "muted", text: "Loading…" })]);
    app.appendChild(page);
    api("/widget/api/browse/thread?ref=" + encodeURIComponent(view.ref)).then((data) => {
      page.innerHTML = "";
      if (!data.post) { page.appendChild(el("div", { class: "muted", text: "Couldn't load that post." })); return; }
      const rootChain = el("div", { class: "thread-chain" });
      const chain = (data.ancestors || []).concat([data.post]);
      chain.forEach((p, i) => {
        const isFocus = i === chain.length - 1;
        if (!isFocus && p.counts && (p.counts.replies || 0) < 1) p.counts = Object.assign({}, p.counts, { replies: 1 });
        const card = renderLiveCard(p, { focused: isFocus });
        card.classList.add(isFocus ? "thread-focus" : "thread-ancestor");
        if (!isFocus) card.classList.add("chain-link");
        rootChain.appendChild(card);
      });
      page.appendChild(rootChain);
      if (data.replies && data.replies.length) {
        page.appendChild(el("h2", { text: "Replies" }));
        data.replies.forEach((reply) => {
          page.appendChild(el("div", { class: "card", style: "padding:0;" }, [renderLiveCard(reply)]));
        });
      }
    }).catch((e) => {
      page.innerHTML = "";
      page.appendChild(el("div", { class: "muted", text: e.message || "Couldn't load that thread." }));
    });
  }

  // -- Profile section --------------------------------------------------------

  function renderOwnProfileTab(app) {
    if (!ctx.has_profile) {
      app.appendChild(el("div", { class: "card" }, [
        el("h2", { text: "Get started" }),
        el("div", { class: "sub", text: "You don't have a Nostr identity yet." }),
        el("div", { class: "row" }, [
          primaryButton("\u{2795}", "Create my identity", openCreateIdentity),
        ]),
      ]));
      renderKeysCards(app);
      return;
    }
    const page = el("div", { class: "profile-page" }, [el("div", { class: "muted", text: "Loading…" })]);
    app.appendChild(page);
    const active = activeId();

    function upload(which, file) {
      api("/widget/api/profile/image?which=" + which + "&id=" + active.number, {
        method: "POST", headers: { "Content-Type": file.type || "application/octet-stream" }, raw: file,
      }).then(() => { toast(which === "avatar" ? "Avatar updated." : "Banner updated.", true); return loadContext(); })
        .catch((err) => toast(err.message, false));
    }

    function renderOwn(s) {
      page.innerHTML = "";
      page.appendChild(profileBanner(s.banner_url, s.editable ? (file) => upload("banner", file) : null));
      const followingStat = el("div", { class: "profile-stat clickable", text: "Following: " + s.following_count + "  ›" });
      followingStat.addEventListener("click", () => openFollowingList(s));
      const statsBox = el("div", { class: "profile-stats" }, [
        el("div", { class: "profile-stat", text: "Known followers: " + s.followers_count }),
        followingStat,
      ]);
      appendProfileIdentity(page, s.avatar_url, s.display_name || s.handle, s.handle, statsBox,
        s.editable ? (file) => upload("avatar", file) : null);
      page.appendChild(el("div", { class: "profile-handle" }, [
        el("span", { class: "mono", text: s.npub }),
        el("button", { class: "copy-btn", text: "⧉", title: "Copy npub", onclick: () => copyText(s.npub) }),
      ]));
      if (s.about) page.appendChild(el("div", { class: "profile-bio", text: s.about }));
      if (s.website) page.appendChild(el("div", { class: "sub" }, [el("a", { href: s.website, target: "_blank", rel: "noopener", text: s.website })]));
      if (s.lud16) page.appendChild(el("div", { class: "sub", text: "\u{26A1} " + s.lud16 }));
      const row = el("div", { class: "row" });
      if (s.editable) row.appendChild(primaryButton("\u{270F}\u{FE0F}", "Edit profile", () => openEditProfile(s)));
      else row.appendChild(el("span", { class: "sub", style: "margin:0;", text: "This identity has no Profile Room: its profile follows your Matrix name and avatar." }));
      row.appendChild(plainButton("\u{2B07}\u{FE0F}", "Import follows", (e) => doAction("/widget/api/import_follows", { id: String(s.number) }, e.target)));
      if (s.room_id) row.appendChild(linkButton("", "Open Profile Room", matrixToUrl(s.room_id)));
      page.appendChild(row);
      page.appendChild(el("div", { class: "sub" }, [el("a", { href: s.public_url, target: "_blank", rel: "noopener", text: "View on the web →" })]));
      renderIdentitiesCard(page);
      renderKeysCards(page);
      renderProfilePostFeed(page, s.pubkey);
    }
    api("/widget/api/profile?id=" + active.number).then(renderOwn).catch((e) => {
      page.innerHTML = "";
      page.appendChild(el("div", { class: "muted", text: e.message || "Couldn't load your profile." }));
    });
  }

  function openEditProfile(s) {
    const f = s.fields || {};
    const name = el("input", { type: "text", value: f.name || "", placeholder: "display name" });
    const about = el("textarea", { class: "form-textarea", rows: "4", placeholder: "about you" });
    about.value = f.about || "";
    const website = el("input", { type: "text", value: f.website || "", placeholder: "https://example.org" });
    const lud16 = el("input", { type: "text", value: f.lud16 || "", placeholder: "lightning address (name@example.org)" });
    const body = el("div", { class: "card profile-modal" }, [
      modalHeader("Edit profile"),
      el("div", { class: "sub", text: "This is your Profile Room's name and topic too. Changes are published to Nostr." }),
      el("div", { class: "row" }, [name]),
      el("div", { class: "row" }, [about]),
      el("div", { class: "row" }, [website]),
      el("div", { class: "row" }, [lud16]),
      el("div", { class: "row modal-actions" }, [
        plainButton("\u{2715}", "Cancel", closeModal),
        primaryButton("\u{1F4BE}", "Save", (e) => {
          e.target.disabled = true;
          api("/widget/api/profile/update", { method: "POST", body: {
            id: String(s.number), name: name.value, about: about.value, website: website.value, lud16: lud16.value,
          } }).then(() => { toast("Profile updated.", true); closeModal(); return loadContext(); })
            .catch((err) => toast(err.message, false))
            .finally(() => { e.target.disabled = false; });
        }),
      ]),
    ]);
    openModal(body);
  }

  function openFollowingList(s) {
    const body = el("div", { class: "card profile-modal" }, [el("div", { class: "muted", text: "Loading…" })]);
    openModal(body);
    function load() {
      api("/widget/api/following?id=" + s.number).then((data) => {
        body.innerHTML = "";
        body.appendChild(modalHeader("Following", [
          el("button", { class: "modal-close", text: "\u{2B07} Import follows", style: "font-size:13px;",
            onclick: (e) => doAction("/widget/api/import_follows", { id: String(s.number) }, e.target) }),
        ]));
        if (!data.following.length) { body.appendChild(el("div", { class: "muted", text: "Not following anyone yet." })); return; }
        body.appendChild(el("div", { class: "list" }, data.following.map((f) =>
          el("div", { class: "list-item" }, [
            el("div", { class: "clickable", style: "display:flex;align-items:center;gap:8px;min-width:0;",
              onclick: () => { closeModal(); openAccountProfile(f.pubkey); } }, [
              avatarEl(f.avatar_url, f.display_name, "sm"),
              el("span", { text: f.display_name, style: "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" }),
            ]),
            plainButton("\u{2796}", "Unfollow", (e) => doAction("/widget/api/unfollow", { account: f.npub }, e.target).then(load)),
          ])
        )));
      }).catch(() => { body.innerHTML = ""; body.appendChild(el("div", { class: "muted", text: "Couldn't load." })); });
    }
    load();
  }

  function renderIdentitiesCard(page) {
    const list = el("div", { class: "list" });
    ctx.ids.forEach((i) => {
      const meta = el("div", { style: "min-width:0;" }, [
        el("div", { style: "display:flex;align-items:center;gap:6px;flex-wrap:wrap;" }, [
          el("span", { style: "font-weight:600;", text: i.handle }),
          i.active ? el("span", { class: "badge", text: "active" }) : null,
          i.tier === "shadow" ? el("span", { class: "badge plain", text: "shadow" }) : null,
        ]),
        el("div", { class: "muted", style: "font-size:12px;", text: "key " + i.key + " · " + i.followers + " known followers" }),
      ]);
      list.appendChild(el("div", { class: "list-item" }, [
        meta,
        i.active ? null : plainButton("", "Switch", (e) => doAction("/widget/api/switch", { id: String(i.number) }, e.target)),
      ]));
    });
    const children = [el("h2", { text: "Your identities" }),
      el("div", { class: "sub", text: "Your Active identity is the one that posts, follows and replies for you." }), list];
    if (canCreateMore()) children.push(el("div", { class: "row", style: "margin-top:8px;" }, [plainButton("\u{2795}", "New identity", openCreateIdentity)]));
    page.appendChild(el("div", { class: "card", style: "margin-top:12px;" }, children));
  }

  // Bringing your own key, a remote signer, and exporting a key. Keys go
  // only to the bridge, over HTTPS, never into a room message.
  function renderKeysCards(page) {
    function modeFields() {
      const mode = el("select", {}, [el("option", { value: "new", text: "as a new identity" })].concat(
        ctx.ids.length ? [el("option", { value: "replace", text: "replacing an identity's key" })] : []));
      const name = el("input", { type: "text", placeholder: "name for the new identity" });
      const idSelect = el("select", {}, ctx.ids.map((i) => el("option", { value: String(i.number), text: i.handle })));
      const confirm = el("input", { type: "text", placeholder: "type the identity's name to confirm" });
      const warn = el("div", { class: "warn", style: "display:none", text: "Replacing a key loses the followers of the old key: they follow the key, not the handle." });
      const newRow = el("div", { class: "row" }, [name]);
      const replaceRow = el("div", { class: "row", style: "display:none" }, [idSelect, confirm]);
      const follows = el("input", { type: "checkbox", checked: "checked" });
      const posts = el("input", { type: "checkbox", checked: "checked" });
      mode.addEventListener("change", () => {
        const r = mode.value === "replace";
        newRow.style.display = r ? "none" : "";
        replaceRow.style.display = r ? "" : "none";
        warn.style.display = r ? "" : "none";
      });
      return {
        nodes: [el("div", { class: "row" }, [mode]), newRow, replaceRow, warn,
          el("div", { class: "row" }, [el("label", { class: "checkbox-label" }, [follows, el("span", { text: "bring in who this key follows" })])]),
          el("div", { class: "row" }, [el("label", { class: "checkbox-label" }, [posts, el("span", { text: "bring in its latest posts" })])])],
        body: () => {
          const b = { mode: mode.value, import_follows: follows.checked, import_posts: posts.checked ? 20 : 0 };
          if (mode.value === "new") b.name = name.value; else { b.id = idSelect.value; b.confirm = confirm.value; }
          return b;
        },
      };
    }
    const imp = modeFields();
    const keyInput = el("input", { type: "password", placeholder: "nsec1… or ncryptsec1… or hex", autocomplete: "off" });
    const pwInput = el("input", { type: "password", placeholder: "password (only for ncryptsec)", autocomplete: "off" });
    const sig = modeFields();
    const uriInput = el("input", { type: "text", placeholder: "bunker://…", autocomplete: "off" });
    const authBox = el("div");
    page.appendChild(el("div", { class: "card" }, [
      el("h2", { text: "Bring your own key" }),
      el("div", { class: "sub", text: "Use a Nostr key you already have, so your existing followers come with you. Keys go straight to the bridge over HTTPS and are stored encrypted; never paste one into a chat message." }),
      el("details", {}, [el("summary", { text: "Import an nsec or ncryptsec" }),
        el("div", { class: "row" }, [keyInput]), el("div", { class: "row" }, [pwInput])].concat(imp.nodes).concat([
        el("div", { class: "row" }, [primaryButton("", "Import key", (e) => {
          const body = Object.assign(imp.body(), { key: keyInput.value, password: pwInput.value });
          doAction("/widget/api/key/import", body, e.target).then(() => { keyInput.value = ""; pwInput.value = ""; });
        })]),
      ])),
      el("details", {}, [el("summary", { text: "Connect a remote signer (NIP-46)" }),
        el("div", { class: "sub", text: "Your key stays in your signer app (nsec.app, Amber, …). Paste its bunker:// address. Posting and DMs need the signer to be online." }),
        el("div", { class: "row" }, [uriInput])].concat(sig.nodes).concat([
        el("div", { class: "row" }, [primaryButton("", "Connect signer", (e) => {
          e.target.disabled = true;
          api("/widget/api/key/import", { method: "POST", body: Object.assign(sig.body(), { uri: uriInput.value }) })
            .then((r) => {
              authBox.innerHTML = "";
              if (r.auth_url) {
                authBox.appendChild(el("div", { class: "warn" }, [
                  el("span", { text: "Approve the bridge in your signer: " }),
                  el("a", { href: r.auth_url, target: "_blank", rel: "noopener", text: r.auth_url }),
                  el("span", { text: ", then press Connect again." }),
                ]));
              } else { toast(r.message || "Done.", true); return loadContext(); }
            })
            .catch((err) => toast(err.message, false))
            .finally(() => { e.target.disabled = false; });
        })]),
        authBox,
      ])),
    ]));
    const exportable = ctx.ids.filter((i) => i.exportable);
    if (!exportable.length) return;
    const exportSelect = el("select", {}, exportable.map((i) => el("option", { value: String(i.number), text: i.handle })));
    const exportPw = el("input", { type: "password", placeholder: "password to protect it (recommended)", autocomplete: "off" });
    const exported = el("div");
    page.appendChild(el("div", { class: "card" }, [
      el("h2", { text: "Export a key" }),
      el("div", { class: "warn", text: "Anyone with this key is that Nostr identity. Only export it to put it in your own Nostr app or signer." }),
      el("div", { class: "row" }, [exportSelect]),
      el("div", { class: "row" }, [exportPw]),
      el("div", { class: "row" }, [dangerButton("", "Show key", (e) => {
        e.target.disabled = true;
        api("/widget/api/key/export", { method: "POST", body: { id: exportSelect.value, password: exportPw.value } })
          .then((r) => {
            exported.innerHTML = "";
            exported.appendChild(el("div", { class: "sub", text: r.encrypted ? "Protected with your password (NIP-49):" : "Unprotected secret key:" }));
            exported.appendChild(el("div", { class: "secret", text: r.key }));
          })
          .catch((err) => toast(err.message, false))
          .finally(() => { e.target.disabled = false; });
      })]),
      exported,
    ]));
  }

  // -- Communities -------------------------------------------------------------

  let communitiesCache = null;

  function renderCommunitiesTab(app) {
    const linkInput = el("input", { type: "text", placeholder: "Concord invite link, or a NIP-29 group (naddr1… or relay.example'group)" });
    const nameInput = el("input", { type: "text", placeholder: "name of a new community" });
    app.appendChild(el("div", { class: "card" }, [
      el("h2", { text: "Join or create" }),
      el("div", { class: "sub", text: "Concord communities (Armada, Vector, …) are encrypted on Nostr, but stored readable on this homeserver once bridged. NIP-29 groups (Flotilla, Chachi, 0xchat, …) live on one relay. The bot answers in this room." }),
      el("div", { class: "row" }, [linkInput, primaryButton("\u{1F517}", "Join", (e) => {
        if (!linkInput.value.trim()) return;
        doAction("/widget/api/communities/join", { link: linkInput.value.trim() }, e.target).then(() => { linkInput.value = ""; communitiesCache = null; render(); });
      })]),
      el("div", { class: "row" }, [nameInput, plainButton("\u{2795}", "Create community", (e) => {
        if (!nameInput.value.trim()) return;
        doAction("/widget/api/communities/create", { name: nameInput.value.trim() }, e.target).then(() => { nameInput.value = ""; communitiesCache = null; render(); });
      })]),
    ]));
    const concordList = el("div", { class: "list" }, [el("div", { class: "muted", text: "Loading…" })]);
    const groupList = el("div", { class: "list" }, [el("div", { class: "muted", text: "Loading…" })]);
    app.appendChild(el("div", { class: "card" }, [el("h2", { text: "Concord communities" }), concordList]));
    app.appendChild(el("div", { class: "card" }, [el("h2", { text: "NIP-29 groups" }), groupList]));
    api("/widget/api/communities").then((data) => {
      communitiesCache = data;
      concordList.innerHTML = "";
      if (!data.concord.length) concordList.appendChild(el("div", { class: "muted", text: "None yet." }));
      data.concord.forEach((c) => {
        concordList.appendChild(el("div", { class: "list-item clickable", onclick: (e) => {
          if (e.target.closest("a, button")) return;
          clearOverlays(); communityView = { id: c.id }; render();
        } }, [
          el("div", { style: "min-width:0;" }, [
            el("div", { style: "display:flex;align-items:center;gap:6px;flex-wrap:wrap;" }, [
              el("span", { style: "font-weight:600;", text: c.name }),
              el("span", { class: "badge" + (c.role === "member" ? " plain" : ""), text: c.role }),
            ]),
            el("div", { class: "muted", style: "font-size:12px;", text: c.channels.length + " channels · as " + c.as.join(", ") }),
          ]),
          el("div", { class: "row", style: "margin:0;flex-wrap:nowrap;" }, [
            c.space_id ? linkButton("", "Open", matrixToUrl(c.space_id)) : null,
          ]),
        ]));
      });
      groupList.innerHTML = "";
      if (!data.groups.length) groupList.appendChild(el("div", { class: "muted", text: "None yet." }));
      data.groups.forEach((g) => {
        groupList.appendChild(el("div", { class: "list-item" }, [
          el("div", { style: "min-width:0;" }, [
            el("div", { style: "display:flex;align-items:center;gap:6px;flex-wrap:wrap;" }, [
              el("span", { style: "font-weight:600;", text: g.name }),
              el("span", { class: "badge" + (g.role === "member" ? " plain" : ""), text: g.role }),
              g.private ? el("span", { class: "badge plain", text: "private" }) : null,
            ]),
            el("div", { class: "muted", style: "font-size:12px;", text: "on " + g.host + " · as " + g.as.join(", ") }),
          ]),
          el("div", { class: "row", style: "margin:0;flex-wrap:nowrap;" }, [
            g.room_id ? linkButton("", "Open", matrixToUrl(g.room_id)) : null,
            dangerButton("\u{1F6AA}", "Leave", withConfirm("Leave " + g.name + "?", (btn) => leaveCommunity("group", g.key, btn))),
          ]),
        ]));
      });
    }).catch(() => {
      concordList.innerHTML = ""; groupList.innerHTML = "";
      concordList.appendChild(el("div", { class: "muted", text: "Couldn't load." }));
    });
  }

  function leaveCommunity(type, id, btn) {
    return api("/widget/api/communities/leave", { method: "POST", body: { type: type, id: id } })
      .then((data) => { toast(data.message || "Left.", true); communityView = null; communitiesCache = null; render(); })
      .catch((e) => { toast(e.message, false); if (btn) btn.disabled = false; });
  }

  function renderCommunityPage(app, view) {
    app.appendChild(el("button", { class: "back-link", text: "← Back", onclick: () => { communityView = null; render(); } }));
    const page = el("div", { class: "profile-page" }, [el("div", { class: "muted", text: "Loading…" })]);
    app.appendChild(page);
    function draw(data) {
      const c = data.concord.find((x) => x.id === view.id);
      page.innerHTML = "";
      if (!c) { page.appendChild(el("div", { class: "muted", text: "You're not in that community any more." })); return; }
      const head = el("div", { class: "card" }, [
        el("h1", {}, [el("span", { text: c.name }), el("span", { class: "badge" + (c.role === "member" ? " plain" : ""), text: c.role })]),
        c.description ? el("div", { class: "sub", text: c.description }) : null,
        el("div", { class: "sub", text: c.members + " members · you're in as " + c.as.join(", ") }),
        el("div", { class: "row" }, [
          c.space_id ? linkButton("", "Open space", matrixToUrl(c.space_id)) : null,
          c.can_leave ? dangerButton("\u{1F6AA}", "Leave", withConfirm("Leave " + c.name + "? You'll be taken out of its rooms.", (btn) => leaveCommunity("concord", c.id, btn))) : null,
        ]),
      ]);
      page.appendChild(head);
      page.appendChild(el("div", { class: "card" }, [
        el("h2", { text: "Channels" }),
        el("div", { class: "list" }, c.channels.length ? c.channels.map((ch) => el("div", { class: "list-item" }, [
          el("span", { text: "#" + ch.name + (ch.private ? " (private)" : "") }),
          ch.room_id ? linkButton("", "Open", matrixToUrl(ch.room_id)) : null,
        ])) : [el("div", { class: "muted", text: "No channels you can read." })]),
      ]));
      if (c.can_add_channels) {
        const channelName = el("input", { type: "text", placeholder: "channel name" });
        page.appendChild(el("div", { class: "card" }, [
          el("h2", { text: "Add a channel" }),
          el("div", { class: "sub", text: "Makes the room this widget is in a channel of " + c.name + ". It keeps its name, avatar and members." }),
          el("div", { class: "row" }, [channelName, primaryButton("\u{2795}", "Make this room a channel", (e) => {
            if (!channelName.value.trim()) return;
            doAction("/widget/api/communities/concord/channel", { community: c.id, name: channelName.value.trim() }, e.target).then(() => render());
          })]),
        ]));
      }
      if (c.can_invite) {
        const who = el("input", { type: "text", placeholder: "npub, name@domain or a Matrix ghost's ID" });
        page.appendChild(el("div", { class: "card" }, [
          el("h2", { text: "Invite someone" }),
          el("div", { class: "sub", text: "A direct invite, sent over Nostr to their Concord app. Someone with an identity here is invited to the space instead." }),
          el("div", { class: "row" }, [who, primaryButton("\u{2709}\u{FE0F}", "Invite", (e) => {
            if (!who.value.trim()) return;
            doAction("/widget/api/communities/concord/invite", { community: c.id, who: who.value.trim() }, e.target).then(() => { who.value = ""; });
          })]),
        ]));
      }
      if (c.can_manage_links) {
        const label = el("input", { type: "text", placeholder: "label (optional)" });
        page.appendChild(el("div", { class: "card" }, [
          el("h2", { text: "Invite links" }),
          el("div", { class: "sub", text: "Anyone with a link can join, in Armada, Vector or here." }),
          el("div", { class: "list" }, c.links.length ? c.links.map((l) => el("div", { class: "list-item" }, [
            el("div", { style: "min-width:0;" }, [
              el("div", { style: "font-weight:600;", text: l.label || "(no label)" }),
              el("div", { class: "mono muted", text: l.url }),
            ]),
            el("div", { class: "row", style: "margin:0;flex-wrap:nowrap;" }, [
              plainButton("", "Copy", () => copyText(l.url)),
              dangerButton("", "Revoke", withConfirm("Retire this link? People who joined with it stay members.", (btn) =>
                doAction("/widget/api/communities/concord/revoke", { community: c.id, number: String(l.number) }, btn).then(() => render()))),
            ]),
          ])) : [el("div", { class: "muted", text: "No links yet." })]),
          el("div", { class: "row", style: "margin-top:8px;" }, [label, primaryButton("\u{1F517}", "Make a link", (e) =>
            doAction("/widget/api/communities/concord/link", { community: c.id, label: label.value.trim() }, e.target).then(() => render()))]),
        ]));
      }
    }
    api("/widget/api/communities").then(draw).catch((e) => {
      page.innerHTML = "";
      page.appendChild(el("div", { class: "muted", text: e.message || "Couldn't load." }));
    });
  }

  // -- Settings -----------------------------------------------------------------

  const RECOVERY_ROWS = [
    { kind: "profile", label: "Profile Room" },
    { kind: "notifications", label: "Notifications Room" },
    { kind: "space", label: "Space" },
  ];

  function renderRoomRecoveryCard(app) {
    const list = el("div", { class: "list" }, [el("div", { class: "muted", text: "Loading…" })]);
    app.appendChild(el("div", { class: "card" }, [
      el("h2", { text: "Room recovery" }),
      el("div", { class: "sub", text: "Locked out of one of these? Re-invite yourself." }),
      list,
    ]));
    function load() {
      api("/widget/api/recovery_rooms").then((data) => {
        list.innerHTML = "";
        RECOVERY_ROWS.forEach((row) => {
          const info = data[row.kind] || { room_id: null, status: "not_created" };
          const meta = el("div", { style: "min-width:0;" }, [el("div", { text: row.label, style: "font-weight:600;font-size:13px;" })]);
          if (info.room_id) meta.appendChild(el("div", { class: "muted", text: info.room_id, style: "font-size:11px;font-family:monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" }));
          let actionBtn;
          if (info.status === "open") actionBtn = linkButton("", "Open", matrixToUrl(info.room_id, null));
          else if (info.status === "not_created") actionBtn = el("button", { disabled: "disabled", text: "Not Created", title: "Not created yet" });
          else {
            actionBtn = plainButton("\u{1F504}", "Rejoin", (e) => {
              e.target.disabled = true;
              api("/widget/api/rejoin_room", { method: "POST", body: { kind: row.kind } })
                .then(() => { toast("Invited, check your rooms.", true); load(); })
                .catch((err) => { toast(err.message, false); e.target.disabled = false; });
            });
          }
          list.appendChild(el("div", { class: "list-item" }, [meta, actionBtn]));
        });
      }).catch(() => { list.innerHTML = ""; list.appendChild(el("div", { class: "muted", text: "Couldn't load." })); });
    }
    load();
  }

  function renderDmFiltersCard(app) {
    const keywords = el("textarea", { class: "form-textarea", rows: "3", placeholder: "one word or phrase per line" });
    const languages = el("input", { type: "text", placeholder: "allowed languages, e.g. en de (empty: any)" });
    const noName = el("input", { type: "checkbox" });
    const domains = el("textarea", { class: "form-textarea", rows: "2", placeholder: "one domain per line" });
    const status = el("div", { class: "sub", text: "Loading…" });
    const lines = (node) => node.value.split("\n").map((s) => s.trim()).filter((s) => s.length);
    app.appendChild(el("div", { class: "card" }, [
      el("h2", { text: "DM request filters" }),
      el("div", { class: "sub", text: "Messages from accounts you don't follow, that would open a new DM room, are dropped quietly when they match. For all your identities; open conversations are never filtered." }),
      status,
      el("div", { class: "sub", text: "Keywords", style: "margin:0 0 4px;" }), el("div", { class: "row" }, [keywords]),
      el("div", { class: "row" }, [languages]),
      el("div", { class: "row" }, [el("label", { class: "checkbox-label" }, [noName, el("span", { text: "filter senders whose profile has no name" })])]),
      el("div", { class: "sub", text: "NIP-05 domains", style: "margin:0 0 4px;" }), el("div", { class: "row" }, [domains]),
      el("div", { class: "row" }, [primaryButton("\u{1F4BE}", "Save filters", (e) => {
        doAction("/widget/api/dm_filters", {
          keywords: lines(keywords), domains: lines(domains), require_name: noName.checked,
          languages: languages.value.split(/[\s,]+/).filter((s) => s.length),
        }, e.target);
      })]),
    ]));
    api("/widget/api/dm_filters").then((f) => {
      keywords.value = f.keywords.join("\n");
      languages.value = f.languages.join(" ");
      noName.checked = !!f.require_name;
      domains.value = f.domains.join("\n");
      status.textContent = "Requests filtered so far: " + f.filtered + ".";
    }).catch(() => { status.textContent = "Couldn't load."; });
  }

  function renderSettingsTab(app) {
    if (!ctx.has_profile) {
      app.appendChild(el("div", { class: "card" }, [
        el("h2", { text: "Get started" }),
        el("div", { class: "sub", text: "You don't have a Nostr identity yet." }),
        el("div", { class: "row" }, [primaryButton("\u{2795}", "Create my identity", openCreateIdentity)]),
      ]));
    }
    renderThisRoomSection(app);
    renderRoomRecoveryCard(app);
    renderDmFiltersCard(app);
  }

  // What can be done about the room the widget is in.
  function renderThisRoomSection(app) {
    const r = ctx.room;
    const kind = r.kind;
    if (kind === "profile") {
      const card = el("div", { class: "card" }, [el("h2", { text: "This room: " + (r.mine ? "your Profile Room" : "a Profile Room") })]);
      if (r.mine) {
        const newName = el("input", { type: "text", placeholder: "new name for " + r.id });
        card.appendChild(el("div", { class: "sub", text: "Its name, topic and avatar are the Nostr profile of " + r.id + "@" + ctx.domain + "." }));
        card.appendChild(el("div", { class: "row" }, [newName, plainButton("", "Rename", withConfirm(
          "Rename this identity? The bot asks you to confirm in the room; the old name keeps working for a while.",
          (btn) => doAction("/widget/api/rename", { id: String(r.number), new_name: newName.value.trim() }, btn)))]));
        card.appendChild(el("div", { class: "row" }, [
          dangerButton("\u{2702}\u{FE0F}", "Unlink this room", withConfirm(
            "Detach this room from your identity? The bot asks you to confirm in the room.",
            (btn) => doAction("/widget/api/unlink", { id: String(r.number) }, btn))),
          dangerButton("\u{1F5D1}\u{FE0F}", "Delete identity", withConfirm(
            "Delete this Nostr identity? The bot asks you to confirm in the room.",
            (btn) => doAction("/widget/api/delete_profile", { id: String(r.number) }, btn))),
        ]));
      }
      app.appendChild(card);
    } else if (kind === "remote") {
      const following = r.following.length > 0;
      const blocked = r.blocked.length > 0;
      const countInput = el("input", { type: "number", min: "1", max: "100", value: String(ctx.backfill_default || 20), style: "max-width:110px;flex:none;" });
      app.appendChild(el("div", { class: "card" }, [
        el("h2", { text: "This room: " + (r.name || r.nip05 || "a Nostr account") }),
        el("div", { class: "sub", text: following ? "Followed by your " + r.following.join(", ") + "." : "Not followed by your identities." }),
        el("div", { class: "row" }, [
          plainButton("\u{1F441}\u{FE0F}", "View full profile", () => openAccountProfile(r.pubkey)),
          following
            ? dangerButton("\u{2796}", "Unfollow", (e) => doAction("/widget/api/unfollow", { account: r.npub }, e.target))
            : primaryButton("\u{2795}", "Follow", (e) => doAction("/widget/api/follow", { account: r.npub }, e.target)),
          plainButton("\u{2709}\u{FE0F}", "DM", (e) => doAction("/widget/api/dm", { account: r.npub }, e.target)),
          blocked
            ? plainButton("\u{1F513}", "Unblock", (e) => doAction("/widget/api/block", { account: r.npub, block: false }, e.target))
            : dangerButton("\u{1F6AB}", "Block", withConfirm("Block this account?", (btn) => doAction("/widget/api/block", { account: r.npub, block: true }, btn))),
        ]),
        el("div", { class: "row" }, [countInput, plainButton("\u{2B07}\u{FE0F}", "Bring in older posts", (e) =>
          doAction("/widget/api/backfill", { count: countInput.value }, e.target))]),
      ]));
    } else if (kind === "dm") {
      app.appendChild(el("div", { class: "card" }, [
        el("h2", { text: "This room: Direct message" }),
        el("div", { class: "row" }, [
          plainButton("\u{1F441}\u{FE0F}", "View their profile", () => openAccountProfile(r.pubkey)),
          dangerButton("\u{1F6AB}", "Block", withConfirm("Block this account?", (btn) => doAction("/widget/api/block", { account: r.npub, block: true }, btn))),
        ]),
      ]));
    } else if (kind === "community" || kind === "group") {
      app.appendChild(el("div", { class: "card" }, [
        el("h2", { text: "This room: " + (kind === "community" ? "a Concord community" : "a NIP-29 group") }),
        el("div", { class: "sub", text: r.name }),
        el("div", { class: "row" }, [
          kind === "community" ? plainButton("", "Manage", () => { clearOverlays(); communityView = { id: r.community_id }; render(); }) : null,
          dangerButton("\u{1F6AA}", "Leave", withConfirm("Leave " + r.name + "?", (btn) => leaveCommunity(r.type, r.community_id, btn).then(() => loadContext()))),
        ]),
      ]));
    } else if (kind === "notifications" || kind === "space") {
      app.appendChild(el("div", { class: "card" }, [
        el("h2", { text: "This room: " + (kind === "notifications" ? "Notifications" : "your Nostr space") }),
        el("div", { class: "sub", text: kind === "notifications" ? "Where the bridge tells you about mentions, replies, DM requests and more." : "Where your Nostr rooms are gathered." }),
      ]));
    }
  }

  // -- Admin ------------------------------------------------------------------------

  function renderAdminTab(app) {
    app.appendChild(el("div", { class: "row" }, [
      toggleButton("\u{1F511}", "Access", adminSubTab === "access", () => { adminSubTab = "access"; render(); }),
      toggleButton("\u{1FAAA}", "Identities", adminSubTab === "identities", () => { adminSubTab = "identities"; render(); }),
      toggleButton("\u{1F6AA}", "Rooms", adminSubTab === "rooms", () => { adminSubTab = "rooms"; render(); }),
      toggleButton("\u{2699}\u{FE0F}", "Settings", adminSubTab === "settings", () => { adminSubTab = "settings"; render(); }),
    ]));
    if (adminSubTab === "identities") renderAdminIdentitiesSubTab(app);
    else if (adminSubTab === "rooms") renderAdminRoomsSubTab(app);
    else if (adminSubTab === "settings") renderAdminSettingsSubTab(app);
    else renderAdminAccessSubTab(app);
  }

  function renderAdminAccessSubTab(app) {
    const RULE_TYPES = ["mxid", "homeserver", "room"];
    const selectStyle = "font:inherit;font-size:13px;border:1px solid var(--border);background:var(--bg);color:var(--text);border-radius:var(--radius-inner);padding:8px 10px;flex:none;";
    const typeSelect = el("select", { style: selectStyle }, RULE_TYPES.map((t) => el("option", { value: t, text: t })));
    const tierSelect = el("select", { style: selectStyle }, ["shadow", "full"].map((t) => el("option", { value: t, text: t })));
    const valueInput = el("input", { type: "text", placeholder: "@user:example.org, example.org, !room:example.org, or #alias:example.org" });
    function syncTierVisibility() { tierSelect.style.display = typeSelect.value === "room" ? "none" : ""; }
    typeSelect.addEventListener("change", syncTierVisibility);
    syncTierVisibility();
    const statusLine = el("div", { class: "sub", text: "" });
    const list = el("div", { class: "list" }, [el("div", { class: "muted", text: "Loading…" })]);

    function loadGrants() {
      api("/widget/api/admin/grants").then((data) => {
        statusLine.textContent = "Third-party access is currently " + (data.third_party_access ? "ON" : "OFF") + ".";
        list.innerHTML = "";
        if (!data.grants.length) { list.appendChild(el("div", { class: "muted", text: "No grants yet." })); return; }
        data.grants.forEach((g) => {
          const banned = g.tier === "banned";
          list.appendChild(el("div", { class: "list-item" }, [
            el("div", {}, [
              el("div", { style: "font-weight:600;word-break:break-all;", text: g.rule_type + ": " + g.value }),
              el("div", { class: "muted", style: "font-size:12px;margin-top:2px;", text: "tier: " + g.tier + " · granted by " + g.granted_by }),
            ]),
            banned
              ? plainButton("", "Unban", (e) => doAction("/widget/api/admin/allow", { rule_type: g.rule_type, value: g.value, tier: "shadow" }, e.target).then(loadGrants))
              : dangerButton("\u{1F5D1}\u{FE0F}", "Revoke", (e) => doAction("/widget/api/admin/disallow", { rule_type: g.rule_type, value: g.value }, e.target).then(loadGrants)),
          ]));
        });
      }).catch(() => { list.innerHTML = ""; list.appendChild(el("div", { class: "muted", text: "Couldn't load." })); });
    }

    app.appendChild(el("div", { class: "card" }, [
      el("h2", { text: "Third-party access" }),
      el("div", { class: "sub", text: "Let a user, a homeserver, or a single already-bridged room's replies and reactions use this bridge. Mxid and homeserver grants are shadow (an identity made for them, mirroring their Matrix name and avatar) or full (their own profiles); a room is always shadow. A homeserver grant asks for a confirm reply in the room." }),
      statusLine,
      el("div", { class: "row" }, [
        typeSelect, valueInput, tierSelect,
        primaryButton("\u{2705}", "Grant", (e) => {
          if (!valueInput.value.trim()) return;
          const body = { rule_type: typeSelect.value, value: valueInput.value.trim() };
          if (typeSelect.value !== "room") body.tier = tierSelect.value;
          doAction("/widget/api/admin/allow", body, e.target).then(() => { valueInput.value = ""; loadGrants(); });
        }),
        dangerButton("\u{1F6AB}", "Disallow", (e) => {
          if (!valueInput.value.trim()) return;
          doAction("/widget/api/admin/disallow", { rule_type: typeSelect.value, value: valueInput.value.trim() }, e.target)
            .then(() => { valueInput.value = ""; loadGrants(); });
        }),
      ]),
      list,
    ]));
    loadGrants();

    const banKind = el("select", { style: selectStyle }, ["pubkey", "ip"].map((t) => el("option", { value: t, text: t === "pubkey" ? "account" : "IP" })));
    const banKey = el("input", { type: "text", placeholder: "npub, hex key or IP address" });
    const banDuration = el("select", { style: selectStyle }, ["1h", "6h", "1d", "7d", "30d", "forever"].map((d) => {
      const attrs = { value: d, text: d };
      if (d === "7d") attrs.selected = "selected";
      return el("option", attrs);
    }));
    const banReason = el("input", { type: "text", placeholder: "reason (optional)" });
    const banList = el("div", { class: "list" }, [el("div", { class: "muted", text: "Loading…" })]);
    function loadBans() {
      api("/widget/api/admin/bans").then((data) => {
        banList.innerHTML = "";
        if (!data.bans.length) { banList.appendChild(el("div", { class: "muted", text: "No bans." })); return; }
        data.bans.forEach((b) => {
          banList.appendChild(el("div", { class: "list-item" }, [
            el("div", { style: "min-width:0;" }, [
              el("div", { style: "font-weight:600;word-break:break-all;", text: (b.kind === "pubkey" ? (b.npub || b.key) : b.kind + " " + b.key) }),
              el("div", { class: "muted", style: "font-size:12px;margin-top:2px;", text: "until " + (b.until || "forever") + " · " + b.reason }),
            ]),
            (b.kind === "pubkey" || b.kind === "ip")
              ? plainButton("", "Unban", (e) => doAction("/widget/api/admin/unban", { kind: b.kind, key: b.key }, e.target).then(loadBans))
              : null,
          ]));
        });
      }).catch(() => { banList.innerHTML = ""; banList.appendChild(el("div", { class: "muted", text: "Couldn't load." })); });
    }
    app.appendChild(el("div", { class: "card" }, [
      el("h2", { text: "Relay bans" }),
      el("div", { class: "sub", text: "Accounts and IPs the bridge's relay refuses, set by hand or automatically by the spam guard." }),
      el("div", { class: "row" }, [banKind, banKey]),
      el("div", { class: "row" }, [banDuration, banReason, dangerButton("\u{1F6AB}", "Ban", (e) => {
        if (!banKey.value.trim()) return;
        doAction("/widget/api/admin/ban", { kind: banKind.value, key: banKey.value.trim(), duration: banDuration.value, reason: banReason.value.trim() }, e.target)
          .then(() => { banKey.value = ""; banReason.value = ""; loadBans(); });
      })]),
      banList,
    ]));
    loadBans();
  }

  function renderAdminIdentitiesSubTab(app) {
    let allItems = [];
    const filterInput = el("input", { type: "text", placeholder: "Filter by name, owner, npub…" });
    const kindSelect = el("select", { style: "font:inherit;font-size:13px;border:1px solid var(--border);background:var(--bg);color:var(--text);border-radius:var(--radius-inner);padding:8px 10px;" },
      ["all", "profile", "shadow"].map((k) => el("option", { value: k, text: k === "all" ? "All kinds" : k })));
    const countLine = el("div", { class: "sub", text: "" });
    const list = el("div", { class: "list" }, [el("div", { class: "muted", text: "Loading…" })]);

    function renderList() {
      const q = filterInput.value.trim().toLowerCase();
      const kind = kindSelect.value;
      const filtered = allItems.filter((it) => {
        if (kind !== "all" && it.kind !== kind) return false;
        if (!q) return true;
        return (it.handle + " " + it.npub + " " + it.owner_matrix_id + " " + (it.room_id || "")).toLowerCase().indexOf(q) !== -1;
      });
      list.innerHTML = "";
      countLine.textContent = filtered.length + " of " + allItems.length + " identities.";
      if (!filtered.length) { list.appendChild(el("div", { class: "muted", text: "No matching identities." })); return; }
      filtered.forEach((it) => {
        const badgeRow = [el("span", { class: "badge", text: it.kind })];
        if (it.tier) badgeRow.push(el("span", { class: "badge" + (it.tier === "shadow" ? " plain" : ""), style: "margin-left:4px;", text: it.tier }));
        if (it.key_origin === "bunker") badgeRow.push(el("span", { class: "badge plain", style: "margin-left:4px;", text: "remote signer" }));
        if (it.is_third_party) badgeRow.push(el("span", { class: "badge plain", style: "margin-left:4px;", text: "third-party" }));
        list.appendChild(el("div", { class: "list-item" }, [
          el("div", { style: "flex:1;min-width:0;" }, [
            el("div", { style: "display:flex;align-items:center;gap:4px;flex-wrap:wrap;" }, badgeRow.concat([
              el("span", { style: "font-weight:600;word-break:break-all;", text: it.handle }),
            ])),
            el("div", { class: "muted", style: "font-size:12px;margin-top:2px;word-break:break-all;",
              text: "owner: " + it.owner_matrix_id + (it.room_id ? " · room: " + it.room_id : " · no Profile Room") }),
            el("div", { class: "mono muted", text: it.npub }),
          ]),
        ]));
      });
    }
    filterInput.addEventListener("input", renderList);
    kindSelect.addEventListener("change", renderList);
    app.appendChild(el("div", { class: "card" }, [
      el("h2", { text: "Identities" }),
      el("div", { class: "sub", text: "Every Nostr identity this bridge holds: profiles, and shadow identities made automatically." }),
      el("div", { class: "row" }, [filterInput, kindSelect]),
      countLine,
      list,
    ]));
    api("/widget/api/admin/identities").then((data) => { allItems = data.identities; renderList(); })
      .catch(() => { list.innerHTML = ""; list.appendChild(el("div", { class: "muted", text: "Couldn't load." })); });
  }

  function renderAdminRoomsSubTab(app) {
    let allItems = [];
    const filterInput = el("input", { type: "text", placeholder: "Filter by room id, maps to, owner…" });
    const kindSelect = el("select", { style: "font:inherit;font-size:13px;border:1px solid var(--border);background:var(--bg);color:var(--text);border-radius:var(--radius-inner);padding:8px 10px;" },
      [el("option", { value: "all", text: "All kinds" })]);
    const countLine = el("div", { class: "sub", text: "" });
    const list = el("div", { class: "list" }, [el("div", { class: "muted", text: "Loading…" })]);

    function renderList() {
      const q = filterInput.value.trim().toLowerCase();
      const kind = kindSelect.value;
      const filtered = allItems.filter((it) => {
        if (kind !== "all" && it.kind !== kind) return false;
        if (!q) return true;
        return (it.room_id + " " + it.kind + " " + it.maps_to + " " + (it.owner_matrix_id || "")).toLowerCase().indexOf(q) !== -1;
      });
      list.innerHTML = "";
      countLine.textContent = filtered.length + " of " + allItems.length + " rooms.";
      if (!filtered.length) { list.appendChild(el("div", { class: "muted", text: "No matching rooms." })); return; }
      filtered.forEach((it) => {
        list.appendChild(el("div", { class: "list-item" }, [
          el("div", { style: "flex:1;min-width:0;" }, [
            el("div", { style: "display:flex;align-items:center;gap:6px;flex-wrap:wrap;" }, [
              el("span", { class: "badge", text: it.kind }),
              el("span", { style: "font-weight:600;word-break:break-all;", text: it.room_id }),
            ]),
            el("div", { class: "muted", style: "font-size:12px;margin-top:2px;word-break:break-all;",
              text: "maps to: " + it.maps_to + " · owner: " + (it.owner_matrix_id || "shared, no single owner") }),
          ]),
        ]));
      });
    }
    filterInput.addEventListener("input", renderList);

    const backstageBody = el("div", { class: "muted", text: "Loading…" });
    app.appendChild(el("div", { class: "card" }, [
      el("h2", { text: "Backstage space" }),
      el("div", { class: "sub", text: "A single shared space every ghost joins once, so the rooms ghosts are added to (Remote User Rooms, groups, community channels, ...) can let them join by themselves instead of being invited every time." }),
      backstageBody,
    ]));
    api("/widget/api/admin/backstage_space").then((data) => {
      backstageBody.innerHTML = "";
      if (!data.enabled) {
        backstageBody.appendChild(el("div", { class: "muted", text: "Disabled (bridge.backstage_space_enabled is off)." + (data.room_id ? " Room ID from before it was disabled: " + data.room_id : "") }));
        return;
      }
      backstageBody.appendChild(el("div", { style: "display:flex;align-items:center;gap:8px;flex-wrap:wrap;" }, [
        el("span", { style: "font-weight:600;word-break:break-all;", text: data.room_id || "(not created yet)" }),
        plainButton("", "Receive Invite",
          withConfirm("Receive an invite to the backstage space?", (btn) => {
            return api("/widget/api/admin/backstage_space/invite", { method: "POST", body: {} })
              .then(() => toast("Invited, check your invites.", true))
              .catch((err) => toast(err.message, false))
              .finally(() => { btn.disabled = false; });
          })),
      ]));
    }).catch(() => { backstageBody.innerHTML = ""; backstageBody.appendChild(el("div", { class: "muted", text: "Couldn't load." })); });

    app.appendChild(el("div", { class: "card" }, [
      el("h2", { text: "Mapped rooms" }),
      el("div", { class: "sub", text: "Every Matrix room this bridge keeps for something on Nostr." }),
      el("div", { class: "row" }, [filterInput, kindSelect]),
      countLine,
      list,
    ]));
    api("/widget/api/admin/rooms").then((data) => {
      allItems = data.rooms;
      const kinds = Array.from(new Set(allItems.map((it) => it.kind))).sort();
      kinds.forEach((k) => kindSelect.appendChild(el("option", { value: k, text: k })));
      kindSelect.addEventListener("change", renderList);
      renderList();
    }).catch(() => { list.innerHTML = ""; list.appendChild(el("div", { class: "muted", text: "Couldn't load." })); });
  }

  function renderAdminSettingsSubTab(app) {
    // Every setting is listed, editable or not, so this page also documents
    // them. A secret's value never reaches the page.
    let allItems = [];
    const selectStyle = "font:inherit;font-size:13px;border:1px solid var(--border);background:var(--bg);color:var(--text);border-radius:var(--radius-inner);padding:8px 10px;";
    const filterInput = el("input", { type: "text", placeholder: "Filter by key, label, description…" });
    const sectionSelect = el("select", { style: selectStyle }, [el("option", { value: "all", text: "All sections" })]);
    const countLine = el("div", { class: "sub", text: "" });
    const banner = el("div", { class: "muted", style: "display:none;margin-top:4px;" });
    const list = el("div", { class: "list" }, [el("div", { class: "muted", text: "Loading…" })]);

    function valueControl(item) {
      if (item.sensitive) {
        return el("div", { class: "muted", style: "font-size:12px;", text: "\u{1F512} Set in config.yaml, not shown here." });
      }
      if (item.type === "bool") {
        return el("input", Object.assign({ type: "checkbox" }, item.value ? { checked: "checked" } : {}, item.editable ? {} : { disabled: "disabled" }));
      }
      if (item.type === "enum") {
        return el("select", Object.assign({ style: selectStyle }, item.editable ? {} : { disabled: "disabled" }),
          (item.enum_choices || []).map((choice) =>
            el("option", Object.assign({ value: choice, text: choice }, choice === item.value ? { selected: "selected" } : {}))));
      }
      if (item.type === "list_str") {
        return el("textarea", Object.assign(
          {
            rows: "3",
            style: "width:100%;font:inherit;font-size:13px;border:1px solid var(--border);background:var(--bg);color:var(--text);border-radius:var(--radius-inner);padding:8px 10px;",
          },
          item.editable ? {} : { disabled: "disabled" },
          { text: (item.value || []).join("\n") },
        ));
      }
      const inputType = (item.type === "int" || item.type === "float") ? "number" : "text";
      return el("input", Object.assign(
        { type: inputType, value: item.value === null || item.value === undefined ? "" : String(item.value) },
        item.type === "float" ? { step: "any" } : {},
        item.editable ? {} : { disabled: "disabled" },
      ));
    }

    function coerceValue(item, control) {
      if (item.type === "bool") return control.checked;
      if (item.type === "int") {
        const n = parseInt(control.value, 10);
        if (Number.isNaN(n)) throw new Error("Enter a whole number.");
        return n;
      }
      if (item.type === "float") {
        const n = parseFloat(control.value);
        if (Number.isNaN(n)) throw new Error("Enter a number.");
        return n;
      }
      if (item.type === "list_str") {
        return control.value.split("\n").map((s) => s.trim()).filter((s) => s.length);
      }
      return control.value;
    }

    function renderList() {
      const q = filterInput.value.trim().toLowerCase();
      const section = sectionSelect.value;
      const filtered = allItems.filter((it) => {
        if (section !== "all" && it.section !== section) return false;
        if (!q) return true;
        return (it.key + " " + it.label + " " + it.description).toLowerCase().indexOf(q) !== -1;
      });
      list.innerHTML = "";
      countLine.textContent = filtered.length + " of " + allItems.length + " settings.";
      if (!filtered.length) {
        list.appendChild(el("div", { class: "muted", text: "No matching settings." }));
        return;
      }
      filtered.forEach((it) => {
        const control = valueControl(it);
        const badges = [el("span", { class: "badge", text: it.section })];
        if (it.sensitive) {
          badges.push(el("span", { class: "badge", style: "background:var(--hover);color:var(--text);margin-left:4px;", text: "secret" }));
        } else if (!it.editable) {
          badges.push(el("span", { class: "badge", style: "background:var(--hover);color:var(--text);margin-left:4px;", text: "config.yaml only" }));
        } else {
          badges.push(el("span", {
            class: "badge", style: "background:var(--hover);color:var(--text);margin-left:4px;",
            text: it.source === "database" ? "database override" : "config.yaml",
          }));
        }

        const actions = [];
        if (it.editable) {
          actions.push(primaryButton("\u{1F4BE}", "Save", (e) => {
            let value;
            try { value = coerceValue(it, control); } catch (err) { toast(err.message, false); return; }
            doAction("/widget/api/admin/settings/" + encodeURIComponent(it.key), { value: value }, e.target).then(loadSettings);
          }));
          if (it.source === "database") {
            actions.push(plainButton("\u{21A9}\u{FE0F}", "Reset to config.yaml value", (e) =>
              doAction("/widget/api/admin/settings/" + encodeURIComponent(it.key) + "/reset", {}, e.target).then(loadSettings)));
          }
        }

        list.appendChild(el("div", { class: "list-item" }, [
          el("div", { style: "flex:1;min-width:0;" }, [
            el("div", { style: "display:flex;align-items:center;gap:4px;flex-wrap:wrap;" }, badges.concat([
              el("span", { style: "font-weight:600;word-break:break-all;margin-left:4px;", text: it.label }),
            ])),
            el("div", { class: "muted", style: "font-size:11px;margin-top:2px;word-break:break-all;font-family:monospace;", text: it.key }),
            el("div", { class: "muted", style: "font-size:12px;margin-top:2px;", text: it.description }),
            el("div", { style: "margin-top:6px;max-width:320px;" }, [control]),
          ]),
          el("div", { class: "row" }, actions),
        ]));
      });
    }

    function loadSettings() {
      api("/widget/api/admin/settings").then((data) => {
        allItems = data.settings;
        banner.style.display = data.overrides_enabled ? "none" : "";
        banner.textContent = data.overrides_enabled ? "" :
          "Changing settings here is turned off for this bridge (bridge.allow_db_setting_overrides: false), so every setting below is config.yaml only.";
        const sections = Array.from(new Set(allItems.map((it) => it.section))).sort();
        const chosen = sectionSelect.value;
        sectionSelect.innerHTML = "";
        sectionSelect.appendChild(el("option", { value: "all", text: "All sections" }));
        sections.forEach((s) => sectionSelect.appendChild(el("option", Object.assign({ value: s, text: s }, s === chosen ? { selected: "selected" } : {}))));
        renderList();
      }).catch(() => { list.innerHTML = ""; list.appendChild(el("div", { class: "muted", text: "Couldn't load." })); });
    }

    filterInput.addEventListener("input", renderList);
    sectionSelect.addEventListener("change", renderList);

    app.appendChild(el("div", { class: "card" }, [
      el("h2", { text: "Settings" }),
      el("div", { class: "sub", text: "Every bridge setting, documented here whether or not it can be changed from this page. Changes apply straight away." }),
      banner,
      el("div", { class: "row" }, [filterInput, sectionSelect]),
      countLine,
      list,
    ]));

    loadSettings();
  }

  function primaryButton(icon, title, handler) {
    return el("button", { class: "primary", onclick: handler, title: title, "aria-label": title, text: title });
  }
  function plainButton(icon, title, handler) {
    return el("button", { onclick: handler, title: title, "aria-label": title, text: title });
  }
  function dangerButton(icon, title, handler) {
    return el("button", { class: "danger", onclick: handler, title: title, "aria-label": title, text: title });
  }
  function toggleButton(icon, title, on, handler) {
    return el("button", { class: on ? "toggle-on" : "", onclick: handler, title: title, "aria-label": title, text: title });
  }

  // Instead of confirm(), which Element's widget frame silently blocks: an
  // inline "message [Yes] [Cancel]" row after the button.
  function withConfirm(message, run) {
    return (e) => {
      const btn = e.target;
      if (btn.dataset.confirmPending === "1") return;
      btn.dataset.confirmPending = "1";
      btn.disabled = true;
      const row = el("div", { class: "confirm-row" }, [
        el("span", { class: "muted", text: message }),
        dangerButton("\u{2713}", "Yes", () => {
          row.remove();
          Promise.resolve(run(btn)).finally(() => { delete btn.dataset.confirmPending; });
        }),
        plainButton("\u{2715}", "Cancel", () => {
          row.remove();
          btn.disabled = false;
          delete btn.dataset.confirmPending;
        }),
      ]);
      btn.insertAdjacentElement("afterend", row);
    };
  }

  async function loadContext() {
    ctx = await api("/widget/api/context");
    render();
  }

  // Bumped on every start() and on Cancel, so a late answer to an
  // abandoned attempt never replaces a newer one.
  let startGeneration = 0;

  function showConnectErrorState(message) {
    const app = document.getElementById("app");
    app.innerHTML = "";
    app.appendChild(el("div", { class: "card" }, [
      el("h2", { text: "Couldn't connect" }),
      el("div", { class: "sub", text: message }),
      el("div", { class: "row" }, [primaryButton("\u{1F504}", "Retry", () => start())]),
    ]));
  }

  function showConnectingState() {
    const app = document.getElementById("app");
    app.innerHTML = "";
    const cancelBtn = plainButton("\u{2715}", "Cancel", () => {
      startGeneration++;
      showConnectErrorState("Cancelled.");
    });
    cancelBtn.style.display = "none";
    app.appendChild(el("div", { class: "spinner" }, [
      el("div", { text: "Connecting…" }),
      el("div", { style: "margin-top:12px" }, [cancelBtn]),
    ]));
    setTimeout(() => { cancelBtn.style.display = ""; }, 3000);
  }

  async function start() {
    const myGeneration = ++startGeneration;
    if (window.parent === window) {
      showConnectErrorState("Open this from its room in your Matrix client: it signs in through the client, so it can't work in a window of its own.");
      return;
    }
    showConnectingState();
    try {
      const creds = await getOpenIdToken();
      if (myGeneration !== startGeneration) return;
      const session = await api("/widget/api/session", {
        method: "POST",
        body: { access_token: creds.access_token, matrix_server_name: creds.matrix_server_name },
      });
      if (myGeneration !== startGeneration) return;
      sessionId = session.session_id;
      await loadContext();
    } catch (e) {
      if (myGeneration !== startGeneration) return;
      showConnectErrorState(e.message);
    }
  }

  // Checking in, so the session lasts while the widget is open.
  setInterval(() => { if (sessionId) api("/widget/api/ping").catch(() => {}); }, 5 * 60 * 1000);

  (function initScrollTopButton() {
    const btn = document.getElementById("scroll-top-btn");
    if (!btn) return;
    btn.addEventListener("click", () => window.scrollTo({ top: 0, behavior: "smooth" }));
    window.addEventListener("scroll", () => { btn.classList.toggle("visible", window.scrollY > 300); });
  })();

  (function initMobileMenuButton() {
    const btn = document.getElementById("mobile-menu-btn");
    if (!btn) return;
    btn.addEventListener("click", () => { mobileMenuOpen = true; render(); });
  })();

  start();
})();
</script>
</body>
</html>
"""
