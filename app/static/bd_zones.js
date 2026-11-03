/* Read-only, admin-only preview. No drawing/editing/dispatch controls. */
(() => {
  'use strict';
  const config = JSON.parse(document.getElementById('zone-config').textContent);
  const texts = config.texts;
  const el = (id) => document.getElementById(id);
  const form = el('zone-form');
  const colors = ['#225fa2', '#a45126', '#2c7751', '#80519e', '#ad4657', '#287c88', '#8b681c', '#525ea4'];
  let map = null, zones = null, grids = null, outline = null, pending = null, boundaries = null, gridBoundaries = null, reviews = null;
  let result = null, selected = null, controller = null, sequence = 0, busy = false;
  const format = (value, digits = 0) => Number(value).toLocaleString(config.language, { maximumFractionDigits: digits });
  const ward = () => config.scope;
  const showMapError = (message) => { el('zone-map-status').textContent = message; el('zone-map-status').hidden = false; };

  function clearLayer(layer) {
    if (layer) layer.forEach((feature) => layer.remove(feature));
  }

  function fitGeometry(geometry) {
    if (!map || !geometry) return;
    const bounds = new google.maps.LatLngBounds();
    const visit = (coords) => {
      if (typeof coords[0] === 'number') bounds.extend({ lng: coords[0], lat: coords[1] });
      else coords.forEach(visit);
    };
    visit(geometry.coordinates);
    if (!bounds.isEmpty()) map.fitBounds(bounds, 32);
  }

  function repaint() {
    el('zone-rows').querySelectorAll('tr[data-id]').forEach((row) => {
      row.classList.toggle('is-selected', row.dataset.id === selected);
      row.querySelector('button').setAttribute('aria-pressed', String(row.dataset.id === selected));
    });
    if (!map) return;
    reviews.setStyle({ visible: el('zone-review-layer').checked, icon: { path: google.maps.SymbolPath.CIRCLE, scale: 5, fillColor: '#b42318', fillOpacity: .9, strokeColor: '#fff', strokeWeight: 1 }, zIndex: 8 });
    const indices = new Map((result?.features || []).map((feature, i) => [feature.id, i]));
    zones.setStyle((feature) => {
      const id = feature.getProperty('id');
      return { fillColor: ['independent_land', 'residual_land'].includes(feature.getProperty('unit_kind')) ? '#a65f00' : colors[(indices.get(id) || 0) % colors.length],
        fillOpacity: selected && selected !== id ? .12 : .3,
        strokeWeight: 0, zIndex: selected === id ? 3 : 2 };
    });
    const density = el('zone-density-layer').checked;
    const densityColors = ['#fff1cf', '#f8d489', '#efa04e', '#d7672d', '#9e351e'];
    grids.setStyle((feature) => {
      const shopsPerKm = Number(feature.getProperty('store_count')) / Math.max(Number(feature.getProperty('area_km2')), .000001);
      const band = shopsPerKm < 50 ? 0 : shopsPerKm < 200 ? 1 : shopsPerKm < 500 ? 2 : shopsPerKm < 1000 ? 3 : 4;
      return { visible: Boolean(density || (el('zone-grid-layer').checked && selected && feature.getProperty('zone_id') === selected)),
        fillColor: densityColors[band], fillOpacity: density ? .65 : 0, strokeWeight: 0, zIndex: 4, clickable: false };
    });
    boundaries.setStyle((feature) => ({ strokeColor: selected === feature.getProperty('id') ? '#152d4c' : '#fff', strokeWeight: selected === feature.getProperty('id') ? 3 : 1.6, zIndex: 4, clickable: false }));
    gridBoundaries.setStyle((feature) => ({ visible: Boolean(el('zone-grid-layer').checked && selected && feature.getProperty('zone_id') === selected), strokeColor: '#193a5e', strokeWeight: 1, zIndex: 5, clickable: false }));
  }

  function select(id) {
    selected = id;
    const feature = result?.features.find((item) => item.id === id);
    if (feature) fitGeometry(feature.geometry);
    repaint();
  }

  function exteriorBoundary(feature) {
    const polygons = feature.geometry.type === 'Polygon' ? [feature.geometry.coordinates] : feature.geometry.coordinates;
    return { type: 'Feature', properties: feature.properties, geometry: { type: 'MultiLineString', coordinates: polygons.map((rings) => rings[0]) } };
  }

  function renderMap() {
    if (!map) return;
    clearLayer(zones); clearLayer(grids); clearLayer(outline); clearLayer(pending); clearLayer(boundaries); clearLayer(gridBoundaries); clearLayer(reviews);
    const boundary = result?.boundary || ward()?.boundary;
    if (boundary) outline.addGeoJson({ type: 'Feature', geometry: boundary, properties: {} });
    if (result) {
      zones.addGeoJson({ type: 'FeatureCollection', features: result.features });
      grids.addGeoJson({ type: 'FeatureCollection', features: result.grids });
      boundaries.addGeoJson({ type: 'FeatureCollection', features: result.features.map(exteriorBoundary) });
      gridBoundaries.addGeoJson({ type: 'FeatureCollection', features: result.grids.map(exteriorBoundary) });
      pending.addGeoJson({ type: 'FeatureCollection', features: result.uncovered_features || [] });
      reviews.addGeoJson({ type: 'FeatureCollection', features: (result.review_merchants || []).filter((row) => Number.isFinite(row.lng) && Number.isFinite(row.lat)).map((row) => ({ type: 'Feature', geometry: { type: 'Point', coordinates: [row.lng,row.lat] }, properties: row })) });
    }
    fitGeometry(boundary);
    repaint();
  }

  function reset() {
    sequence += 1;
    controller?.abort();
    busy = false;
    result = null; selected = null;
    const current = ward();
    el('zone-data-status').textContent = current?.ready
      ? `${texts.ready} · ${current.store_count_pending ? texts.databasePending + ' · ' + current.merchant_source : format(current.store_count) + ' ' + texts.shops} · ${format(current.grid_count)} ${texts.cells}`
      : texts.missing;
    el('zone-generate').disabled = !current?.ready;
    el('zone-generate').textContent = texts.generate;
    el('zone-rows').replaceChildren();
    const row = el('zone-rows').insertRow();
    const cell = row.insertCell(); cell.colSpan = 5; cell.textContent = texts.noResult;
    el('zone-summary').textContent = '';
    el('zone-source').textContent = current?.captured_at ? `${texts.snapshot}: ${current.captured_at.slice(0, 10)}` : '';
    el('zone-result-name').textContent = current?.name || '';
    el('zone-validity').textContent = texts.noResult;
    el('zone-issues').hidden = true;
    el('zone-audit').hidden = true;
    el('zone-review-layer').checked = false;
    el('zone-notice').className = 'msg';
    el('zone-notice').textContent = texts.previewNotice;
    renderMap();
  }

  function render(data) {
    result = data; selected = null;
    const summary = data.summary, meta = data.meta;
    el('zone-result-name').textContent = meta.ward_name;
    el('zone-validity').textContent = summary.eligible ? texts.valid : texts.draft;
    el('zone-summary').textContent = `${summary.region_candidate_count ?? summary.zone_count} ${texts.regionCandidates} · ${summary.independent_unit_count || 0} ${texts.landUnits} · ${summary.grid_count} ${texts.grids} · ${format(meta.store_count)} ${texts.shops} · ${format(meta.area_km2, 2)} km²${summary.balance ? ` · ${texts.workload} ${format(summary.workload_min, 2)}～${format(summary.workload_max, 2)} · ${texts.merchantRange} ${format(summary.merchant_count_min)}～${format(summary.merchant_count_max)}` : ` · ${texts.ratio} ${format(summary.area_ratio, 2)}`} · ${texts.excluded} ${format(meta.excluded_area_km2, 2)} km²`;
    el('zone-source').textContent = `${texts.snapshot}: ${meta.captured_at.slice(0, 10)} · ${meta.source.merchants} · ${meta.source.osm_timestamp}`;
    el('zone-issues').textContent = data.issues.map((issue) => `${texts[issue.code] || texts.unknownIssue}${issue.count ? ` (${issue.count})` : ''}`).join('\n');
    el('zone-issues').hidden = !data.issues.length;
    el('zone-issues').className = data.issues.some((issue) => issue.severity !== 'info') ? 'msg err' : 'msg';
    el('zone-rows').replaceChildren();
    data.features.forEach((feature, index) => {
      const properties = feature.properties;
      const row = el('zone-rows').insertRow(); row.dataset.id = feature.id;
      const cell = row.insertCell();
      const button = document.createElement('button'); button.type = 'button'; button.className = 'zone-row-select';
      button.setAttribute('aria-pressed', 'false');
      const swatch = document.createElement('span'); swatch.className = 'zone-swatch'; swatch.style.backgroundColor = colors[index % colors.length];
      button.append(swatch, `${['independent_land', 'residual_land'].includes(properties.unit_kind) ? texts.landUnit : texts.zones} ${String(index + 1).padStart(2, '0')}`);
      const id = document.createElement('small'); id.textContent = feature.id; button.append(id);
      button.addEventListener('click', () => select(feature.id)); cell.append(button);
      row.insertCell().textContent = properties.area_km2 < .001 ? `${format(properties.area_km2 * 1000000, 3)} m²` : `${format(properties.area_km2, 3)} km²`;
      row.insertCell().textContent = format(properties.store_count);
      row.insertCell().textContent = format(properties.grid_count);
      const loadCell = row.insertCell();
      loadCell.textContent = properties.workload == null ? '—' : format(properties.workload, 3);
      const problems = [];
      if (properties.load_valid === false) problems.push(texts.workload_conflict);
      if (properties.shape_valid === false) problems.push(texts.zone_shape);
      if (problems.length) {
        loadCell.title = problems.join(' ');
        const notice = document.createElement('small'); notice.textContent = problems.join(' ');
        notice.className = 'zone-row-review'; cell.append(notice);
      }
    });
    renderAudit(data);
    renderMap();
  }

  function renderAudit(data) {
    const audit = data.audit;
    el('zone-audit').hidden = !audit;
    if (!audit) return;
    el('zone-audit-status').textContent = data.summary.confirmation_ready ? texts.auditReady : texts.auditBlocked;
    el('zone-audit-status').className = data.summary.confirmation_ready ? 'msg ok' : 'msg err';
    el('zone-audit-rows').replaceChildren();
    const values = [[texts.auditArea,format(audit.uncovered_m2,4)], [texts.auditOverlap,format(audit.overlap_m2,4)], [texts.auditOutside,format(audit.outside_m2,4)],
      [texts.auditDuplicateUnits,format((audit.duplicate_cell_ids || 0)+(audit.duplicate_region_ids || 0)+(audit.duplicate_grid_ids || 0))],
      [texts.auditSourceStores,format(audit.source_merchant_count)], [texts.auditWorkStores,format(audit.expected_merchants)],
      [texts.auditAssigned,format(audit.assigned_merchants)], [texts.auditUnassigned,format(audit.unassigned_merchants)],
      [texts.auditDuplicate,format(audit.duplicate_merchants)], [texts.auditExcluded,format(audit.excluded_unreviewed)],
      [texts.auditInvalid,format((audit.invalid_source_coordinates || 0) + audit.source_duplicates)]];
    values.forEach(([label,value]) => { const row=el('zone-audit-rows').insertRow(); const header=document.createElement('th'); header.scope='row';header.textContent=label;row.append(header);row.insertCell().textContent=value; });
    const rows=data.review_merchants || [];
    el('zone-review').hidden=!rows.length;
    el('zone-review-count').textContent=`(${format(rows.length)})`;
    el('zone-review-rows').replaceChildren();
    rows.slice(0,50).forEach((merchant) => { const row=el('zone-review-rows').insertRow();row.insertCell().textContent=merchant.name || merchant.store_id;row.insertCell().textContent=merchant.reason==='excluded_terrain' ? texts.excludedReason : merchant.reason==='invalid_coordinates' ? texts.invalidLocation : texts.unlocatedReason; });
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy || !form.reportValidity() || !ward()?.ready) return;
    busy = true; controller?.abort(); controller = new AbortController();
    const requestId = ++sequence;
    el('zone-generate').disabled = true; el('zone-generate').textContent = texts.loading;
    el('zone-notice').textContent = texts.loading;
    try {
      const params = new URLSearchParams(new FormData(form));
      const response = await fetch(`/zones/preview?${params}`, { signal: controller.signal, headers: { Accept: 'application/json' } });
      if (response.redirected || !(response.headers.get('content-type') || '').includes('application/json')) throw new Error(texts.requestError);
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : texts.requestError);
      if (requestId !== sequence) return;
      render(data);
      el('zone-notice').className = 'msg'; el('zone-notice').textContent = texts.previewNotice;
    } catch (error) {
      if (error.name !== 'AbortError' && requestId === sequence) {
        el('zone-notice').className = 'msg err'; el('zone-notice').textContent = error.message || texts.requestError;
      }
    } finally {
      if (requestId === sequence) { busy = false; el('zone-generate').disabled = !ward()?.ready; el('zone-generate').textContent = texts.generate; }
    }
  });

  el('zone-density-layer').addEventListener('change', repaint);
  el('zone-grid-layer').addEventListener('change', repaint);
  el('zone-review-layer').addEventListener('change', repaint);
  reset();
  window.gm_authFailure = () => showMapError(texts.mapError);
  window.initBdZoneMap = () => {
    map = new google.maps.Map(el('zone-map'), { center: { lat: 35.6895, lng: 139.7005 }, zoom: 11,
      mapTypeControl: false, streetViewControl: false, fullscreenControl: true });
    zones = new google.maps.Data({ map }); grids = new google.maps.Data({ map }); outline = new google.maps.Data({ map }); pending = new google.maps.Data({ map }); boundaries = new google.maps.Data({ map }); gridBoundaries = new google.maps.Data({ map }); reviews = new google.maps.Data({ map });
    const info = new google.maps.InfoWindow();
    reviews.addListener('click', (event) => { const node=document.createElement('div');node.textContent=`${event.feature.getProperty('name') || event.feature.getProperty('store_id')} · ${event.feature.getProperty('reason')==='excluded_terrain' ? texts.excludedReason : texts.unlocatedReason}`;info.setContent(node);info.setPosition(event.latLng);info.open(map); });
    pending.setStyle({ fillColor: '#b42318', fillOpacity: .65, strokeColor: '#b42318', strokeWeight: 1, zIndex: 7, clickable: false });
    outline.setStyle({ fillOpacity: 0, strokeColor: '#40516a', strokeWeight: 2, clickable: false, zIndex: 1 });
    zones.addListener('click', (event) => select(event.feature.getProperty('id')));
    el('zone-map-status').hidden = true; renderMap();
  };
  if (!config.key) showMapError(texts.keyMissing);
  else {
    const url = new URL('https://maps.googleapis.com/maps/api/js');
    url.search = new URLSearchParams({ key: config.key, callback: 'initBdZoneMap', loading: 'async', language: config.language === 'ja' ? 'ja' : 'zh-CN' });
    const script = document.createElement('script'); script.src = url.toString(); script.async = true;
    script.onerror = () => showMapError(texts.mapError); document.head.append(script);
  }
})();
