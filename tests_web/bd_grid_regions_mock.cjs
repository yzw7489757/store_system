const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
class Element {
  constructor() { this.handlers = {}; this.disabled = false; this.checked = false; this.textContent = ''; }
  addEventListener(name, fn) { this.handlers[name] = fn; }
  matches() { return !!this.dataset; }
}
const ids = Object.fromEntries(['grid-region-editor','grid-region-form','grid-region-config',
  'grid-region-status','grid-region-save','grid-region-merge','grid-region-count','grid-region-area','grid-region-stores','grid-region-clear'].map(id => [id,new Element()]));
const boxes = [1,2].map(id => Object.assign(new Element(), {dataset:{gridId:String(id)},
  parentElement:{textContent:`grid${id}`,hidden:false},
  value:JSON.stringify({id,revision:0,zone_id:null,zone_revision:null})}));
const form = ids['grid-region-form'];
form.querySelectorAll = () => boxes;
form.elements = {csrf_token:{value:'synthetic-csrf'}};
ids['grid-region-editor'].hidden = true;
ids['grid-region-merge'].checked = true;
const texts = Object.fromEntries(['selected','empty','loading','ready','shape','error','mapMissing','merged','separate'].map(key => [key,key]));
ids['grid-region-config'].textContent = JSON.stringify({grids:[{id:1,area:1,stores:3},{id:2,area:.96,stores:4}],texts});
const layers=[];
class Data {
  constructor(options) { this.options=options; this.handlers={}; layers.push(this); }
  addGeoJson(data) { this.data=data; }
  setMap(map) { this.map=map; }
  setStyle(style) { this.style=style; }
  addListener(name, fn) { this.handlers[name]=fn; }
}
let fail=false, pending, delay=false, scheduled, calls=0;
const fetch = async (url,options) => {
  calls++;
  const data=JSON.parse(options.body);
  assert.equal(data.csrf_token,'synthetic-csrf');
  assert.equal(data.selections.length,2);
  assert.equal(typeof data.merge_boundaries,'boolean');
  if (delay) await new Promise(resolve => {pending=resolve;});
  return {ok:!fail,json:async()=>fail?{error:'not-contiguous'}:{
    geometry:{type:'Polygon',coordinates:[]},
    display_geometry:{type:data.merge_boundaries?'Polygon':'GeometryCollection',geometries:[]},
    properties:{grid_count:2,area_km2:1.96,store_count:7,shape_valid:true}}};
};
const context={window:{},document:{getElementById:id=>ids[id]},AbortController,fetch,
  setTimeout(fn){scheduled=fn;return 1;},clearTimeout(){scheduled=null;},google:{maps:{Data}}};
vm.runInNewContext(fs.readFileSync('app/static/bd_grid_regions.js','utf8'),context);
const changed=target=>form.handlers.change({target});
const flush=async()=>{const callback=scheduled;scheduled=null;await callback?.();};
(async()=>{
  context.window.initGridRegionEditor({});
  boxes.forEach(box=>{box.checked=true;});
  changed(boxes[0]);
  assert.equal(calls,0); assert.equal(ids['grid-region-save'].disabled,true);
  await flush();
  assert.equal(calls,1); assert.equal(ids['grid-region-save'].disabled,false);
  assert.equal(layers.at(-1).data.features[0].geometry.type,'Polygon');
  assert.equal(layers[0].style({getProperty(){return 1;}}).strokeOpacity,0);
  ids['grid-region-merge'].checked=false; changed(ids['grid-region-merge']); await flush();
  assert.equal(calls,2);
  assert.equal(layers.at(-1).data.features[0].geometry.type,'GeometryCollection');
  assert.equal(layers[0].style({getProperty(){return 1;}}).strokeOpacity,1);
  assert.ok(ids['grid-region-status'].textContent.includes('separate'));
  let prevented=false;
  form.handlers.submit({preventDefault(){prevented=true;}});
  assert.equal(prevented,false);
  assert.equal(ids['grid-region-editor'].hidden,false);
  assert.equal(ids['grid-region-count'].textContent,'2');
  assert.equal(ids['grid-region-area'].textContent,'1.96');
  assert.equal(ids['grid-region-stores'].textContent,'7');
  assert.ok(boxes.every(box=>!box.parentElement.hidden));
  fail=true; changed(boxes[0]); await flush();
  assert.equal(ids['grid-region-save'].disabled,true);
  assert.equal(ids['grid-region-status'].textContent,'not-contiguous');
  fail=false; delay=true; changed(boxes[0]); const old=flush(); await Promise.resolve();
  ids['grid-region-clear'].handlers.click(); pending(); await old;
  assert.equal(ids['grid-region-save'].disabled,true);
  assert.ok(boxes.every(box=>!box.checked && box.parentElement.hidden));
  assert.equal(ids['grid-region-editor'].hidden,true);
  layers[0].handlers.click({feature:{getProperty(){return 1;}}});
  assert.equal(boxes[0].checked,true);
  assert.equal(ids['grid-region-editor'].hidden,false);
  assert.equal(ids['grid-region-count'].textContent,'1');
  assert.equal(boxes[0].parentElement.hidden,false);
  assert.equal(boxes[1].parentElement.hidden,true);
  layers[0].handlers.click({feature:{getProperty(){return 1;}}});
  assert.equal(ids['grid-region-editor'].hidden,true);
  console.log('automatic merge: debounce, both boundary modes, selected drawer, error and stale response passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
