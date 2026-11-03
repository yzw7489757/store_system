/* 管理地图只读取已落库片区；点击按需读取管理抽屉。 */
(() => {
  'use strict';
  const config = JSON.parse(document.getElementById('zone-management-config').textContent);
  const texts = config.texts;
  const byId = id => document.getElementById(id);
  const drawer = byId('zone-drawer'), panel = byId('zone-panel'), select = byId('zone-select');
  const notice = byId('zone-map-status');
  let gridLayer, popupTimer;
  let map, layer, popup, bounds, selected = config.selected, controller, requestNumber = 0;
  const features = new Map();
  let previewHiddenRegions = new Set();
  window.setGridRegionPreviewZones = ids => {
    previewHiddenRegions = new Set(ids);
    layer?.setStyle(style);
  };
  const format = value => Number(value).toLocaleString(config.language, {maximumFractionDigits: 2});
  function notify(message) { notice.textContent = message; notice.hidden = !message; }
  // 仅用于地图展示：封闭内洞一起圈入，不改变真实几何、面积或商户归属。
  // 保留每个Polygon的外环；不跨凹口、海湾或独立MultiPolygon连接。
  function displayCollection(collection) {
    return {...collection, features: collection.features.map(feature => {
      if (feature.properties?.preserve_holes) return feature;
      const geometry = feature.geometry;
      if (geometry.type === 'Polygon') {
        return {...feature, geometry: {...geometry, coordinates: [geometry.coordinates[0]]}};
      }
      if (geometry.type === 'MultiPolygon') {
        return {...feature, geometry: {...geometry, coordinates: geometry.coordinates.map(polygon => [polygon[0]])}};
      }
      return feature;
    })};
  }
  const boundaryColor = '#c2187a';
  const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)');
  let pulseFrame = null, lastPulse = 0;
  function style(feature) {
    const id = Number(feature.getProperty('id'));
    const active = id === selected;
    return {visible: config.mode !== 'manage' && !previewHiddenRegions.has(id), fillColor: boundaryColor, fillOpacity: active ? .13 : 0,
      strokeColor: boundaryColor, strokeOpacity: 1,
      strokeWeight: active ? 4 : 2.5, zIndex: active ? 2 : 1};
  }
  function stopPulse() {
    if (pulseFrame !== null) window.cancelAnimationFrame(pulseFrame);
    pulseFrame = null;
  }
  function pulse(time) {
    pulseFrame = null;
    const feature = features.get(selected);
    if (!feature || reducedMotion?.matches || document.hidden) return;
    if (time-lastPulse >= 80) {
      const intensity = (Math.sin(time/550)+1)/2;
      layer.overrideStyle(feature, {strokeColor: boundaryColor, strokeOpacity: .65+.35*intensity,
        strokeWeight: 4+2*intensity});
      lastPulse = time;
    }
    pulseFrame = window.requestAnimationFrame(pulse);
  }
  function highlight() {
    stopPulse();
    if (layer) {
      layer.revertStyle(); layer.setStyle(style);
      if (features.has(selected) && !reducedMotion?.matches && !document.hidden)
        pulseFrame = window.requestAnimationFrame(pulse);
    }
  }
  document.addEventListener('visibilitychange', highlight);
  reducedMotion?.addEventListener('change', highlight);
  window.addEventListener?.('pagehide', stopPulse);
  function hideGrids() {
    if (gridLayer) gridLayer.setMap(null);
    gridLayer = null;
  }
  panel.addEventListener('change', event => {
    if (event.target.id !== 'zone-grid-toggle') return;
    hideGrids();
    if (!event.target.checked) return;
    if (!map) { event.target.checked = false; notify(texts.keyMissing); return; }
    try {
      const data = JSON.parse(byId('zone-grid-data').textContent);
      gridLayer = new google.maps.Data({map});
      gridLayer.addGeoJson(displayCollection(data));
      gridLayer.setStyle(feature => ({fillOpacity: 0, strokeColor: boundaryColor,
        strokeWeight: 1.5, clickable: false, zIndex: 3}));
    } catch {
      hideGrids(); event.target.checked = false; notify(texts.mapError);
    }
  });
  function focusZone(id) {
    if (!map || !features.has(id)) return;
    const area = new google.maps.LatLngBounds();
    features.get(id).getGeometry().forEachLatLng(point => area.extend(point));
    if (!area.isEmpty()) map.fitBounds(area, {top: 80, right: window.innerWidth > 760 ? 460 : 30, bottom: 70, left: 30});
  }
  function setSelection(id) {
    selected = id; select.value = id === null ? '' : String(id);
    const url = new URL(window.location.href);
    if (id === null) url.searchParams.delete('zone'); else url.searchParams.set('zone', String(id));
    window.history.replaceState(null, '', url);
    highlight();
  }
  async function openZone(id, focus = true) {
    if (config.mode === 'manage' || !Number.isInteger(id) || id < 1) return;
    hideGrids();
    controller?.abort(); controller = new AbortController();
    const current = ++requestNumber;
    clearTimeout(popupTimer);
    setSelection(id); popup?.close(); drawer.hidden = false;
    panel.textContent = texts.loading; panel.setAttribute('aria-busy', 'true');
    focusZone(id);
    if (focus) byId('zone-close').focus({preventScroll: true});
    try {
      const response = await fetch(`/zones/${id}/panel${window.location.search}`, {
        signal: controller.signal, headers: {'X-Requested-With': 'XMLHttpRequest'}});
      if (current !== requestNumber) return;
      if (response.redirected && new URL(response.url).pathname === '/login') throw new Error(texts.login);
      if (!response.ok) throw new Error(texts.detailError);
      const html = await response.text();
      if (current !== requestNumber) return;
      panel.innerHTML = html;
      if (focus) byId('zone-detail-title')?.focus({preventScroll: true});
    } catch (error) {
      if (error.name === 'AbortError' || current !== requestNumber) return;
      const message = document.createElement('p'); message.textContent = error.message || texts.detailError;
      const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'btn ghost';
      retry.textContent = texts.retry; retry.addEventListener('click', () => openZone(id));
      panel.replaceChildren(message, retry);
    } finally { if (current === requestNumber) panel.removeAttribute('aria-busy'); }
  }
  function closeDrawer() {
    hideGrids();
    controller?.abort(); requestNumber++; drawer.hidden = true; panel.removeAttribute('aria-busy');
    setSelection(null); popup?.close(); select.focus({preventScroll: true});
  }
  select.addEventListener('change', () => select.value ? openZone(Number(select.value)) : closeDrawer());
  byId('zone-close').addEventListener('click', closeDrawer);
  byId('zone-reset').addEventListener('click', () => { closeDrawer(); if (map && !bounds.isEmpty()) map.fitBounds(bounds, 32); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && !drawer.hidden) closeDrawer(); });
  window.gm_authFailure = () => notify(texts.mapError);
  window.initZoneManagementMap = () => {
    try {
      map = new google.maps.Map(byId('zone-map'), {center: {lat: 35.68, lng: 139.76}, zoom: 11,
        mapTypeControl: false, streetViewControl: false, fullscreenControl: false, gestureHandling: 'greedy'});
      layer = new google.maps.Data({map});
      layer.addGeoJson(displayCollection(config.geometry)).forEach(feature => features.set(Number(feature.getProperty('id')), feature));
      layer.setStyle(style); popup = new google.maps.InfoWindow({disableAutoPan: true, headerDisabled: true});
      bounds = new google.maps.LatLngBounds();
      layer.forEach(feature => feature.getGeometry().forEachLatLng(point => bounds.extend(point)));
      if (!bounds.isEmpty()) map.fitBounds(bounds, 32);
      layer.addListener('mouseover', event => {
        clearTimeout(popupTimer);
        const feature = event.feature;
        layer.overrideStyle(feature, {fillOpacity: Number(feature.getProperty('id')) === selected ? .13 : 0, strokeWeight: 4});
        const content = document.createElement('div'); content.className = 'zone-hover';
        content.addEventListener('mouseenter', () => clearTimeout(popupTimer));
        content.addEventListener('mouseleave', () => popup.close());
        const header = document.createElement('div'); header.className = 'zone-hover-heading';
        const title = document.createElement('strong'); title.textContent = feature.getProperty('name');
        const close = document.createElement('button'); close.type = 'button'; close.className = 'zone-hover-close';
        close.textContent = '×'; close.setAttribute('aria-label', texts.close);
        close.addEventListener('click', () => popup.close());
        header.append(title, close); content.append(header);
        for (const value of [
          `${texts.area} ${format(feature.getProperty('area'))} km² · ${texts.stores} ${format(feature.getProperty('stores'))}`,
          `${texts.team}：${feature.getProperty('team') || texts.unassigned}`,
          `${texts.leader}：${feature.getProperty('leader') || texts.unassigned}`,
          `${texts.progress}：${format(feature.getProperty('progress') || 0)}%`]) {
          const line = document.createElement('p'); line.textContent = value; content.append(line);
        }
        popup.setContent(content); popup.setPosition(event.latLng); popup.open({map, shouldFocus: false});
      });
      layer.addListener('mouseout', event => {
        layer.revertStyle(event.feature);
        popupTimer = setTimeout(() => popup.close(), 180);
      });
      layer.addListener('click', event => openZone(Number(event.feature.getProperty('id'))));
      highlight();
      if (selected !== null) focusZone(selected);
      window.zoneManagementMap = map;
      window.initGridRegionEditor?.(map);
      notify('');
    } catch { notify(texts.mapError); }
  };
  if (!config.key) { notify(texts.keyMissing); return; }
  const script = document.createElement('script');
  const query = new URLSearchParams({key: config.key, callback: 'initZoneManagementMap', language: config.language, v: 'weekly', loading: 'async'});
  script.src = `https://maps.googleapis.com/maps/api/js?${query}`;
  script.async = true; script.onerror = () => notify(texts.mapError); document.head.append(script);
})();
