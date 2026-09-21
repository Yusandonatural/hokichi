#!/usr/bin/env python3
"""eMAFF農地ナビのCSV（遊休農地ピン）と筆ポリゴンを結合し、地図UI用のデータを作る。

入力
  data/emaff/*.csv        eMAFF農地ナビからダウンロードした公表情報（緯度・経度つき）
  data/fude/*.fgb         pipeline/fetch_fude.py が置いた筆ポリゴン（任意。無ければ点を四角で描く）
出力
  web/data/<市区町村コード>.geojson   遊休農地（採点済み）。ポリゴンに乗ったものは区画形状、乗らなければ点周りの正方形
  web/data/index.json                 市区町村の一覧・件数・範囲・生成日・出典

    python3 pipeline/build_web_data.py --emaff data/emaff --fude data/fude --out web/data
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import hokichi  # noqa: E402

try:
    import numpy as np
    import pyogrio
    from shapely import STRtree, from_wkb
    from shapely.geometry import Point, mapping
    HAVE_GEO = True
except ImportError:  # 筆ポリゴンを使わない最小構成でも動かす
    HAVE_GEO = False

# 奈良県の市区町村コード（eMAFF CSVに市区町村名が無い場合の補完用）
NARA = {
    "29201": "奈良市", "29202": "大和高田市", "29203": "大和郡山市", "29204": "天理市",
    "29205": "橿原市", "29206": "桜井市", "29207": "五條市", "29208": "御所市",
    "29209": "生駒市", "29210": "香芝市", "29211": "葛城市", "29212": "宇陀市",
    "29322": "山添村", "29342": "平群町", "29343": "三郷町", "29344": "斑鳩町",
    "29345": "安堵町", "29361": "川西町", "29362": "三宅町", "29363": "田原本町",
    "29385": "曽爾村", "29386": "御杖村", "29401": "高取町", "29402": "明日香村",
    "29424": "上牧町", "29425": "王寺町", "29426": "広陵町", "29427": "河合町",
    "29441": "吉野町", "29442": "大淀町", "29443": "下市町", "29444": "黒滝村",
    "29446": "天川村", "29447": "野迫川村", "29449": "十津川村", "29450": "下北山村",
    "29451": "上北山村", "29452": "川上村", "29453": "東吉野村",
}

ATTRIBUTION = {
    "emaff": "eMAFF農地ナビ（農林水産省）公表情報を加工",
    "fude": "「筆ポリゴンデータ（{year}年度公開）」（農林水産省）を加工",
    "basemap": "地理院タイル（国土地理院）",
}


def norm_code(code):
    """'29212' / '292122'(検査数字付き) / 29212.0 を 5桁に揃える。"""
    s = str(code or "").strip().split(".")[0]
    return s[:5] if len(s) >= 5 else s.zfill(5) if s else ""


def square_around(lat, lon, area_sqm):
    """ポリゴンに乗らなかった点の周りに、面積相当の正方形を描く（表示用の近似）。"""
    side = math.sqrt(max(area_sqm or 500.0, 100.0))
    dlat = side / 2 / 111_320.0
    dlon = side / 2 / (111_320.0 * math.cos(math.radians(lat)))
    return {"type": "Polygon", "coordinates": [[
        [lon - dlon, lat - dlat], [lon + dlon, lat - dlat],
        [lon + dlon, lat + dlat], [lon - dlon, lat + dlat], [lon - dlon, lat - dlat]]]}


def load_fude(fude_dir, city_code):
    """指定市区町村の筆ポリゴンを (STRtree, geoms, props) で返す。無ければ None。"""
    if not HAVE_GEO:
        return None
    paths = sorted(glob.glob(os.path.join(fude_dir, "*.fgb")) +
                   glob.glob(os.path.join(fude_dir, "*.geojson")))
    if not paths:
        return None
    geoms, props = [], []
    for path in paths:
        try:
            meta, _idx, wkb, fields = pyogrio.raw.read(
                path, where=f"local_government_cd LIKE '{city_code}%'")
        except Exception as exc:  # 属性フィルタ非対応の形式は全件読んで絞る
            print(f"  where句なしで再読込 ({exc.__class__.__name__}): {path}", file=sys.stderr)
            meta, _idx, wkb, fields = pyogrio.raw.read(path)
        names = list(meta["fields"])
        cols = {n: fields[i] for i, n in enumerate(names)}
        if "local_government_cd" in cols:
            mask = np.array([norm_code(c) == city_code for c in cols["local_government_cd"]])
        else:
            mask = np.ones(len(wkb), dtype=bool)
        if not mask.any():
            continue
        g = from_wkb(np.asarray(wkb, dtype=object)[mask])
        geoms.extend(g.tolist())
        for j in np.nonzero(mask)[0]:
            props.append({
                "fude_uuid": str(cols.get("polygon_uuid", [""] * len(wkb))[j]),
                "land_type": str(cols.get("land_type", [""] * len(wkb))[j]),
                "issue_year": str(cols.get("issue_year", [""] * len(wkb))[j]),
            })
    if not geoms:
        return None
    return STRtree(geoms), geoms, props


def feature_for(p, match):
    if match is not None:
        geom_obj, fprops = match
        geometry = mapping(geom_obj)
        matched = True
    else:
        geometry = square_around(p.lat, p.lon, p.area_sqm)
        fprops = {}
        matched = False
    return {
        "type": "Feature",
        "id": p.cid,
        "geometry": geometry,
        "properties": {
            "cid": p.cid, "score": p.score,
            "city_code": p.city_code, "city": p.city, "place": p.place,
            "parcel": p.parcel_label, "landuse": p.landuse,
            "area_sqm": round(p.area_sqm, 1) if p.area_sqm is not None else None,
            "shinko": p.shinko, "toshi": p.toshi, "idle": p.idle,
            "survey_date": p.survey_date,
            "intent": p.idle_intent or p.owner_intent,
            "owner_known": p.owner_known, "bank": p.bank,
            "cluster_id": p.cluster_id, "cluster_size": p.cluster_size,
            "cluster_area": p.cluster_area,
            "reasons": p.reasons,
            "lat": p.lat, "lon": p.lon,
            "matched": matched,
            "fude_uuid": fprops.get("fude_uuid", ""),
            "land_type": fprops.get("land_type", ""),
        },
    }


def bbox_of(features):
    xs, ys = [], []
    for f in features:
        for ring in f["geometry"]["coordinates"]:
            for x, y in ring:
                xs.append(x); ys.append(y)
    return [min(xs), min(ys), max(xs), max(ys)] if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--emaff", default="data/emaff")
    ap.add_argument("--fude", default="data/fude")
    ap.add_argument("--config", default="data/config.json")
    ap.add_argument("--map", default=None, help="列対応表(JSON)")
    ap.add_argument("--out", default="web/data")
    ap.add_argument("--all", action="store_true",
                    help="遊休農地以外も出力する（既定は遊休/荒廃と判定された筆のみ）")
    ap.add_argument("--fude-year", default="2025")
    a = ap.parse_args()

    cfg = hokichi.load_config(a.config if os.path.exists(a.config) else None)
    override = hokichi.load_override(a.map)
    csvs = sorted(glob.glob(os.path.join(a.emaff, "*.csv")))
    if not csvs:
        raise SystemExit(f"{a.emaff}/ にCSVがない。eMAFF農地ナビからダウンロードして置くこと。")

    rows, mapping = [], {}
    for path in csvs:
        part, enc = hokichi.read_table(path)
        print(f"読込: {path} ({enc}, {len(part)}行)", file=sys.stderr)
        if not mapping:
            mapping = hokichi.guess_mapping(list(part[0].keys()), override)
        rows.extend(part)
    for need in ("location", "lat", "lon"):
        if need not in mapping:
            raise SystemExit(f"列 '{need}' を認識できない。tools/hokichi.py inspect で確認し --map で指定すること。")

    parcels = hokichi.process_rows(rows, mapping, cfg)
    idle_words = cfg["vocab"]["idle"]
    if not a.all:
        parcels = [p for p in parcels if hokichi.has_any(p.idle, idle_words)]
    parcels = [p for p in parcels if p.lat is not None and p.lon is not None]
    print(f"対象 {len(parcels)} 筆", file=sys.stderr)

    by_city = {}
    for p in parcels:
        p.city_code = norm_code(p.city_code)
        by_city.setdefault(p.city_code, []).append(p)

    os.makedirs(a.out, exist_ok=True)
    index = []
    for code, plist in sorted(by_city.items()):
        name = next((p.city for p in plist if p.city), "") or NARA.get(code, code)
        fude = load_fude(a.fude, code)
        matched = 0
        features = []
        for p in plist:
            match = None
            if fude is not None:
                tree, geoms, props = fude
                hits = tree.query(Point(p.lon, p.lat), predicate="within")
                if len(hits):
                    j = int(hits[0])
                    match = (geoms[j], props[j])
                    matched += 1
            features.append(feature_for(p, match))
        fc = {"type": "FeatureCollection",
              "name": f"{name} 遊休農地", "features": features}
        with open(os.path.join(a.out, f"{code}.geojson"), "w", encoding="utf-8") as fh:
            json.dump(fc, fh, ensure_ascii=False, separators=(",", ":"))
        clusters = {p.cluster_id for p in plist}
        index.append({
            "code": code, "name": name, "count": len(plist),
            "matched": matched, "clusters": len(clusters),
            "area_ha": round(sum(p.area_sqm or 0 for p in plist) / 10_000, 1),
            "bbox": bbox_of(features),
            "max_score": max(p.score for p in plist),
        })
        print(f"  {code} {name}: {len(plist)}筆 (ポリゴン一致 {matched})", file=sys.stderr)

    meta = {
        "generated": dt.date.today().isoformat(),
        "fude_used": any(c["matched"] for c in index),
        "attribution": {k: v.format(year=a.fude_year) for k, v in ATTRIBUTION.items()},
        "note": "所有者の氏名は含まれない（農地台帳の公表事項に氏名は無い）。地番から登記で確認すること。",
        "cities": index,
    }
    with open(os.path.join(a.out, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1)
    print(f"出力: {a.out}/index.json ほか {len(index)} 市区町村", file=sys.stderr)


if __name__ == "__main__":
    main()
