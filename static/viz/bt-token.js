/* bt 鉴权垫片：EventSource / 同步 XHR 均不能带 header。
 * 对 /api/console/ 请求自动附 ?token=<localStorage.bt_agent_token>（auth.py:316-328 支持）。
 * 运行时读取，绝不硬编码 token；不写日志、不回传第三方。必须先于 boot.js 执行。*/
(function(){
  "use strict";
  function tok(){try{return (typeof localStorage!=='undefined')&&localStorage.getItem('bt_agent_token')||''}catch(e){return ''}}
  function tag(u){
    if(!u||String(u).indexOf('/api/console/')<0) return u;
    var t=tok(); if(!t) return u;
    return u+(String(u).indexOf('?')>=0?'&':'?')+'token='+encodeURIComponent(t);
  }
  if(typeof XMLHttpRequest!=='undefined'&&XMLHttpRequest.prototype.open){
    var _o=XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open=function(m,u){return _o.apply(this,[m,tag(u)].concat([].slice.call(arguments,2)))};
  }
  if(typeof window!=='undefined'&&window.fetch){
    var _f=window.fetch;
    window.fetch=function(u,c){ return _f(tag(typeof u==='string'?u:(u&&u.url)||u), c); };
  }
  if(typeof window!=='undefined'&&window.EventSource){
    var _ES=window.EventSource;
    function ES(u,c){return new _ES(tag(u),c)}
    ES.prototype=_ES.prototype; ES.CONNECTING=_ES.CONNECTING; ES.OPEN=_ES.OPEN; ES.CLOSED=_ES.CLOSED;
    window.EventSource=ES;
  }
})();
