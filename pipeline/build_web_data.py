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


PREFS = ["北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県", "茨城県", "栃木県", "群馬県",
         "埼玉県", "千葉県", "東京都", "神奈川県", "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県",
         "岐阜県", "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県",
         "鳥取県", "島根県", "岡山県", "広島県", "山口県", "徳島県", "香川県", "愛媛県", "高知県", "福岡県",
         "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県"]


def emaff_geojson_rows(path):
    """eMAFF農地ナビの「ピン情報」GeoJSON を、CSVと同じ列名の行に変換する。

    公表情報のキーは英語（ShikuchosonCode, Tiban, UsageSituationInvestigationResult …）。
    コード値で判定できるものはコードを使い、名称の揺れに依存しない。
    """
    with open(path, encoding="utf-8-sig") as fh:
        fc = json.load(fh)
    rows = []
    for f in fc.get("features", []):
        pr = f.get("properties") or {}
        g = f.get("geometry") or {}
        lon = lat = None
        if g.get("type") == "Point":
            lon, lat = g["coordinates"][:2]
        elif g.get("coordinates"):
            c = g["coordinates"]
            while isinstance(c, list) and c and isinstance(c[0], list):
                c = c[0]
            lon, lat = c[:2]
        code = norm_code(pr.get("ShikuchosonCode", ""))
        city = (pr.get("FarmCommitteeName") or "").replace("農業委員会", "") or NARA.get(code, "")
        addr = pr.get("Address") or ""
        for pref in PREFS:                       # 「奈良県奈良市誓多林町1005-1」→「誓多林町1005-1」
            if addr.startswith(pref):
                addr = addr[len(pref):]
                break
        if city and addr.startswith(city):
            addr = addr[len(city):]
        usage = str(pr.get("UsageSituationInvestigationResult", ""))
        idle = pr.get("UsageSituationInvestigationResultCodeName", "") if usage in ("1", "2") else ""
        bank = pr.get("RightSettingContentsCodeName", "") if str(pr.get("RightSettingContents", "0")) != "0" else ""
        right = pr.get("KindOfRightCodeName", "") if str(pr.get("KindOfRight", "0")) != "0" else ""
        owner_intent = pr.get("OwnerFarmIntentionCodeName", "")
        if owner_intent in ("非公表", "未回答", "調査対象外"):
            owner_intent = ""
        idle_intent = pr.get("OwnerStatementIntentSurveyResultsCodeName", "")
        if idle_intent in ("調査対象外",):
            idle_intent = ""
        rows.append({
            "市区町村コード": code, "市区町村名": city,
            "所在・地番": addr, "地番": pr.get("Tiban", ""),
            "地目": pr.get("ClassificationOfLandCodeName", ""),
            "面積": pr.get("AreaOnRegistry", ""),
            "農振法区分": pr.get("SectionOfNoushinhouCodeName", ""),
            "都市計画法区分": pr.get("SectionOfToshikeikakuhouCodeName", ""),
            "遊休農地": idle,
            "利用状況調査日": pr.get("UsageSituationInvestigationDate", ""),
            "所有者の農地に関する意向": owner_intent,
            "遊休農地の所有者等の意向": idle_intent,
            "利用意向調査日": pr.get("UseIntentionInvestigationDate", ""),
            "所有者等の確知の状況": pr.get("UseIntentionAscertainmentResultCodeName", ""),
            "農地中間管理権の状況": bank,
            "権利の種類": right,
            "所有者ハッシュ": pr.get("FarmerIndicationNumberHash", ""),
            "台帳ID": pr.get("DaichoId", ""),
            "緯度": lat, "経度": lon,
        })
    return rows


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
    """指定市区町村の筆ポリゴンを (STRtree, geoms, props) で返す。無ければ None。

    配布形式によって市区町村を示す列が違う:
      - 集落境界DB版 (MB0001_*.fgb): key = 都道府県2桁+市区町村3桁+集落コード
      - 筆ポリゴン公開サイト版: local_government_cd
    """
    if not HAVE_GEO:
        return None
    paths = sorted(glob.glob(os.path.join(fude_dir, "*.fgb")) +
                   glob.glob(os.path.join(fude_dir, "*.geojson")) +
                   glob.glob(os.path.join(fude_dir, "*.json")))
    if not paths:
        return None
    geoms, props = [], []
    for path in paths:
        try:
            names = list(pyogrio.read_info(path)["fields"])
        except Exception as exc:
            print(f"  読めない: {path} ({exc})", file=sys.stderr)
            continue
        code_col = "key" if "key" in names else ("local_government_cd" if "local_government_cd" in names else None)
        wkb, fields = None, None
        if code_col:
            try:
                meta, _idx, wkb, fields = pyogrio.raw.read(path, where=f"{code_col} LIKE '{city_code}%'")
            except Exception as exc:
                print(f"  where句なしで再読込 ({exc.__class__.__name__}): {path}", file=sys.stderr)
        if wkb is None:
            meta, _idx, wkb, fields = pyogrio.raw.read(path)
        names = list(meta["fields"])
        cols = {n: fields[i] for i, n in enumerate(names)}
        if code_col:
            mask = np.array([str(c).startswith(city_code) for c in cols[code_col]])
        else:
            mask = np.ones(len(wkb), dtype=bool)
        if not mask.any():
            continue
        g = from_wkb(np.asarray(wkb, dtype=object)[mask])
        geoms.extend(g.tolist())
        sel = np.nonzero(mask)[0]
        for j in sel:
            props.append({
                "fude_uuid": str(cols.get("polygon_uuid", [""] * len(wkb))[j]),
                "land_type": str(cols.get("land_type", [""] * len(wkb))[j]),
                "issue_year": str(cols.get("issue_year", [""] * len(wkb))[j]),
            })
    if not geoms:
        return None
    print(f"  筆ポリゴン {len(geoms)} 区画", file=sys.stderr)
    return STRtree(geoms), geoms, props


NEAR_M = 20.0   # ピンがこの距離以内なら「近傍一致」とみなす（ピンは登記簿由来の概ねの位置）


def match_polygon(fude, lat, lon):
    """(geom, props, kind, dist_m) を返す。kind: 'within' / 'near' / None。"""
    if fude is None:
        return None
    tree, geoms, props = fude
    pt = Point(lon, lat)
    hits = tree.query(pt, predicate="within")
    if len(hits):
        j = int(hits[0])
        return geoms[j], props[j], "within", 0.0
    j = int(tree.nearest(pt))
    dx = (geoms[j].distance(pt)) * 111_320.0 * math.cos(math.radians(lat))
    dy = (geoms[j].distance(pt)) * 111_320.0
    dist = min(dx, dy) if dx and dy else max(dx, dy)
    if dist <= NEAR_M:
        return geoms[j], props[j], "near", round(dist, 1)
    return None


def round_coords(obj, nd=6):
    if isinstance(obj, (list, tuple)):
        if obj and isinstance(obj[0], (int, float)):
            return [round(float(v), nd) for v in obj]
        return [round_coords(o, nd) for o in obj]
    return obj


def write_fude_context(fude, plist, path):
    """ピンの範囲（＋約1km）にある筆ポリゴンを薄い背景レイヤ用に書き出す。"""
    if fude is None or not plist:
        return 0
    tree, geoms, props = fude
    lats = [p.lat for p in plist]; lons = [p.lon for p in plist]
    m = 0.01
    from shapely.geometry import box
    hits = tree.query(box(min(lons) - m, min(lats) - m, max(lons) + m, max(lats) + m), predicate="intersects")
    feats = []
    for j in hits:
        j = int(j)
        geo = mapping(geoms[j])
        geo["coordinates"] = round_coords(geo["coordinates"])
        feats.append({"type": "Feature", "geometry": geo,
                      "properties": {"u": props[j]["fude_uuid"][:8], "t": props[j]["land_type"]}})
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": feats}, fh, ensure_ascii=False, separators=(",", ":"))
    return len(feats)


def feature_for(p, match):
    if match is not None:
        geom_obj, fprops, kind, dist = match
        geometry = mapping(geom_obj)
        geometry["coordinates"] = round_coords(geometry["coordinates"])
        matched = True
    else:
        geometry = square_around(p.lat, p.lon, p.area_sqm)
        fprops, kind, dist = {}, "none", None
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
            "owner_hash": p.owner_hash, "owner_group": p.owner_group,
            "daicho_id": p.daicho_id,
            "matched": matched, "match_kind": kind, "match_dist_m": dist,
            "fude_uuid": fprops.get("fude_uuid", ""),
            "land_type": fprops.get("land_type", ""),
        },
    }


def bbox_of(features):
    xs, ys = [], []

    def walk(c):
        if c and isinstance(c[0], (int, float)):
            xs.append(c[0]); ys.append(c[1])
        else:
            for o in c:
                walk(o)

    for f in features:
        walk(f["geometry"]["coordinates"])
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
    inputs = sorted(glob.glob(os.path.join(a.emaff, "*.csv")) +
                    glob.glob(os.path.join(a.emaff, "*.geojson")) +
                    glob.glob(os.path.join(a.emaff, "*.json")))
    if not inputs:
        raise SystemExit(f"{a.emaff}/ にCSV/GeoJSONがない。eMAFF農地ナビからダウンロードして置くこと。")

    rows = []
    for path in inputs:
        if path.lower().endswith((".geojson", ".json")):
            part = emaff_geojson_rows(path)
            print(f"読込: {path} (農地ナビGeoJSON, {len(part)}筆)", file=sys.stderr)
        else:
            part, enc = hokichi.read_table(path)
            print(f"読込: {path} ({enc}, {len(part)}行)", file=sys.stderr)
        rows.extend(part)
    if not rows:
        raise SystemExit("読み込めた筆が0件だった。")
    headers = []
    for r in rows:                                # 入力ごとに列が違っても拾えるよう和集合を取る
        for k in r:
            if k not in headers:
                headers.append(k)
    mapping = hokichi.guess_mapping(headers, override)
    for need in ("location", "lat", "lon"):
        if need not in mapping:
            raise SystemExit(f"列 '{need}' を認識できない。tools/hokichi.py inspect で確認し --map で指定すること。")

    idle_words = cfg["vocab"]["idle"]

    def keep(p):
        if p.lat is None or p.lon is None:
            return False
        return a.all or hokichi.has_any(p.idle, idle_words)

    parcels = hokichi.process_rows(rows, mapping, cfg, keep=keep)
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
            match = match_polygon(fude, p.lat, p.lon)
            if match is not None:
                matched += 1
            features.append(feature_for(p, match))
        n_ctx = write_fude_context(fude, plist, os.path.join(a.out, f"{code}-fude.geojson"))
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
            "fude_context": n_ctx,
        })
        print(f"  {code} {name}: {len(plist)}筆 (ポリゴン一致 {matched} / 背景の区画 {n_ctx})", file=sys.stderr)

    meta = {
        "generated": dt.date.today().isoformat(),
        "fude_used": any(c["matched"] for c in index),
        "attribution": {k: v.format(year=a.fude_year) for k, v in ATTRIBUTION.items()},
        "note": "所有者の氏名は含まれない（農地台帳の公表事項に氏名は無い）。地番から登記で確認すること。"
                " 農地ナビのピンは登記簿由来の概ねの位置で、筆ポリゴン（衛星判読）とは数十m ずれることが多く、"
                "荒廃した筆は筆ポリゴンに存在しないことがある。区画形状は参考。",
        "cities": index,
    }
    with open(os.path.join(a.out, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1)
    print(f"出力: {a.out}/index.json ほか {len(index)} 市区町村", file=sys.stderr)


if __name__ == "__main__":
    main()
