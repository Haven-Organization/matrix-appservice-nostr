/* The game player: the Games room widget's page, on the bridge's origin.
 *
 * 1. Signs in like the bridge widget: the client's Matrix OpenID token,
 *    traded for a bridge session (/widget/api/session).
 * 2. Opens /xdc/ws for the game: the bridge sends the game's details, who
 *    the player is, and every move so far, then each new move (and realtime
 *    packet) as it happens; moves and packets the app sends go back the
 *    same way, and the bridge signs and publishes them.
 * 3. Downloads the .xdc from where it was posted (through the bridge if that
 *    host doesn't allow it), checks its sha256, and runs it in a frame on
 *    the sandbox origin, answering the frame's requests for files from the
 *    archive. The app never gets network access.
 */
(function(){
  'use strict';
  var cfg = window.__PLAYER;
  var params = new URLSearchParams(location.search);
  var widgetId = params.get('widgetId') || '';
  var gameId = params.get('game') || '';
  var statusEl = document.getElementById('status');
  function say(t){ statusEl.textContent = t; statusEl.style.display = t ? 'flex' : 'none'; }
  function fail(t){ say(t); statusEl.classList.add('err'); }

  /* -- the Matrix widget API: just enough for OpenID -- */
  var counter = 0, waiting = {}, openIdResolve = null;
  window.addEventListener('message', function(ev){
    var m = ev.data;
    if (!m || typeof m !== 'object' || ev.source !== window.parent) return;
    if (m.widgetId && widgetId && m.widgetId !== widgetId) return;
    if (m.api === 'fromWidget' && m.requestId && waiting[m.requestId] && 'response' in m){
      var r = waiting[m.requestId]; delete waiting[m.requestId]; r(m.response); return;
    }
    if (m.api === 'toWidget' && m.action === 'capabilities'){
      // Signing in needs the client around it, so no pop-out button.
      window.parent.postMessage(Object.assign({}, m, { response: { capabilities: ['io.element.requires_client'] } }), '*'); return;
    }
    if (m.api === 'toWidget' && m.action === 'openid_credentials'){
      window.parent.postMessage(Object.assign({}, m, { response: {} }), '*');
      if (openIdResolve){ var o = openIdResolve; openIdResolve = null; o(m.data); } return;
    }
    if (m.api === 'toWidget' && m.requestId) window.parent.postMessage(Object.assign({}, m, { response: {} }), '*');
  });
  function fromWidget(action, data){
    return new Promise(function(resolve){
      var requestId = 'req-' + (counter++) + '-' + Date.now();
      waiting[requestId] = resolve;
      window.parent.postMessage({ api: 'fromWidget', widgetId: widgetId, requestId: requestId, action: action, data: data || {} }, '*');
    });
  }
  function openId(){
    return fromWidget('get_openid', {}).then(function(first){
      if (first && first.state === 'allowed') return first;
      if (first && first.state === 'blocked') throw new Error('Your client refused to tell the game who you are.');
      return new Promise(function(resolve, reject){
        openIdResolve = resolve;
        setTimeout(function(){ if (openIdResolve){ openIdResolve = null; reject(new Error('Timed out waiting for approval.')); } }, 60000);
      });
    });
  }

  /* -- the game's moves, in serial order -- */
  var moves = [], seen = {}, delivered = 0, listening = false;
  function absorb(ev){
    if (!ev || seen[ev.id]) return false;
    seen[ev.id] = true;
    moves.push(ev);
    return true;
  }
  function tag(ev, name){ var t = (ev.tags || []).find(function(x){ return x[0] === name; }); return t ? t[1] : undefined; }
  function deliver(){
    if (!listening) return;
    var max = moves.length;
    for (var i = delivered; i < max; i++){
      var ev = moves[i], payload = null;
      try{ payload = JSON.parse(ev.content || 'null'); }catch(_){ payload = null; }
      toApp({ jsonrpc: '2.0', method: 'webxdc.update', params: {
        payload: payload, serial: i + 1, max_serial: max,
        info: tag(ev, 'info'), document: tag(ev, 'document'), summary: tag(ev, 'summary') } });
    }
    delivered = max;
  }

  /* -- the bridge -- */
  var ws = null, nextAck = 1, acks = {}, self = { addr: '', name: '' }, game = null;
  function connect(session){
    return new Promise(function(resolve, reject){
      var url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host
        + '/xdc/ws?game=' + encodeURIComponent(gameId) + '&session=' + encodeURIComponent(session);
      ws = new WebSocket(url);
      ws.onmessage = function(e){
        var m; try{ m = JSON.parse(e.data); }catch(_){ return; }
        if (m.t === 'init'){
          game = m.game; self = m.self || self;
          (m.updates || []).forEach(absorb);
          resolve();
        } else if (m.t === 'update'){ if (absorb(m.event)) deliver(); }
        else if (m.t === 'realtime'){ toApp({ jsonrpc: '2.0', method: 'webxdc.realtime', params: { b64: m.b64 } }); }
        else if (m.t === 'ack' && acks[m.id]){ var a = acks[m.id]; delete acks[m.id]; m.ok ? a.resolve() : a.reject(new Error(m.error || 'failed')); }
        else if (m.t === 'error'){ fail(m.error || 'Something went wrong.'); reject(new Error(m.error)); }
      };
      ws.onclose = function(){ if (!game) reject(new Error('Could not reach the bridge.')); else say('Disconnected from the bridge. Reload to keep playing.'); };
    });
  }
  function bridgeSend(msg, wantAck){
    if (!ws || ws.readyState !== 1) return Promise.reject(new Error('not connected'));
    if (!wantAck){ ws.send(JSON.stringify(msg)); return Promise.resolve(); }
    var id = nextAck++;
    msg.id = id;
    return new Promise(function(resolve, reject){ acks[id] = { resolve: resolve, reject: reject }; ws.send(JSON.stringify(msg)); });
  }

  /* -- the app's files -- */
  var archive = null, index = new Map(), cache = new Map(), shimSrc = '';
  var MIME = { html: 'text/html; charset=utf-8', htm: 'text/html; charset=utf-8', js: 'text/javascript', mjs: 'text/javascript',
    css: 'text/css', json: 'application/json', png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif',
    svg: 'image/svg+xml', webp: 'image/webp', ico: 'image/x-icon', wasm: 'application/wasm', mp3: 'audio/mpeg',
    ogg: 'audio/ogg', wav: 'audio/wav', mp4: 'video/mp4', webm: 'video/webm', woff: 'font/woff', woff2: 'font/woff2',
    ttf: 'font/ttf', txt: 'text/plain', xml: 'application/xml', data: 'application/octet-stream' };
  var CSP = "default-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' data: blob:; connect-src 'self' data: blob:; base-uri 'self'; form-action 'none'";
  function mimeOf(name){ var ext = (name.split('.').pop() || '').toLowerCase(); return MIME[ext] || 'application/octet-stream'; }

  async function download(){
    var bytes = null;
    say('Downloading ' + (game.name || 'the game') + '…');
    try{
      var res = await fetch(game.url, { mode: 'cors', credentials: 'omit' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      bytes = new Uint8Array(await res.arrayBuffer());
    }catch(_){
      var via = await fetch('/xdc/app?game=' + encodeURIComponent(gameId), { headers: { Authorization: 'Bearer ' + cfg.session } });
      if (!via.ok) throw new Error('Could not download the game (' + via.status + ').');
      bytes = new Uint8Array(await via.arrayBuffer());
    }
    if (game.x){
      var digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
      var hex = Array.from(digest, function(b){ return b.toString(16).padStart(2, '0'); }).join('');
      if (hex !== game.x) throw new Error('The game file doesn’t match what was posted (its hash differs), so it won’t be run.');
    }
    archive = bytes;
    window.PCZip.entries(bytes).forEach(function(e){ if (e.name) index.set(window.PCZip.normalise(e.name), e); });
    if (!index.has('index.html')) throw new Error('That isn’t a webxdc app (it has no index.html).');
  }
  async function fileOf(name){
    if (cache.has(name)) return cache.get(name);
    var entry = index.get(name);
    if (!entry) return null;
    var bytes = await window.PCZip.read(archive, entry);
    cache.set(name, bytes);
    return bytes;
  }
  function injectShim(html){
    var tag = '<script src="/__webxdc__.js"></script>';
    if (/<head[^>]*>/i.test(html)) return html.replace(/<head[^>]*>/i, function(m){ return m + tag; });
    if (/<html[^>]*>/i.test(html)) return html.replace(/<html[^>]*>/i, function(m){ return m + tag; });
    return tag + html;
  }
  async function serve(path){
    var enc = new TextEncoder();
    if (path === '/__webxdc__.js' || path === '/webxdc.js'){
      var head = 'window.__XDC = ' + JSON.stringify({ addr: self.addr, name: self.name }) + ';\n';
      return { status: 200, type: 'text/javascript', body: enc.encode(head + shimSrc) };
    }
    var name = decodeURIComponent(path || '/').replace(/^\/+/, '');
    if (!name || name.charAt(name.length - 1) === '/') name += 'index.html';
    name = window.PCZip.normalise(name);
    if (!index.has(name) && index.has(name + '/index.html')) name += '/index.html';
    var bytes = await fileOf(name);
    if (!bytes) return { status: 404, type: 'text/plain', body: enc.encode('not in this app') };
    var type = mimeOf(name);
    if (type.indexOf('text/html') === 0) bytes = enc.encode(injectShim(new TextDecoder().decode(bytes)));
    return { status: 200, type: type, body: bytes };
  }

  /* -- the sandbox frame -- */
  var frame = null;
  function toApp(m){ if (frame && frame.contentWindow) try{ frame.contentWindow.postMessage(m, cfg.sandboxOrigin); }catch(_){} }
  function reply(id, result, transfer){ if (frame) frame.contentWindow.postMessage({ jsonrpc: '2.0', id: id, result: result }, cfg.sandboxOrigin, transfer || []); }
  function replyError(id, message){ toApp({ jsonrpc: '2.0', id: id, error: { message: message } }); }
  window.addEventListener('message', function(ev){
    if (!frame || ev.source !== frame.contentWindow || ev.origin !== cfg.sandboxOrigin) return;
    var d = ev.data;
    if (!d || typeof d !== 'object' || d.jsonrpc !== '2.0') return;
    var id = d.id, p = d.params || {};
    if (d.method === 'fetch'){
      var path = '/';
      try{ path = new URL(p.request.url).pathname; }catch(_){}
      serve(path).then(function(r){
        var buf = r.body.byteOffset === 0 && r.body.byteLength === r.body.buffer.byteLength ? r.body.buffer : r.body.slice().buffer;
        reply(id, { status: r.status, statusText: '', headers: { 'content-type': r.type, 'content-security-policy': CSP,
          'cache-control': 'no-store', 'x-content-type-options': 'nosniff' }, bytes: buf }, [buf]);
      }, function(){ replyError(id, 'could not read that file from the app'); });
      return;
    }
    if (d.method === 'webxdc.sendUpdate'){
      bridgeSend({ t: 'send', update: p.update, descr: p.descr || '' }, true)
        .then(function(){ reply(id, null); }, function(e){ replyError(id, e.message); });
      return;
    }
    if (d.method === 'webxdc.setUpdateListener'){
      listening = true;
      delivered = Math.max(0, Math.min(moves.length, parseInt(p.serial || 0, 10) || 0));
      deliver();
      reply(id, null);
      return;
    }
    if (d.method === 'webxdc.rtJoin'){ bridgeSend({ t: 'rt_join' }, true).then(function(){ reply(id, null); }, function(e){ replyError(id, e.message); }); return; }
    if (d.method === 'webxdc.rtSend'){ bridgeSend({ t: 'rt_send', b64: p.b64 || '' }).catch(function(){}); return; }
    if (d.method === 'webxdc.rtLeave'){ bridgeSend({ t: 'rt_leave' }).catch(function(){}); if (id !== undefined) reply(id, null); return; }
    if (d.method === 'webxdc.crash' || d.method === 'sandbox.error'){ console.warn('[game]', p.message || p); return; }
    if (d.method === 'sandbox.running'){ say(''); return; }
    if (id !== undefined && d.method) replyError(id, 'unsupported: ' + d.method);
  });
  function startFrame(){
    var token = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2));
    frame = document.createElement('iframe');
    frame.id = 'game';
    frame.setAttribute('allow', 'autoplay; fullscreen; gamepad; pointer-lock');
    frame.src = cfg.sandboxOrigin + '/__sandbox__/?__xdc=' + encodeURIComponent(token);
    frame.addEventListener('load', function(){
      frame.contentWindow.postMessage({ jsonrpc: '2.0', method: 'init', params: { version: 1, entry: 'index.html' } }, cfg.sandboxOrigin);
    });
    document.body.appendChild(frame);
  }

  /* -- go -- */
  fromWidget('content_loaded', {});
  (async function(){
    try{
      if (!gameId) throw new Error('No game was given.');
      if (window.parent === window)
        throw new Error('Open this game from its room in your Matrix client: it signs in through the client, so it can\'t run in a window of its own.');
      say('Signing in…');
      var creds = await openId();
      var res = await fetch('/widget/api/session', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ access_token: creds.access_token, matrix_server_name: creds.matrix_server_name }) });
      var data = await res.json().catch(function(){ return {}; });
      if (!res.ok) throw new Error(data.detail || 'Could not sign in.');
      cfg.session = data.session_id;
      shimSrc = await (await fetch('/xdc/static/shim.js')).text();
      say('Connecting…');
      await connect(cfg.session);
      await download();
      say('Starting ' + (game.name || 'the game') + '…');
      startFrame();
    }catch(e){ fail(e.message || String(e)); }
  })();
})();
