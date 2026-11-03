// 执行真实页面脚本，地图SDK及DOM用最小既有Node环境模拟，不访问外网。
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const {config, payload} = JSON.parse(fs.readFileSync(0,'utf8'));
class Element {
  constructor(tag='div') { this.tag=tag;this.style={};this.children=[];this.dataset={};this.listeners={};this.checked=false;this.hidden=false;this.disabled=false;this.attributes={};this.classList={toggle(){}}; }
  set textContent(value) { this._text=String(value);this.children=[]; }
  get textContent() { return (this._text || '')+this.children.map(x=>typeof x==='string'?x:x.textContent).join(''); }
  append(...items) { this.children.push(...items); }
  appendChild(item) {this.append(item);}
  replaceChildren(...items) {this.children=items;this._text='';}
  insertRow() {const row=new Element('tr');this.append(row);return row;}
  insertCell() {const cell=new Element('td');this.append(cell);return cell;}
  addEventListener(event,fn) {this.listeners[event]=fn;}
  setAttribute(key,value) {this.attributes[key]=value;}
  reportValidity() {return true;}
  querySelector(tag) {for(const c of this.children) {if(typeof c==='object') {if(c.tag===tag)return c;const found=c.querySelector(tag);if(found)return found;}} return null;}
  querySelectorAll() {return this.children.filter(c=>c.tag==='tr' && c.dataset.id);}
}
const nodes=new Map();
const el=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
el('zone-config').textContent=JSON.stringify({...config,key:''});
const maps=[];
class Data {
  constructor(){this.features=[];maps.push(this);}
  addGeoJson(collection){for(const f of collection.type==='FeatureCollection'?collection.features:[collection]) this.features.push({getProperty:k=>f.properties[k],raw:f});}
  forEach(fn){[...this.features].forEach(fn);}
  remove(feature){this.features.splice(this.features.indexOf(feature),1);}
  setStyle(style){this.style=style;}
  addListener(name,fn){this[name]=fn;}
}
let fit=0, requested='', fail=false;
const context={
  document:{getElementById:el,createElement:tag=>new Element(tag),head:new Element()},window:{},
  google:{maps:{Data,Map:class {fitBounds(){fit++;}},LatLngBounds:class{extend(){}isEmpty(){return false;}},InfoWindow:class{},SymbolPath:{CIRCLE:'circle'}}},
  URL,URLSearchParams,AbortController,
  FormData:class extends Map {constructor(){super([['ward','tokyo23'],['target_stores','8000'],['target_area','25'],['area_weight','0.25'],['load_min','0.75'],['load_max','1.35']]);}},
  fetch:async url=>{requested=url;if(fail)throw new Error('synthetic source unavailable');return {ok:true,redirected:false,headers:{get:()=> 'application/json'},json:async()=>payload};}
};
vm.runInNewContext(fs.readFileSync('app/static/bd_zones.js','utf8'),context);
(async()=>{
  assert.match(el('zone-data-status').textContent,/public\.kanto_merchants_v20261029/);
  context.window.initBdZoneMap();
  await el('zone-form').listeners.submit({preventDefault(){}});
  assert.match(requested,/target_area=25/);assert.match(requested,/area_weight=0.25/);
  assert.equal(el('zone-rows').children.length,payload.features.length);
  assert.equal(el('zone-rows').children[0].children.length,5);
  assert.match(el('zone-source').textContent,/PostgreSQL/);
  assert.equal(el('zone-generate').disabled,false);
  assert.equal(el('zone-audit-status').textContent,config.texts.auditBlocked);
  const button=el('zone-rows').children[0].querySelector('button');
  button.listeners.click();assert.equal(button.attributes['aria-pressed'],'true');assert.ok(fit>=2);
  assert.equal(maps[0].features.length,payload.features.length);
  el('zone-density-layer').checked=true;el('zone-density-layer').listeners.change();
  assert.equal(maps[1].style(maps[1].features[0]).visible,true);
  assert.equal(maps[1].style(maps[1].features[0]).strokeWeight,0);
  assert.equal(maps[1].style(maps[1].features[0]).fillOpacity,.65);
  const markerLayer=maps[6];
  assert.equal(markerLayer.features.length,payload.review_merchants.filter(r=>Number.isFinite(r.lng)&&Number.isFinite(r.lat)).length);
  fail=true;await el('zone-form').listeners.submit({preventDefault(){}});
  assert.equal(el('zone-generate').disabled,false);assert.match(el('zone-notice').textContent,/synthetic source unavailable/);
  console.log(JSON.stringify({rows:payload.features.length,cells_per_row:5,source_visible:true,selection:true,map_features:maps[0].features.length,error_recovery:true}));
})().catch(error=>{console.error(error);process.exitCode=1;});
