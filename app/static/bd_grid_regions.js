/* 选择与边界模式变化后自动计算；仅最终保存改变数据库。 */
(() => {
  'use strict';
  const byElementId = id => document.getElementById(id);
  const root = byElementId('grid-region-editor');
  if (!root) return;
  const config = JSON.parse(byElementId('grid-region-config').textContent);
  const form = byElementId('grid-region-form');
  const boxes = [...form.querySelectorAll('[data-grid-id]')];
  const gridMetadata = new Map(config.grids.map(grid => [Number(grid.id), grid]));
  const byId = new Map(boxes.map(box => [Number(box.dataset.gridId), box]));
  const status = byElementId('grid-region-status'), save = byElementId('grid-region-save');
  const mode = byElementId('grid-region-merge');
  let map, layer, previewLayer, controller, timer, requestNumber = 0, calculated = '';
  const selection = () => boxes.filter(box => box.checked).map(box => JSON.parse(box.value));
  const key = () => JSON.stringify({selections: selection(), merge_boundaries: mode.checked});
  function style(feature) {
    const box = byId.get(Number(feature.getProperty('id')));
    const dissolved = box?.checked && mode.checked && !!calculated;
    return {fillColor: '#176b57', fillOpacity: dissolved ? 0 : box?.checked ? .3 : .03,
      strokeColor: '#176b57', strokeWeight: dissolved ? 0 : box?.checked ? 3 : 1,
      strokeOpacity: dissolved ? 0 : 1,
      clickable: !box?.disabled, zIndex: 5};
  }
  function changed() {
    clearTimeout(timer); controller?.abort(); requestNumber++;
    calculated = ''; save.disabled = true;
    window.setGridRegionPreviewZones?.([]);
    previewLayer?.setMap(null); previewLayer = null;
    const chosen = selection();
    const count = chosen.length;
    root.hidden = count === 0;
    boxes.forEach(box => { box.parentElement.hidden = !box.checked; });
    byElementId('grid-region-count').textContent = String(count);
    byElementId('grid-region-area').textContent = chosen.reduce((sum, grid) => sum + Number(gridMetadata.get(grid.id)?.area || 0), 0).toFixed(2);
    byElementId('grid-region-stores').textContent = chosen.reduce((sum, grid) => sum + Number(gridMetadata.get(grid.id)?.stores || 0), 0).toLocaleString();
    status.textContent = count ? `${config.texts.selected}: ${count} · ${config.texts.loading}` : config.texts.empty;
    layer?.setStyle(style);
    if (count) timer = setTimeout(calculate, 200);
  }
  async function calculate() {
    if (!selection().length) return;
    controller = new AbortController();
    const current = ++requestNumber, selectedKey = key();
    try {
      const response = await fetch('/zones/grid-merge-preview', {
        method: 'POST', signal: controller.signal, headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({selections: selection(), merge_boundaries: mode.checked,
          csrf_token: form.elements.csrf_token.value})});
      if (response.redirected) throw new Error(config.texts.error);
      const data = await response.json();
      if (current !== requestNumber || selectedKey !== key()) return;
      if (!response.ok) throw new Error(data.error || config.texts.error);
      if (map) {
        previewLayer = new google.maps.Data({map: !root.hidden ? map : null});
        previewLayer.addGeoJson({type: 'FeatureCollection', features: [{...data,
          geometry: data.display_geometry || data.geometry}]});
        previewLayer.setStyle({fillColor: '#176b57', fillOpacity: .12, strokeColor: '#094b3b',
          strokeWeight: 3, clickable: false, zIndex: 6});
      }
      calculated = selectedKey; save.disabled = false;
      byElementId('grid-region-area').textContent = data.properties.area_km2.toFixed(2);
      byElementId('grid-region-stores').textContent = Number(data.properties.store_count).toLocaleString();
      layer?.setStyle(style);
      window.setGridRegionPreviewZones?.(!root.hidden ? selection().map(grid => grid.zone_id).filter(id => id !== null) : []);
      status.textContent = `${data.properties.grid_count} ${config.texts.selected} · ` +
        `${data.properties.area_km2.toFixed(2)} km² · ${data.properties.store_count} · ` +
        (mode.checked ? config.texts.merged : config.texts.separate) + ' · ' +
        (data.properties.shape_valid ? config.texts.ready : config.texts.shape) +
        (map ? '' : ` ${config.texts.mapMissing}`);
    } catch (error) {
      if (error.name !== 'AbortError' && current === requestNumber) status.textContent = error.message || config.texts.error;
    }
  }
  form.addEventListener('change', event => {
    if (event.target.matches('[data-grid-id]') || event.target === mode) changed();
  });
  byElementId('grid-region-clear').addEventListener('click', () => {
    boxes.forEach(box => { box.checked = false; }); changed();
  });
  window.initGridRegionEditor = targetMap => {
    if (layer) return;
    map = targetMap;
    layer = new google.maps.Data({map});
    layer.addGeoJson({type: 'FeatureCollection', features: config.grids.map(grid => ({
      type: 'Feature', geometry: grid.geometry, properties: {id: grid.id}}))});
    layer.setStyle(style);
    if (config.fit_grids) {
      const bounds = new google.maps.LatLngBounds();
      layer.forEach(feature => feature.getGeometry().forEachLatLng(point => bounds.extend(point)));
      if (!bounds.isEmpty()) map.fitBounds(bounds, 32);
    }
    layer.addListener('click', event => {
      const box = byId.get(Number(event.feature.getProperty('id')));
      if (!box || box.disabled) return;
      box.checked = !box.checked; changed();
    });
  };
  if (window.zoneManagementMap) window.initGridRegionEditor(window.zoneManagementMap);
  form.addEventListener('submit', event => {
    if (!calculated || calculated !== key()) { event.preventDefault(); save.disabled = true; }
  });
  window.addEventListener?.('pagehide', () => { clearTimeout(timer); controller?.abort(); });
})();
