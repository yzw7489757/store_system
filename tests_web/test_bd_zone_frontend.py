"""真实页面JS的DOM/地图适配冒烟；不启动临时服务，不调用外部SDK。"""
import json
import re
import shutil
import subprocess
from pathlib import Path
import pytest
from tests_web.test_bd_zone_coverage import admin,source,snapshot  # noqa:F401


def test_frontend_renders_loads_source_selection_and_failure():
    if not shutil.which('node'):
        pytest.skip('Node is required for frontend script verification')
    completed=subprocess.run(['node','tests_web/bd_zone_management_mock.cjs'],text=True,capture_output=True,timeout=15)
    assert completed.returncode==0,completed.stderr
    assert json.loads(completed.stdout)['drawer_and_recovery']


def test_h5_map_selects_grid_and_opens_matching_store_form():
    """执行实际H5地图脚本；不联网、不加载Google SDK、不启动新服务。"""
    if not shutil.which('node'):
        pytest.skip('Node is required for frontend script verification')
    script=r'''
const fs=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const ring=[[139,35],[139.01,35],[139.01,35.01],[139,35]];
const shopId='shop:001/alpha';
const config={key:'synthetic-browser-key',language:'zh',error:'map-error',
 geometry:{type:'FeatureCollection',features:[{type:'Feature',geometry:{type:'Polygon',coordinates:[ring]},properties:{id:12}}]},
 stores:[{id:shopId,name:'shop-one',lat:35.002,lon:139.002,visited:false},
         {id:'invalid',name:'invalid',lat:null,lon:null}]};
const details={open:false};
let gridSelected=false,shopSelected=false,appended;
const elements={
 'work-map-config':{textContent:JSON.stringify(config)},
 'work-map-status':{textContent:''},'work-map':{},
 'grid-12':{scrollIntoView(){gridSelected=true;}},
 ['store-'+shopId]:{querySelector(selector){assert.equal(selector,'details');return details;},scrollIntoView(){shopSelected=true;}}
};
const layers=[];
class Data {
 constructor(){this.handlers={};layers.push(this);}
 addGeoJson(collection){this.features=collection.features;}
 setStyle(style){this.style=style;}
 addListener(name,handler){this.handlers[name]=handler;}
}
const context={URL,URLSearchParams,window:{},document:{
 getElementById(id){return elements[id];},createElement(){return {};},head:{append(node){appended=node;}}},
 google:{maps:{Data,SymbolPath:{CIRCLE:'circle'},Map:class {fitBounds(){}},
 LatLngBounds:class {extend(){} isEmpty(){return false;}}}}};
vm.runInNewContext(fs.readFileSync('app/static/bd_work.js','utf8'),context);
assert.ok(appended.src.startsWith('https://maps.googleapis.com/maps/api/js?'));
context.window.initWorkMap();
assert.equal(layers.length,3);
const polygons=layers[0],shops=layers[2];
assert.equal(shops.features.length,1);
assert.equal(shops.features[0].properties.store_id,shopId);
shops.handlers.click({feature:{getProperty(key){return shops.features[0].properties[key];}}});
assert.ok(details.open && shopSelected);
polygons.handlers.click({feature:{getProperty(){return 12;}}});
assert.ok(gridSelected);
context.window.gm_authFailure();
assert.equal(elements['work-map-status'].textContent,'map-error');
console.log('H5 map: grid selection, store selection and auth failure passed');
'''
    result=subprocess.run(['node','-e',script],text=True,capture_output=True,timeout=15)
    assert result.returncode==0,result.stderr
