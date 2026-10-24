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
