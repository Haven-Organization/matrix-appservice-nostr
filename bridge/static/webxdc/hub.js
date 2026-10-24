/* The Nostr Games widget (bridge.games_hub): find a game and open it in
 * this room. Signs in like the other widgets: the client's Matrix OpenID
 * token, traded for a bridge session.
 */
(function(){
  'use strict';
  var params = new URLSearchParams(location.search);
  var widgetId = params.get('widgetId') || '';
  var roomId = params.get('matrix_room_id') || '';
  var theme = params.get('theme') || '';
  if (theme.indexOf('dark') !== -1) document.documentElement.setAttribute('data-theme', 'dark');
  var list = document.getElementById('list');
  var filterInput = document.getElementById('filter');

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
      if (first && first.state === 'blocked') throw new Error('Your client refused to tell the widget who you are.');
      return new Promise(function(resolve, reject){
        openIdResolve = resolve;
        setTimeout(function(){ if (openIdResolve){ openIdResolve = null; reject(new Error('Timed out waiting for approval.')); } }, 60000);
      });
    });
  }

  /* -- the bridge -- */
  var session = null, signingIn = null;
  /* Signing in: the client's OpenID token, traded for a bridge session. The
   * session lasts while it's used (and this page checks in every few
   * minutes); if the bridge doesn't know it any more, this signs in again. */
  function signIn(){
    if (!signingIn) signingIn = openId().then(function(creds){
      return raw('/widget/api/session', { access_token: creds.access_token, matrix_server_name: creds.matrix_server_name });
    }).then(function(data){ session = data.session_id; signingIn = null; return session; },
            function(e){ signingIn = null; throw e; });
    return signingIn;
  }
  function raw(path, body){
    var headers = { 'Content-Type': 'application/json' };
    if (session) headers.Authorization = 'Bearer ' + session;
    return fetch(path, { method: body ? 'POST' : 'GET', headers: headers, body: body ? JSON.stringify(body) : undefined })
      .then(function(res){
        return res.json().catch(function(){ return {}; }).then(function(data){
          if (!res.ok){ var e = new Error(data.detail || ('Request failed (' + res.status + ')')); e.status = res.status; throw e; }
          return data;
        });
      });
  }
  function api(path, body){
    return raw(path, body).catch(function(e){
      if (e.status !== 401 || path === '/widget/api/session') throw e;
      return signIn().then(function(){ return raw(path, body); });
    });
  }
  setInterval(function(){ if (session) api('/widget/api/ping').catch(function(){}); }, 5 * 60 * 1000);
  function toast(text, ok){
    var n = document.getElementById('toast');
    n.textContent = text; n.className = 'toast show' + (ok ? '' : ' err');
    clearTimeout(toast.t); toast.t = setTimeout(function(){ n.className = 'toast'; }, 5000);
  }
  function el(tag, cls, text){ var n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }

  /* -- cartridge pictures, fetched with the session as they scroll into view -- */
  var pictures = {};
  var seenObserver = 'IntersectionObserver' in window ? new IntersectionObserver(function(entries){
    entries.forEach(function(e){ if (e.isIntersecting){ seenObserver.unobserve(e.target); load(e.target); } });
  }, { rootMargin: '200px' }) : null;
  function load(img){
    var id = img.dataset.game;
    if (!pictures[id]){
      pictures[id] = fetch('/xdc/api/cartridge?game=' + encodeURIComponent(id), { headers: { Authorization: 'Bearer ' + session } })
        .then(function(res){ if (!res.ok) throw new Error(); return res.blob(); })
        .then(function(blob){ return URL.createObjectURL(blob); });
    }
    pictures[id].then(function(url){ img.src = url; }).catch(function(){ img.alt = ''; });
  }

  /* -- the lists -- */
  var tab = 'nostr', games = { nostr: null, room: null }, canOpen = true;
  function show(){
    var items = games[tab];
    list.className = '';
    list.textContent = '';
    if (items === null){ list.className = 'note'; list.textContent = 'Looking…'; return; }
    var q = filterInput.value.trim().toLowerCase();
    var shown = items.filter(function(g){
      return !q || (g.name + ' ' + (g.description || '') + ' ' + ((g.author || {}).name || '')).toLowerCase().indexOf(q) !== -1;
    });
    if (!shown.length){
      list.className = 'note';
      list.textContent = q ? 'Nothing matches.' : (tab === 'room'
        ? 'No games in this room yet: game posts, cards, pinned games and .xdc files show up here.'
        : 'No games found on Nostr just now.');
      return;
    }
    var grid = el('div', 'grid');
    shown.forEach(function(g){
      var tile = el('div', 'tile');
      tile.title = canOpen ? 'Open ' + g.name + ' in this room' : g.name;
      var img = el('img', 'pic'); img.alt = g.name; img.dataset.game = g.id; img.loading = 'lazy';
      tile.appendChild(img);
      tile.appendChild(el('div', 'name', g.name));
      if (g.author) tile.appendChild(el('div', 'by', 'by ' + g.author.name));
      if (g.local || (g.sources || []).indexOf('pinned') !== -1){
        var badges = el('div', 'by');
        if (g.local) badges.appendChild(el('span', 'badge', 'this room only'));
        var labels = { pinned: '📌 pinned' };
        (g.sources || []).forEach(function(s){ if (labels[s]) badges.appendChild(el('span', 'badge', labels[s])); });
        tile.appendChild(badges);
      }
      if (g.description) tile.appendChild(el('div', 'desc', g.description));
      tile.addEventListener('click', function(){ open({ game: g.id }, tile); });
      grid.appendChild(tile);
      if (seenObserver) seenObserver.observe(img); else load(img);
    });
    list.appendChild(grid);
  }
  function fetchTab(which, refresh){
    games[which] = null; if (which === tab) show();
    var path = which === 'nostr' ? '/xdc/api/catalog' + (refresh ? '?refresh=true' : '')
                                 : '/xdc/api/room?room_id=' + encodeURIComponent(roomId);
    return api(path).then(function(data){
      games[which] = data.games || [];
      if (which === 'room') canOpen = data.can_open !== false;
      if (which === tab) show();
    }).catch(function(e){
      games[which] = [];
      if (which === tab){ list.className = 'note'; list.textContent = e.message; }
    });
  }
  function open(what, button){
    if (button) button.style.opacity = '.6';
    return api('/xdc/api/open', Object.assign({ room_id: roomId, in_hub: true }, what))
      .then(function(r){ games.room = null; play(r.game, r.name); })
      .catch(function(e){ toast(e.message, false); })
      .then(function(){ if (button) button.style.opacity = ''; });
  }

  /* -- playing, right here: the player in a frame, signed in with this session -- */
  var playing = document.getElementById('playing'), player = document.getElementById('player');
  function play(gameId, name){
    document.getElementById('title').textContent = name || '';
    player.src = '/xdc/play?game=' + encodeURIComponent(gameId) + '&theme=' + encodeURIComponent(theme) +
                 '&embedded=1#session=' + encodeURIComponent(session);
    playing.hidden = false;
  }
  var openedInto = params.get('game');  // the widget was pointed at a game ([play, a card's 🎮)
  document.getElementById('back').addEventListener('click', function(){
    playing.hidden = true;
    player.src = 'about:blank';
    if (openedInto){
      // Point the widget back at the list, or the client would reopen the game next time.
      openedInto = null;
      api('/xdc/api/close', { room_id: roomId }).catch(function(){});
    }
    if (games[tab] === null) fetchTab(tab); else show();
  });
  window.addEventListener('message', function(ev){
    if (ev.source !== player.contentWindow || !ev.data) return;
    // The player says what it's playing.
    if (typeof ev.data.hubTitle === 'string') document.getElementById('title').textContent = ev.data.hubTitle;
    // Or that its sign-in was refused: a fresh one.
    if (ev.data.hubSessionWanted) signIn().then(function(s){ player.contentWindow.postMessage({ hubSession: s }, location.origin); }, function(){});
  });

  document.querySelectorAll('nav button[data-tab]').forEach(function(b){
    b.addEventListener('click', function(){
      document.querySelectorAll('nav button[data-tab]').forEach(function(x){ x.classList.toggle('on', x === b); });
      tab = b.dataset.tab;
      if (games[tab] === null && session) fetchTab(tab); else show();
    });
  });
  filterInput.addEventListener('input', show);
  document.getElementById('refresh').addEventListener('click', function(){ if (session) fetchTab(tab, true); });
  document.getElementById('open').addEventListener('submit', function(ev){
    ev.preventDefault();
    var input = document.getElementById('link'), link = input.value.trim();
    if (!link || !session) return;
    var button = ev.target.querySelector('button'); button.disabled = true;
    open({ link: link }).then(function(){ button.disabled = false; });
  });

  /* -- go -- */
  fromWidget('content_loaded', {});
  (async function(){
    try{
      if (window.parent === window)
        throw new Error('Open this from its room in your Matrix client: it signs in through the client, so it can\'t work in a window of its own.');
      await signIn();
      if (params.get('game')) play(params.get('game'), '');  // opened into a game ([play, or a card's 🎮)
      fetchTab('nostr');
    }catch(e){ list.className = 'note'; list.textContent = e.message; }
  })();
})();
