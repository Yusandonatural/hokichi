/* hokichi 耕作放棄地マップ — 依存: maplibre-gl（CDN）。データは ./data/index.json と ./data/<code>.geojson */
(function () {
  "use strict";

  const GSI = {
    pale:  { url: "https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png", max: 18 },
    std:   { url: "https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png", max: 18 },
    photo: { url: "https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{z}/{x}/{y}.jpg", max: 18 },
  };
  const GSI_ATTR = '<a href="https://maps.gsi.go.jp/development/ichiran.html" target="_blank" rel="noopener">地理院タイル</a>';
  const NARA_CENTER = [135.93, 34.42];
  const CART_KEY = "hokichi.cart.v1";

  const $ = (id) => document.getElementById(id);
  const el = {
    city: $("city"), minScore: $("minScore"), minScoreOut: $("minScoreOut"),
    lendOnly: $("lendOnly"), hideUnknown: $("hideUnknown"), landuse: $("landuse"),
    basemap: $("basemap"), summary: $("summary"), list: $("list"), detail: $("detail"),
    attrib: $("attrib"), cartBtn: $("cartBtn"), cartCount: $("cartCount"),
    cart: $("cart"), cartTable: $("cartTable").querySelector("tbody"),
    cartCsv: $("cartCsv"), cartLetter: $("cartLetter"), cartClear: $("cartClear"),
    cartClose: $("cartClose"), letterOut: $("letterOut"),
  };

  let index = null;          // index.json
  let fc = null;             // 現在の市町村の FeatureCollection
  let activeId = null;
  let cart = loadCart();

  // ---------------------------------------------------------------- 地図
  const map = new maplibregl.Map({
    container: "map",
    style: styleFor("pale"),
    center: NARA_CENTER, zoom: 9.3,
    attributionControl: false,
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");
  map.addControl(new maplibregl.AttributionControl({ compact: false }), "bottom-right");
  map.addControl(new maplibregl.GeolocateControl({ trackUserLocation: false }), "top-right");

  function styleFor(kind) {
    const b = GSI[kind];
    return {
      version: 8,
      glyphs: "https://maps.gsi.go.jp/xyz/noto-jp/{fontstack}/{range}.pbf",
      sources: { gsi: { type: "raster", tiles: [b.url], tileSize: 256, maxzoom: b.max, attribution: GSI_ATTR } },
      layers: [{ id: "gsi", type: "raster", source: "gsi" }],
    };
  }

  const legend = document.createElement("div");
  legend.className = "legend";
  legend.innerHTML = '<i style="background:#1b7f4c"></i>90点以上 <i style="background:#8fbf3f"></i>70〜 <i style="background:#e6b422"></i>50〜 <i style="background:#c0504d"></i>〜50 ／ 破線＝区画形状は推定';
  $("map").appendChild(legend);

  function addDataLayers() {
    if (map.getSource("parcels")) return;
    map.addSource("parcels", { type: "geojson", data: fc || emptyFC(), promoteId: "cid" });
    map.addSource("cities", { type: "geojson", data: cityPointsFC() });
    map.addLayer({
      id: "parcel-fill", type: "fill", source: "parcels",
      paint: { "fill-color": scoreColorExpr(), "fill-opacity": ["case", ["boolean", ["feature-state", "active"], false], 0.75, 0.45] },
    });
    map.addLayer({
      id: "parcel-line", type: "line", source: "parcels",
      paint: {
        "line-color": ["case", ["boolean", ["feature-state", "active"], false], "#000", scoreColorExpr()],
        "line-width": ["case", ["boolean", ["feature-state", "active"], false], 3, 1.2],
        "line-dasharray": ["case", ["get", "matched"], ["literal", [1, 0]], ["literal", [2, 1.5]]],
      },
    });
    map.addLayer({
      id: "parcel-label", type: "symbol", source: "parcels", minzoom: 15,
      layout: { "text-field": ["get", "parcel"], "text-size": 11, "text-font": ["NotoSansCJKjp-Regular"], "text-allow-overlap": false },
      paint: { "text-color": "#1f2a1f", "text-halo-color": "#fff", "text-halo-width": 1.2 },
    });
    map.addLayer({
      id: "city-circle", type: "circle", source: "cities",
      paint: { "circle-color": "#2e7d4f", "circle-opacity": 0.55, "circle-stroke-color": "#fff", "circle-stroke-width": 1.5,
               "circle-radius": ["interpolate", ["linear"], ["sqrt", ["get", "count"]], 0, 6, 30, 30, 200, 60] },
    });
    map.addLayer({
      id: "city-label", type: "symbol", source: "cities",
      layout: { "text-field": ["concat", ["get", "name"], "\n", ["get", "count"], "筆"], "text-size": 11, "text-font": ["NotoSansCJKjp-Regular"] },
      paint: { "text-color": "#1f2a1f", "text-halo-color": "#fff", "text-halo-width": 1.2 },
    });

    map.on("click", "parcel-fill", (e) => { const f = e.features[0]; select(f.properties.cid, false); });
    map.on("click", "city-circle", (e) => { el.city.value = e.features[0].properties.code; loadCity(el.city.value); });
    map.on("mouseenter", "parcel-fill", () => map.getCanvas().style.cursor = "pointer");
    map.on("mouseleave", "parcel-fill", () => map.getCanvas().style.cursor = "");
    map.on("mouseenter", "city-circle", () => map.getCanvas().style.cursor = "pointer");
    map.on("mouseleave", "city-circle", () => map.getCanvas().style.cursor = "");
    setCityLayerVisibility();
  }

  function scoreColorExpr() {
    return ["step", ["get", "score"], "#c0504d", 50, "#e6b422", 70, "#8fbf3f", 90, "#1b7f4c"];
  }
  function scoreColor(s) { return s >= 90 ? "#1b7f4c" : s >= 70 ? "#8fbf3f" : s >= 50 ? "#e6b422" : "#c0504d"; }
  function emptyFC() { return { type: "FeatureCollection", features: [] }; }

  function cityPointsFC() {
    if (!index) return emptyFC();
    return {
      type: "FeatureCollection",
      features: index.cities.filter(c => c.bbox).map(c => ({
        type: "Feature",
        geometry: { type: "Point", coordinates: [(c.bbox[0] + c.bbox[2]) / 2, (c.bbox[1] + c.bbox[3]) / 2] },
        properties: { code: c.code, name: c.name, count: c.count },
      })),
    };
  }

  function setCityLayerVisibility() {
    const v = el.city.value ? "none" : "visible";
    ["city-circle", "city-label"].forEach(id => map.getLayer(id) && map.setLayoutProperty(id, "visibility", v));
  }

  map.on("load", async () => {
    try {
      index = await (await fetch("data/index.json", { cache: "no-cache" })).json();
    } catch (e) {
      el.summary.innerHTML = "<b>データがまだない</b><br>pipeline/build_web_data.py を実行して web/data/ を生成する。";
      return;
    }
    for (const c of index.cities) {
      const o = document.createElement("option");
      o.value = c.code; o.textContent = `${c.name}（${c.count}筆 / ${c.area_ha}ha）`;
      el.city.appendChild(o);
    }
    const a = index.attribution || {};
    el.attrib.innerHTML = [a.emaff, a.fude, a.basemap].filter(Boolean).join(" ／ ") + `<br>生成 ${index.generated}`;
    addDataLayers();
    renderSummary();
    const remembered = safeGet("hokichi.city");
    if (remembered && index.cities.some(c => c.code === remembered)) { el.city.value = remembered; loadCity(remembered); }
  });

  map.on("style.load", () => { if (index) { addDataLayers(); if (fc) map.getSource("parcels").setData(filtered()); } });

  // ---------------------------------------------------------------- データ読込・フィルタ
  async function loadCity(code) {
    safeSet("hokichi.city", code);
    activeId = null; el.detail.classList.add("hidden");
    if (!code) { fc = null; refresh(); map.flyTo({ center: NARA_CENTER, zoom: 9.3 }); return; }
    el.summary.innerHTML = "読込中…";
    fc = await (await fetch(`data/${code}.geojson`, { cache: "no-cache" })).json();
    refresh();
    const c = index.cities.find(x => x.code === code);
    if (c && c.bbox) map.fitBounds([[c.bbox[0], c.bbox[1]], [c.bbox[2], c.bbox[3]]], { padding: 40, maxZoom: 14 });
  }

  function filtered() {
    if (!fc) return emptyFC();
    const min = +el.minScore.value, lend = el.lendOnly.checked, hideUnk = el.hideUnknown.checked, lu = el.landuse.value;
    return {
      type: "FeatureCollection",
      features: fc.features.filter(f => {
        const p = f.properties;
        if (p.score < min) return false;
        if (lend && !/貸|機構|中間管理|委託|任せ/.test(p.intent || "")) return false;
        if (hideUnk && /不明|確知できない|不確知|未確知/.test(p.owner_known || "")) return false;
        if (lu && !(p.landuse || "").includes(lu)) return false;
        return true;
      }),
    };
  }

  function refresh() {
    const data = filtered();
    if (map.getSource("parcels")) map.getSource("parcels").setData(data);
    setCityLayerVisibility();
    renderSummary(data);
    renderList(data);
  }

  // ---------------------------------------------------------------- 描画
  function renderSummary(data) {
    if (!el.city.value) {
      const tot = index ? index.cities.reduce((s, c) => s + c.count, 0) : 0;
      el.summary.innerHTML = `県内 <b>${index ? index.cities.length : 0}</b> 市町村 / 遊休農地 <b>${tot}</b> 筆。円をクリックで市町村へ。`;
      el.list.innerHTML = "";
      return;
    }
    const n = data.features.length, ha = data.features.reduce((s, f) => s + (f.properties.area_sqm || 0), 0) / 1e4;
    const cl = new Set(data.features.map(f => f.properties.cluster_id)).size;
    el.summary.innerHTML = `表示 <b>${n}</b> 筆 / <b>${ha.toFixed(1)}</b> ha / 団地候補 <b>${cl}</b>（全 ${fc.features.length} 筆）`;
  }

  function renderList(data) {
    const groups = new Map();
    for (const f of data.features) {
      const k = f.properties.cluster_id;
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(f);
    }
    const ordered = [...groups.entries()].sort((a, b) => Math.max(...b[1].map(f => f.properties.score)) - Math.max(...a[1].map(f => f.properties.score)));
    el.list.innerHTML = "";
    for (const [cid, feats] of ordered) {
      const p0 = feats[0].properties;
      const d = document.createElement("details");
      d.className = "cluster"; d.open = ordered.length <= 6;
      const area = feats.reduce((s, f) => s + (f.properties.area_sqm || 0), 0);
      d.innerHTML = `<summary><span>${esc(p0.place || cid)}</span><small>${feats.length}筆 / ${(area / 100).toFixed(1)}a（団地全体 ${p0.cluster_size}筆）</small></summary>`;
      for (const f of feats.sort((a, b) => b.properties.score - a.properties.score)) {
        const p = f.properties;
        const row = document.createElement("div");
        row.className = "item" + (p.cid === activeId ? " active" : ""); row.dataset.cid = p.cid;
        row.innerHTML = `<span class="score" style="background:${scoreColor(p.score)}">${p.score}</span>
          <span><b>${esc(p.parcel)}</b> <span class="meta">${esc(p.landuse || "-")} ${p.area_sqm ? Math.round(p.area_sqm) + "㎡" : ""}</span><br><span class="meta">${esc(p.intent || "意向未回答")}${p.matched ? "" : " ・形状推定"}</span></span>
          <span class="meta">${inCart(p.cid) ? "✓" : ""}</span>`;
        row.addEventListener("click", () => select(p.cid, true));
        d.appendChild(row);
      }
      el.list.appendChild(d);
    }
  }

  function select(cid, fly) {
    if (activeId && map.getSource("parcels")) map.setFeatureState({ source: "parcels", id: activeId }, { active: false });
    activeId = cid;
    map.setFeatureState({ source: "parcels", id: cid }, { active: true });
    const f = fc.features.find(x => x.properties.cid === cid);
    if (!f) return;
    const p = f.properties;
    if (fly) map.flyTo({ center: [p.lon, p.lat], zoom: Math.max(map.getZoom(), 16) });
    document.querySelectorAll(".item").forEach(x => x.classList.toggle("active", x.dataset.cid === cid));
    const gsi = `https://maps.gsi.go.jp/#17/${p.lat}/${p.lon}/&base=std&ls=std%7Cseamlessphoto&disp=11`;
    const gmap = `https://www.google.com/maps?q=${p.lat},${p.lon}`;
    el.detail.classList.remove("hidden");
    el.detail.innerHTML = `
      <h3><span class="score" style="background:${scoreColor(p.score)}">${p.score}</span> ${esc(p.city)}${esc(p.place)} ${esc(p.parcel)}</h3>
      <dl>
        <dt>地目 / 面積</dt><dd>${esc(p.landuse || "-")} / ${p.area_sqm ? Math.round(p.area_sqm) + "㎡（" + (p.area_sqm / 100).toFixed(1) + "a）" : "-"}</dd>
        <dt>遊休農地</dt><dd>${esc(p.idle || "-")}（調査 ${esc(p.survey_date || "-")}）</dd>
        <dt>所有者の意向</dt><dd>${esc(p.intent || "未回答")}</dd>
        <dt>所有者等の確知</dt><dd>${esc(p.owner_known || "-")}</dd>
        <dt>農振 / 都計</dt><dd>${esc(p.shinko || "-")} / ${esc(p.toshi || "-")}</dd>
        <dt>中間管理権</dt><dd>${esc(p.bank || "-")}</dd>
        <dt>団地</dt><dd>${esc(p.cluster_id)}：${p.cluster_size}筆 / ${(p.cluster_area / 100).toFixed(1)}a</dd>
        <dt>区画形状</dt><dd>${p.matched ? "筆ポリゴンに一致" : "推定（点の周りの正方形）"}</dd>
      </dl>
      <p class="reasons">${(p.reasons || []).map(esc).join(" / ")}</p>
      <div class="row">
        <button class="btn primary" id="addCart">${inCart(p.cid) ? "請求リストから外す" : "登記請求リストに追加"}</button>
        <a class="btn" href="${gsi}" target="_blank" rel="noopener">地理院地図</a>
        <a class="btn" href="${gmap}" target="_blank" rel="noopener">Googleマップ</a>
      </div>`;
    el.detail.scrollTop = 0;
    $("addCart").addEventListener("click", () => { toggleCart(p); select(cid, false); renderList(filtered()); });
  }

  // ---------------------------------------------------------------- 請求リスト（ブラウザ内）
  function loadCart() { try { return JSON.parse(localStorage.getItem(CART_KEY) || "[]"); } catch { return []; } }
  function saveCart() { try { localStorage.setItem(CART_KEY, JSON.stringify(cart)); } catch {} updateCartBadge(); }
  function inCart(cid) { return cart.some(x => x.cid === cid); }
  function toggleCart(p) {
    if (inCart(p.cid)) cart = cart.filter(x => x.cid !== p.cid);
    else cart.push({ cid: p.cid, city_code: p.city_code, city: p.city, place: p.place, parcel: p.parcel, landuse: p.landuse, area_sqm: p.area_sqm, score: p.score, cluster_id: p.cluster_id, lat: p.lat, lon: p.lon, added: new Date().toISOString().slice(0, 10) });
    saveCart();
  }
  function updateCartBadge() { el.cartCount.textContent = cart.length; }
  function renderCart() {
    el.cartTable.innerHTML = "";
    for (const c of cart) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${esc(c.city)}</td><td>${esc(c.place)}</td><td><b>${esc(c.parcel)}</b></td><td>${esc(c.landuse || "")}</td><td>${c.area_sqm ? Math.round(c.area_sqm) : ""}</td><td>${c.score}</td><td><button class="btn danger" data-rm="${c.cid}">×</button></td>`;
      el.cartTable.appendChild(tr);
    }
    el.cartTable.querySelectorAll("[data-rm]").forEach(b => b.addEventListener("click", () => { cart = cart.filter(x => x.cid !== b.dataset.rm); saveCart(); renderCart(); renderList(filtered()); }));
  }
  function cartCsv() {
    const head = ["候補ID", "市区町村コード", "市区町村", "大字・字", "地番", "地目", "面積(㎡)", "点数", "団地ID", "緯度", "経度", "追加日", "請求種別", "請求日", "取得済", "備考"];
    const rows = cart.map(c => [c.cid, c.city_code, c.city, c.place, c.parcel, c.landuse, c.area_sqm ? Math.round(c.area_sqm) : "", c.score, c.cluster_id, c.lat, c.lon, c.added, "全部事項(330円)", "", "", ""]);
    const csv = "﻿" + [head, ...rows].map(r => r.map(v => `"${String(v ?? "").replace(/"/g, '""')}"`).join(",")).join("\r\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    a.download = `parcels_to_query_${new Date().toISOString().slice(0, 10)}.csv`; a.click();
  }
  function cartLetter() {
    const byCity = new Map();
    cart.forEach(c => { if (!byCity.has(c.city)) byCity.set(c.city, []); byCity.get(c.city).push(c); });
    let out = "";
    for (const [city, items] of byCity) {
      out += `${city}農業委員会 御中\n\n農地の借受けに関するご相談（照会事項）\n\n` +
        `貴市町村内の遊休農地をお借りし、無農薬・無化学肥料（自然栽培）による栽培を行いたく、下記の農地についてご教示をお願いいたします。\n\n` +
        `【対象として検討している農地】\n` +
        items.map((c, i) => `${i + 1}. ${c.place} ${c.parcel}番（${c.landuse || "地目不明"}・${c.area_sqm ? Math.round(c.area_sqm) + "㎡" : "面積不明"}）`).join("\n") +
        `\n\n【ご教示いただきたい事項】\n` +
        `1. 上記農地の利用状況調査（農地法第30条）における判定と判定年月日\n` +
        `2. 利用意向調査に対する所有者の回答内容\n` +
        `3. 農業委員会から所有者へ借受けの打診をしていただくことの可否\n` +
        `4. 相続未登記・所有者等を確知できない農地の有無と、農地法第41条に基づく探索・公示・裁定手続の可否\n` +
        `5. 地域計画（目標地図）における位置づけと、当社が担い手として位置づけられる余地\n` +
        `6. 事前にご挨拶すべき農会長・水利組合長等\n\n` +
        `株式会社悠三堂\n連絡先：\n\n----\n\n`;
    }
    el.letterOut.textContent = out;
    el.letterOut.classList.remove("hidden");
    navigator.clipboard?.writeText(out).catch(() => {});
  }

  // ---------------------------------------------------------------- イベント
  el.city.addEventListener("change", () => loadCity(el.city.value));
  el.minScore.addEventListener("input", () => { el.minScoreOut.textContent = el.minScore.value; refresh(); });
  [el.lendOnly, el.hideUnknown, el.landuse].forEach(x => x.addEventListener("change", refresh));
  el.basemap.addEventListener("change", () => map.setStyle(styleFor(el.basemap.value)));
  el.cartBtn.addEventListener("click", () => { renderCart(); el.letterOut.classList.add("hidden"); el.cart.classList.remove("hidden"); });
  el.cartClose.addEventListener("click", () => el.cart.classList.add("hidden"));
  el.cart.addEventListener("click", (e) => { if (e.target === el.cart) el.cart.classList.add("hidden"); });
  el.cartCsv.addEventListener("click", cartCsv);
  el.cartLetter.addEventListener("click", cartLetter);
  el.cartClear.addEventListener("click", () => { if (confirm("請求リストを全て消しますか？")) { cart = []; saveCart(); renderCart(); renderList(filtered()); } });
  updateCartBadge();

  function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  function safeGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
  function safeSet(k, v) { try { localStorage.setItem(k, v); } catch {} }
})();
