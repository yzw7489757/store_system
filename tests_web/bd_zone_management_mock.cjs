const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
class Element {
  constructor() { this.handlers = {}; this.children = []; this.hidden = false; this.value = ''; this.textContent = ''; this.attributes = {}; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  focus() { this.focused = true; }
}
const ids = {};
for (const id of ['zone-management-config','zone-drawer','zone-panel','zone-select','zone-map-status','zone-close','zone-reset','zone-map','zone-detail-title','zone-grid-data']) ids[id] = new Element();
const texts = Object.fromEntries(['unassigned','area','stores','team','leader','ready','review','progress','close','loading','detailError','retry','login','keyMissing','mapError'].map(k => [k,k]));
ids['zone-management-config'].textContent = JSON.stringify({key:'synthetic',language:'zh',selected:null,texts,geometry:{type:'FeatureCollection',features:[{geometry:{type:'Polygon',coordinates:[[[139,35],[140,35],[139,36],[139,35]]]},properties:{id:12,name:'<img src=x>',area:25,stores:3,team:'team-a',leader:'captain-a',review:false}}]}});
const layers = [], popups = [], maps = [];
class Data {
  constructor() { this.handlers = {}; layers.push(this); }
  addGeoJson(collection) { this.collection=collection; this.features = collection.features.map(f => ({getProperty(k) { return f.properties[k]; }, getGeometry() { return {forEachLatLng(cb) { f.geometry.coordinates[0].forEach(([lng,lat]) => cb({lng,lat})); }}; }})); return this.features; }
  setStyle(value) { this.style = value; }
  setMap(map) { this.map = map; }
  forEach(cb) { this.features.forEach(cb); }
  addListener(name, handler) { this.handlers[name] = handler; }
  overrideStyle(feature, style) { this.override = style; }
  revertStyle() { this.override = null; }
}
let nextResponse, fetches = [], documentHandlers = {}, frames = new Map(), frameId = 0;
const motion = {matches:false, addEventListener(name,handler){this.handler=handler;}};
const context = {URL,URLSearchParams,AbortController,Map,console,setTimeout,clearTimeout,
  window:{requestAnimationFrame(cb){frames.set(++frameId,cb);return frameId;},cancelAnimationFrame(id){frames.delete(id);},matchMedia(){return motion;},location:{href:'http://localhost/zones?lang=ja',search:'?lang=ja'},innerWidth:1200,history:{replaceState(a,b,url) { context.window.location.href=String(url); }}},
  document:{getElementById:id=>ids[id],addEventListener(name,handler) { documentHandlers[name]=handler; },createElement:()=>new Element(),head:{append(script) { context.sdk=script; }}},
  fetch(url,options) { fetches.push({url,options}); return nextResponse; },
  google:{maps:{Data,Map:class { constructor(element,options) { this.options=options; maps.push(this); } fitBounds() {} },
    LatLngBounds:class { constructor() { this.points=[]; } extend(point) { this.points.push(point); } isEmpty() { return this.points.length===0; } },
    InfoWindow:class { constructor() { popups.push(this); } setContent(value) { this.content=value; } setPosition() {} open() { this.opened=true; } close() { this.opened=false; } }}}
};

const outer=[[139,35],[140,35],[140,36],[139,35]];
const hole=[[139.2,35.2],[139.3,35.2],[139.3,35.3],[139.2,35.2]];
const original=JSON.parse(ids['zone-management-config'].textContent);
original.geometry.features[0].geometry.coordinates.push(hole);
original.geometry.features.push({type:'Feature',geometry:{type:'MultiPolygon',coordinates:[[outer,hole],[[[141,35],[142,35],[142,36],[141,35]],hole]]},properties:{id:13}});
ids['zone-management-config'].textContent=JSON.stringify(original);
vm.runInNewContext(fs.readFileSync('app/static/bd_zone_manage.js','utf8'),context);
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  assert.match(context.sdk.src,/maps.googleapis.com/);
  context.window.initZoneManagementMap();
  const layer=layers[0], feature=layer.features[0];
  assert.equal(layer.collection.features[0].geometry.coordinates.length,1);
  assert.equal(layer.collection.features[1].geometry.coordinates.length,2);
  assert.ok(layer.collection.features[1].geometry.coordinates.every(polygon=>polygon.length===1));
  assert.deepEqual(JSON.parse(JSON.stringify(layer.collection.features[0].geometry.coordinates[0])),original.geometry.features[0].geometry.coordinates[0]);
  assert.equal(original.geometry.features[0].geometry.coordinates.length,2);

  layer.handlers.mouseover({feature,latLng:{}});
  assert.equal(layer.override.fillOpacity,0);
  assert.equal(popups[0].content.children[0].children[0].textContent,'<img src=x>');
  assert.equal(popups[0].content.children[0].children[1].attributes['aria-label'],'close');
  assert.ok(popups[0].content.children.some(child=>child.textContent.includes('progress')));
  popups[0].content.children[0].children[1].handlers.click();
  assert.equal(popups[0].opened,false);
  assert.ok(popups[0].content.children.some(child=>child.textContent.includes('captain-a')));
  layer.handlers.mouseout({feature});
  popups[0].content.handlers.mouseenter();
  assert.equal(popups[0].opened,false);
  nextResponse=Promise.resolve({ok:true,redirected:false,text:async()=>'<section>panel-12</section>'});
  layer.handlers.click({feature}); await flush();
  assert.equal(ids['zone-drawer'].hidden,false); assert.equal(ids['zone-select'].value,'12');
  assert.equal(ids['zone-panel'].innerHTML,'<section>panel-12</section>');
  assert.ok(fetches[0].url.startsWith('/zones/12/panel'));
  assert.ok(!fetches.some(call=>call.url.includes('/preview')));
  assert.ok(ids['zone-detail-title'].focused);
  assert.equal(layer.style(layer.features[0]).strokeColor,layer.style(layer.features[1]).strokeColor);
  assert.equal(layer.style(layer.features[0]).fillOpacity,.13);
  assert.equal(layer.style(layer.features[1]).fillOpacity,0);
  assert.equal(maps[0].options.gestureHandling,'greedy');
  assert.equal(frames.size,1);
  const tick=frames.entries().next().value; frames.delete(tick[0]); tick[1](1000);
  assert.ok(layer.override.strokeWeight>=4);
  assert.equal(layer.override.strokeColor,layer.style(feature).strokeColor);
  motion.matches=true;motion.handler();assert.equal(frames.size,0);
  motion.matches=false;motion.handler();assert.equal(frames.size,1);
  ids['zone-grid-data'].textContent=JSON.stringify({type:'FeatureCollection',features:[{geometry:{type:'Polygon',coordinates:[[[139,35],[140,35],[139,36],[139,35]]]},properties:{id:99,review:false}}]});
  const gridData=JSON.parse(ids['zone-grid-data'].textContent);
  gridData.features[0].geometry.coordinates.push(hole);
  ids['zone-grid-data'].textContent=JSON.stringify(gridData);
  const toggle={id:'zone-grid-toggle',checked:true};
  ids['zone-panel'].handlers.change({target:toggle});
  const gridLayer=layers.at(-1);
  assert.equal(gridLayer.features[0].getProperty('id'),99);
  assert.equal(gridLayer.collection.features[0].geometry.coordinates.length,1);
  assert.equal(JSON.parse(ids['zone-grid-data'].textContent).features[0].geometry.coordinates.length,2);
  assert.equal(gridLayer.style(gridLayer.features[0]).clickable,false);
  assert.equal(gridLayer.style(gridLayer.features[0]).strokeColor,layer.style(feature).strokeColor);
  toggle.checked=false; ids['zone-panel'].handlers.change({target:toggle});
  assert.equal(gridLayer.map,null);
  toggle.checked=true; ids['zone-panel'].handlers.change({target:toggle});
  const closeLayer=layers.at(-1);
  documentHandlers.keydown({key:'Escape'}); assert.equal(ids['zone-drawer'].hidden,true);
  assert.equal(closeLayer.map,null);
  assert.equal(frames.size,0);
  assert.ok(ids['zone-select'].focused);
  nextResponse=Promise.reject(new Error('offline')); ids['zone-select'].value='12'; ids['zone-select'].handlers.change(); await flush();
  assert.equal(ids['zone-panel'].children[0].textContent,'offline');
  nextResponse=Promise.resolve({ok:true,redirected:false,text:async()=>'recovered'});
  ids['zone-panel'].children[1].handlers.click(); await flush(); assert.equal(ids['zone-panel'].innerHTML,'recovered');
  toggle.checked=true; ids['zone-panel'].handlers.change({target:toggle});
  const switchLayer=layers.at(-1);
  let resolveOld;
  nextResponse=new Promise(resolve=>{resolveOld=resolve;}); layer.handlers.click({feature});
  assert.equal(switchLayer.map,null);
  nextResponse=Promise.resolve({ok:true,redirected:false,text:async()=>'latest'}); layer.handlers.click({feature}); await flush();
  resolveOld({ok:true,redirected:false,text:async()=>'obsolete'}); await flush();
  assert.equal(ids['zone-panel'].innerHTML,'latest');
  nextResponse=Promise.resolve({ok:true,redirected:true,url:'http://localhost/login'}); layer.handlers.click({feature}); await flush();
  assert.equal(ids['zone-panel'].children[0].textContent,'login');
  context.window.gm_authFailure(); assert.equal(ids['zone-map-status'].textContent,'mapError');
  ids['zone-reset'].handlers.click(); assert.equal(ids['zone-drawer'].hidden,true);
  console.log(JSON.stringify({drawer_and_recovery:true,hover:true,stale_requests:true}));
})().catch(error=>{console.error(error);process.exitCode=1;});
