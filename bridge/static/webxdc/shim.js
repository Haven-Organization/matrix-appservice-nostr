/* window.webxdc for a mini app, in its sandbox frame.
 *
 * Served to the app as /__webxdc__.js (and as webxdc.js, which apps include
 * themselves), with `__XDC = {addr, name}` set in front of it by the player.
 * It speaks JSON-RPC to the player page, through the sandbox loader (this
 * frame's parent, same origin), which forwards both ways. Nothing in here
 * reaches the network: the app has no network at all.
 *
 * The API is webxdc's (https://webxdc.org/docs/spec/): sendUpdate,
 * setUpdateListener (with the serial to resume after), joinRealtimeChannel,
 * selfAddr and selfName. sendToChat and importFiles aren't offered.
 */
(function(){
  'use strict';
  if (window.webxdc) return;
  var meta = window.__XDC || {};
  var nextId = 1, pending = {}, listener = null, rtListener = null;

  function send(m){ try{ window.parent.postMessage(m, location.origin); }catch(_){} }
  function rpc(method, params){
    var id = 'app:' + (nextId++);
    return new Promise(function(resolve, reject){
      pending[id] = { resolve: resolve, reject: reject };
      send({ jsonrpc: '2.0', id: id, method: method, params: params || {} });
    });
  }
  window.addEventListener('message', function(ev){
    if (ev.source !== window.parent) return;
    var d = ev.data;
    if (!d || typeof d !== 'object' || d.jsonrpc !== '2.0') return;
    if (d.id !== undefined && d.method === undefined){
      var p = pending[d.id]; if (!p) return;
      delete pending[d.id];
      if (d.error) p.reject(new Error(d.error.message || 'error')); else p.resolve(d.result);
      return;
    }
    if (d.method === 'webxdc.update' && listener){ try{ listener(d.params); }catch(e){ console.error(e); } }
    if (d.method === 'webxdc.realtime' && rtListener){ try{ rtListener(unb64(d.params && d.params.b64)); }catch(e){ console.error(e); } }
  });

  function b64(bytes){
    var s = '';
    for (var i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(s);
  }
  function unb64(s){
    var bin = atob(s || ''), out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  /* -- the game's own localStorage, kept in the player's Games room --
   * Every game runs on the same sandbox origin, so the browser would give
   * them one localStorage between them. This one is the game's alone, filled
   * from its save when it starts and sent back a moment after it changes (and
   * when the game is left). A copy stays in the browser, under the game's
   * prefix, in case the bridge can't be reached. Flash games under Ruffle keep
   * their saves here. */
  (function(){
    var game = meta.game;
    var real = null;
    try{ real = window.localStorage; }catch(_){}
    if (!game || !real) return;
    var prefix = 'xdc:' + game + ':', data = {}, i, k;
    if (meta.save && typeof meta.save === 'object'){
      data = meta.save;
    } else {
      // Nothing kept yet: what this browser has for the game, under its prefix,
      // or (the first time) what games saved before they had one each.
      var found = false;
      for (i = 0; i < real.length; i++){ k = real.key(i); if (k && k.indexOf(prefix) === 0){ data[k.slice(prefix.length)] = real.getItem(k); found = true; } }
      if (!found) for (i = 0; i < real.length; i++){ k = real.key(i); if (k && k.indexOf('xdc:') !== 0) data[k] = real.getItem(k); }
    }
    var timer = null, dirty = !meta.save && Object.keys(data).length > 0;
    function local(key, value){
      try{ if (value === null) real.removeItem(prefix + key); else real.setItem(prefix + key, value); }catch(_){}
    }
    function flush(){
      clearTimeout(timer); timer = null;
      if (!dirty) return;
      dirty = false;
      send({ jsonrpc: '2.0', method: 'webxdc.save', params: { data: data } });
    }
    function changed(){ dirty = true; clearTimeout(timer); timer = setTimeout(flush, 3000); }
    var has = Object.prototype.hasOwnProperty;
    // The methods on a prototype, so the object itself has no fixed keys of
    // its own (a Proxy listing only the saved keys has to be allowed to).
    var methods = Object.create(Storage.prototype, {
      getItem: { value: function(key){ key = String(key); return has.call(data, key) ? data[key] : null; } },
      setItem: { value: function(key, value){ key = String(key); value = String(value);
        if (data[key] === value) return; data[key] = value; local(key, value); changed(); } },
      removeItem: { value: function(key){ key = String(key); if (!has.call(data, key)) return; delete data[key]; local(key, null); changed(); } },
      clear: { value: function(){ Object.keys(data).forEach(function(key){ local(key, null); }); data = {}; changed(); } },
      key: { value: function(n){ var keys = Object.keys(data); return n >= 0 && n < keys.length ? keys[n] : null; } },
      length: { get: function(){ return Object.keys(data).length; } },
    });
    var store = Object.create(methods);
    // localStorage.foo and localStorage['foo'] too, as with the real one.
    var view = new Proxy(store, {
      get: function(t, p){ return typeof p === 'symbol' || p in t ? t[p] : t.getItem(p); },
      set: function(t, p, v){ if (typeof p === 'symbol' || p in t) return false; t.setItem(p, v); return true; },
      deleteProperty: function(t, p){ t.removeItem(p); return true; },
      has: function(t, p){ return p in t || has.call(data, p); },
      ownKeys: function(){ return Object.keys(data); },
      getOwnPropertyDescriptor: function(t, p){
        return has.call(data, p) ? { value: data[p], writable: true, enumerable: true, configurable: true } : undefined;
      },
    });
    Object.defineProperty(window, 'localStorage', { configurable: true, get: function(){ return view; } });
    window.addEventListener('pagehide', flush);
    document.addEventListener('visibilitychange', function(){ if (document.visibilityState === 'hidden') flush(); });
    if (dirty) timer = setTimeout(flush, 3000);
  })();

  var listening = null, rtReady = null;
  window.webxdc = {
    selfAddr: meta.addr || '',
    selfName: meta.name || '',
    sendUpdateInterval: 1000,
    sendUpdateMaxSize: 128000,
    sendUpdate: function(update, descr){
      rpc('webxdc.sendUpdate', { update: update, descr: descr || '' }).catch(function(e){ console.error('sendUpdate:', e); });
    },
    setUpdateListener: function(cb, serial){
      listener = cb;
      if (!listening) listening = rpc('webxdc.setUpdateListener', { serial: serial || 0 });
      return listening;
    },
    getAllUpdates: function(){ return Promise.resolve([]); },
    sendToChat: function(){ return Promise.reject(new Error('sendToChat is not supported here')); },
    importFiles: function(){ return Promise.reject(new Error('importFiles is not supported here')); },
    joinRealtimeChannel: function(){
      if (rtReady) throw new Error('Already joined a realtime channel. Leave first.');
      rtReady = rpc('webxdc.rtJoin', {});
      var joined = true;
      return {
        setListener: function(cb){ if (!joined) throw new Error('Channel has been left.'); rtListener = cb; },
        send: function(data){
          if (!joined) throw new Error('Channel has been left.');
          if (!(data instanceof Uint8Array)) throw new Error('realtime data must be a Uint8Array');
          if (data.length > 128000) throw new Error('realtime packet too large');
          var wire = b64(data);
          rtReady.then(function(){ if (joined) send({ jsonrpc: '2.0', method: 'webxdc.rtSend', params: { b64: wire } }); });
        },
        leave: function(){
          if (!joined) return;
          joined = false; rtListener = null;
          var ready = rtReady; rtReady = null;
          ready.then(function(){ rpc('webxdc.rtLeave', {}); });
        },
      };
    },
  };
  window.addEventListener('error', function(e){
    send({ jsonrpc: '2.0', method: 'webxdc.crash', params: { message: String((e && e.message) || 'error') } });
  });
})();
