/* H5地图仅渲染服务端已授权范围，不把未派发区域下载给队员。 */
(() => {
  'use strict';
  const config=JSON.parse(document.getElementById('work-map-config').textContent);
  const notice=document.getElementById('work-map-status');
  window.gm_authFailure=()=>{notice.textContent=config.error;};
  window.initWorkMap=()=>{
    const map=new google.maps.Map(document.getElementById('work-map'),{center:{lat:35.68,lng:139.76},zoom:11,mapTypeControl:false,streetViewControl:false});
    const layer=new google.maps.Data({map});layer.addGeoJson(config.geometry);
    layer.setStyle(f=>({fillColor:f.getProperty('review')?'#a65f00':f.getProperty('assigned')?'#225fa2':'#2c7751',fillOpacity:.25,strokeWeight:0}));
    const outlines=new google.maps.Data({map});
    outlines.addGeoJson({type:'FeatureCollection',features:config.geometry.features.map(f=>({type:'Feature',geometry:{type:'MultiLineString',coordinates:(f.geometry.type==='Polygon'?[f.geometry.coordinates]:f.geometry.coordinates).map(r=>r[0])},properties:f.properties}))});
    outlines.setStyle({strokeColor:'#193a5e',strokeWeight:2,clickable:false});
    const bounds=new google.maps.LatLngBounds();
    function extend(coords){if(typeof coords[0]==='number')bounds.extend({lng:coords[0],lat:coords[1]});else coords.forEach(extend);}
    config.geometry.features.forEach(f=>extend(f.geometry.coordinates));
    if(!bounds.isEmpty())map.fitBounds(bounds,24);
    const shops=new google.maps.Data({map});
    shops.addGeoJson({type:'FeatureCollection',features:(config.stores||[]).filter(s=>Number.isFinite(s.lat)&&Number.isFinite(s.lon)).map(s=>({type:'Feature',geometry:{type:'Point',coordinates:[s.lon,s.lat]},properties:{store_id:s.id,visited:s.visited,title:s.name}}))});
    shops.setStyle(f=>({title:f.getProperty('title'),icon:{path:google.maps.SymbolPath.CIRCLE,scale:6,fillColor:f.getProperty('visited')?'#2c7751':'#225fa2',fillOpacity:1,strokeColor:'#fff',strokeWeight:2}}));
    shops.addListener('click',event=>{const card=document.getElementById('store-'+event.feature.getProperty('store_id'));if(card){card.querySelector('details').open=true;card.scrollIntoView({behavior:'smooth',block:'start'});}});
    layer.addListener('click',event=>{const id=Number(event.feature.getProperty('id'));if(Number.isInteger(id))document.getElementById((config.selectPrefix || 'grid-').replace(/^#/, '')+id)?.scrollIntoView({behavior:'smooth',block:'start'});});
  };
  if(!config.key){notice.textContent=config.error;return;}
  const url=new URL('https://maps.googleapis.com/maps/api/js');
  url.search=new URLSearchParams({key:config.key,callback:'initWorkMap',loading:'async',language:config.language==='ja'?'ja':'zh-CN'});
  const script=document.createElement('script');script.src=url.toString();script.async=true;script.onerror=()=>{notice.textContent=config.error;};document.head.append(script);
})();
