#!/usr/bin/env python3
"""eMAFF農地ナビの公表データ(CSV)から、自然栽培に向く耕作放棄地の候補を絞り込む。

Python標準ライブラリのみで動く。依存パッケージなし。

    python3 tools/hokichi.py inspect <csv>              列名の自動対応づけを確認する
    python3 tools/hokichi.py rank <csv>... --out out    候補を採点して一式を出力する
    python3 tools/hokichi.py parcels <candidates.csv>   登記請求用の地番リストだけ作る

出力するのは所在・地番までで、所有者の氏名は扱わない（公表データに含まれない）。
氏名は登記事項証明書でしか取得できない。docs/03-地主を特定する.md を参照。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field, asdict

# ---------------------------------------------------------------- 列の対応づけ

# 正規化後のフィールド名 -> ヘッダに含まれうる語（先に書いたものほど優先）
COLUMN_HINTS = [
    ("city_code",   ["市区町村コード", "市町村コード", "団体コード"]),
    ("city",        ["市区町村名", "市区町村", "市町村名"]),
    # 「遊休農地の所有者等の意向」を「所有者の農地に関する意向」より先に取る
    ("idle_intent", ["遊休農地の所有者等の意向", "遊休農地の所有者の意向", "遊休農地所有者意向"]),
    ("intent_date", ["利用意向調査日", "利用意向調査"]),
    ("owner_intent",["所有者の農地に関する意向", "所有者の意向", "農地に関する意向"]),
    ("owner_known", ["所有者等の確知", "所有者の確知", "確知の状況"]),
    ("idle",        ["遊休農地", "荒廃農地", "低利用"]),
    ("survey_date", ["利用状況調査日", "利用状況調査"]),
    ("location",    ["所在・地番", "所在地番", "所在", "大字", "字名"]),
    ("parcel",      ["地番"]),
    ("landuse",     ["現況地目", "登記地目", "地目"]),
    ("area",        ["面積", "地積"]),
    ("shinko",      ["農振法区分", "農振法", "農業振興地域", "農用地区域"]),
    ("toshi",       ["都市計画法区分", "都市計画法", "都市計画"]),
    ("bank",        ["農地中間管理権", "中間管理"]),
    ("right_type",  ["権利の種類", "権利種類"]),
    ("right_start", ["存続期間始期", "始期"]),
]

ENCODINGS = ["utf-8-sig", "cp932", "utf-8", "euc_jp"]


def read_table(path):
    """文字コードを総当たりでCSVを読む。行は dict のリストで返す。"""
    last = None
    for enc in ENCODINGS:
        try:
            with open(path, encoding=enc, newline="") as fh:
                rows = list(csv.DictReader(fh))
            if rows and any(k for k in rows[0]):
                return rows, enc
        except (UnicodeDecodeError, LookupError) as exc:
            last = exc
            continue
    raise SystemExit(f"CSVを読めなかった: {path} ({last})")


def guess_mapping(headers, override=None):
    """ヘッダ名から正規化フィールドへの対応表を推定する。"""
    mapping, used = {}, set()
    norm = {h: unicodedata.normalize("NFKC", (h or "")).strip() for h in headers}
    for field_name, hints in COLUMN_HINTS:
        if override and field_name in override:
            mapping[field_name] = override[field_name]
            used.add(override[field_name])
            continue
        for hint in hints:
            hit = next((h for h in headers
                        if h not in used and hint in norm[h]), None)
            if hit:
                mapping[field_name] = hit
                used.add(hit)
                break
    if override:
        mapping.update({k: v for k, v in override.items() if v})
    return mapping


# ------------------------------------------------------------ 値のパース

ZEN2HAN = str.maketrans("０１２３４５６７８９", "0123456789")
PARCEL_RE = re.compile(r"(\d+)(?:\s*(?:番地|番|-|‐|ー|の)\s*(\d+))?")


def to_float(value):
    if value is None:
        return None
    s = unicodedata.normalize("NFKC", str(value)).replace(",", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def split_location(location, parcel=None):
    """「大字A字B 123番4」を (大字字, 本番, 枝番, 表示用地番) に分解する。

    地番列が別にある場合はそちらを優先して使う。
    """
    loc = unicodedata.normalize("NFKC", (location or "")).translate(ZEN2HAN).strip()
    src = unicodedata.normalize("NFKC", (parcel or "")).translate(ZEN2HAN).strip() or loc
    m = PARCEL_RE.search(src)
    main = int(m.group(1)) if m else None
    branch = int(m.group(2)) if (m and m.group(2)) else None
    # 地名部分は常に所在文字列から取る（地番列の有無にかかわらず数字の手前まで）
    loc_m = PARCEL_RE.search(loc)
    name = loc[: loc_m.start()] if loc_m else loc
    name = re.sub(r"[\s　]+", "", name)
    if main is None:
        label = src
    elif branch is None:
        label = f"{main}"
    else:
        label = f"{main}-{branch}"
    return name, main, branch, label


def has_any(value, words):
    if not value:
        return False
    s = unicodedata.normalize("NFKC", str(value))
    return any(w in s for w in words)


# ------------------------------------------------------------ 設定と採点

DEFAULT_CONFIG = {
    "target": {"city_codes": [], "note": "空なら全件を対象にする"},
    "cluster_gap": 5,           # 本番がこの差以内なら同じ団地とみなす
    "area": {"unit": "sqm", "ideal_min": 1000, "ideal_max": 5000,
             "hard_min": 300, "hard_max": 30000},
    "weights": {
        "idle": 25,             # 遊休農地・荒廃農地と判定済み
        "intent_lend": 30,      # 貸したい／機構に預けたい
        "intent_self": -25,     # 自ら耕作する
        "in_nouyouchi": 10,     # 農用地区域内（青地）
        "area_fit": 15,         # 面積が扱いやすい範囲
        "landuse_hata": 10,     # 畑・樹園地（茶は畑地）
        "landuse_ta": -5,       # 田（排水工事が要る）
        "cluster": 25,          # 団地性（隣接する候補のまとまり）
        "bank_held": 12,        # 既に農地中間管理権が設定されている
        "owner_unknown": -8,    # 所有者不明（裁定ルートで可能だが長期化）
        "urban": -15,           # 市街化区域（転用圧力が高く長期利用に向かない）
    },
    "vocab": {
        "idle": ["遊休", "荒廃", "低利用", "1号", "2号", "１号", "２号", "有"],
        "intent_lend": ["貸付", "貸し付", "貸したい", "機構", "中間管理", "任せ", "委託"],
        "intent_self": ["自ら耕作", "自己耕作", "自作", "継続"],
        "nouyouchi": ["農用地区域", "農用地", "青地"],
        "urban": ["市街化区域"],
        "hata": ["畑", "樹園地", "茶"],
        "ta": ["田"],
        "bank_held": ["有", "設定", "あり"],
        "owner_unknown": ["不明", "不確知", "未確知", "確知できない"],
    },
}


def load_config(path=None):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path:
        with open(path, encoding="utf-8") as fh:
            user = json.load(fh)
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    return cfg


@dataclass
class Parcel:
    cid: str = ""
    city_code: str = ""
    city: str = ""
    place: str = ""          # 大字・字
    main: int | None = None  # 地番の本番
    branch: int | None = None
    parcel_label: str = ""
    landuse: str = ""
    area_sqm: float | None = None
    shinko: str = ""
    toshi: str = ""
    idle: str = ""
    survey_date: str = ""
    owner_intent: str = ""
    idle_intent: str = ""
    intent_date: str = ""
    owner_known: str = ""
    bank: str = ""
    right_type: str = ""
    cluster_id: str = ""
    cluster_size: int = 1
    cluster_area: float = 0.0
    score: int = 0
    reasons: list = field(default_factory=list)


def build_parcels(rows, mapping, cfg):
    out = []
    unit = cfg["area"].get("unit", "sqm")
    for row in rows:
        def get(fieldname):
            col = mapping.get(fieldname)
            return (row.get(col) or "").strip() if col else ""

        place, main, branch, label = split_location(get("location"), get("parcel"))
        area = to_float(get("area"))
        if area is not None and unit == "a":
            area *= 100.0
        p = Parcel(
            city_code=get("city_code"), city=get("city"), place=place,
            main=main, branch=branch, parcel_label=label,
            landuse=get("landuse"), area_sqm=area,
            shinko=get("shinko"), toshi=get("toshi"), idle=get("idle"),
            survey_date=get("survey_date"), owner_intent=get("owner_intent"),
            idle_intent=get("idle_intent"), intent_date=get("intent_date"),
            owner_known=get("owner_known"), bank=get("bank"),
            right_type=get("right_type"),
        )
        codes = cfg["target"].get("city_codes") or []
        if codes and p.city_code and p.city_code not in [str(c) for c in codes]:
            continue
        out.append(p)
    return out


def cluster(parcels, gap):
    """同じ大字・字の中で、本番が近いものを1つの団地としてまとめる。

    公図を見るまで隣接は確定しないので、あくまで現地調査の優先順位付け用。
    """
    groups = defaultdict(list)
    for p in parcels:
        groups[(p.city_code, p.city, p.place)].append(p)
    for key, members in groups.items():
        numbered = sorted([m for m in members if m.main is not None],
                          key=lambda m: (m.main, m.branch or 0))
        unnumbered = [m for m in members if m.main is None]
        idx, prev = 0, None
        for m in numbered:
            if prev is not None and m.main - prev > gap:
                idx += 1
            prev = m.main
            m.cluster_id = f"{key[2] or key[1] or key[0]}-{idx:02d}"
        for n, m in enumerate(unnumbered):
            m.cluster_id = f"{key[2] or key[1] or key[0]}-x{n:02d}"
    sizes, areas = defaultdict(int), defaultdict(float)
    for p in parcels:
        sizes[p.cluster_id] += 1
        areas[p.cluster_id] += p.area_sqm or 0.0
    for p in parcels:
        p.cluster_size = sizes[p.cluster_id]
        p.cluster_area = round(areas[p.cluster_id], 1)
    return parcels


def score(parcels, cfg):
    w, v = cfg["weights"], cfg["vocab"]
    a = cfg["area"]
    for p in parcels:
        s, why = 0, []
        intent = " ".join([p.idle_intent, p.owner_intent])

        if has_any(p.idle, v["idle"]):
            s += w["idle"]; why.append(f"遊休/荒廃と判定({p.idle})")
        if has_any(intent, v["intent_lend"]):
            s += w["intent_lend"]; why.append("所有者に貸付意向あり")
        elif has_any(intent, v["intent_self"]):
            s += w["intent_self"]; why.append("所有者は自ら耕作の意向")
        if has_any(p.shinko, v["nouyouchi"]):
            s += w["in_nouyouchi"]; why.append("農用地区域内")
        if has_any(p.toshi, v["urban"]):
            s += w["urban"]; why.append("市街化区域")
        if p.area_sqm is not None:
            if a["ideal_min"] <= p.area_sqm <= a["ideal_max"]:
                s += w["area_fit"]; why.append(f"面積が扱いやすい({p.area_sqm:.0f}㎡)")
            elif p.area_sqm < a["hard_min"] or p.area_sqm > a["hard_max"]:
                s -= w["area_fit"]; why.append(f"面積が範囲外({p.area_sqm:.0f}㎡)")
        if has_any(p.landuse, v["hata"]):
            s += w["landuse_hata"]; why.append(f"地目{p.landuse}")
        elif has_any(p.landuse, v["ta"]):
            s += w["landuse_ta"]; why.append("地目田(排水対策が要る)")
        if p.cluster_size >= 3:
            bonus = min(w["cluster"], int(w["cluster"] * p.cluster_size / 6))
            s += bonus
            why.append(f"団地候補 {p.cluster_size}筆/{p.cluster_area:.0f}㎡")
        if p.bank and has_any(p.bank, v["bank_held"]):
            s += w["bank_held"]; why.append("農地中間管理権あり")
        if has_any(p.owner_known, v["owner_unknown"]):
            s += w["owner_unknown"]; why.append("所有者不明(裁定ルート要検討)")
        p.score, p.reasons = s, why
    parcels.sort(key=lambda x: (-x.score, x.cluster_id, x.main or 0, x.branch or 0))
    for n, p in enumerate(parcels, 1):
        p.cid = f"C{n:04d}"
    return parcels


# ------------------------------------------------------------ 出力

CAND_COLUMNS = ["cid", "score", "city_code", "city", "place", "parcel_label",
                "landuse", "area_sqm", "shinko", "toshi", "idle", "survey_date",
                "owner_intent", "idle_intent", "intent_date", "owner_known",
                "bank", "right_type", "cluster_id", "cluster_size",
                "cluster_area", "reasons"]


def write_candidates(parcels, path):
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        wtr = csv.writer(fh)
        wtr.writerow(CAND_COLUMNS)
        for p in parcels:
            d = asdict(p)
            d["reasons"] = " / ".join(p.reasons)
            wtr.writerow([d.get(c, "") for c in CAND_COLUMNS])


def write_parcels_to_query(parcels, path):
    """登記情報提供サービス／法務局に請求するための地番リスト。氏名欄は空のまま。"""
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        wtr = csv.writer(fh)
        wtr.writerow(["候補ID", "市区町村コード", "市区町村", "大字・字", "地番",
                      "地目", "面積(㎡)", "団地ID", "請求種別", "請求日",
                      "取得済", "備考"])
        for p in parcels:
            wtr.writerow([p.cid, p.city_code, p.city, p.place, p.parcel_label,
                          p.landuse, f"{p.area_sqm:.0f}" if p.area_sqm else "",
                          p.cluster_id, "全部事項(330円)", "", "",
                          " / ".join(p.reasons)])


def write_shortlist(parcels, path, cfg, top):
    shown = parcels[:top]
    lines = ["# 候補地ショートリスト", "",
             f"上位 {len(shown)} 件 / 全 {len(parcels)} 筆。"
             " 点数は機械的な目安にすぎない。撤退基準は docs/00-全体フロー.md を見ること。",
             "",
             "**このリストに所有者の氏名は載らない。**"
             " 氏名は `parcels_to_query.csv` の地番で登記を請求して得る"
             "（docs/03-地主を特定する.md）。", ""]
    by_cluster = defaultdict(list)
    for p in shown:
        by_cluster[p.cluster_id].append(p)
    order = sorted(by_cluster.items(),
                   key=lambda kv: -max(x.score for x in kv[1]))

    lines += ["## 団地別", "",
              "「団地全体」は同じ大字・字で地番が近い筆の合計"
              f"（本番の差が {cfg['cluster_gap']} 以内）。"
              "隣接しているかは公図で確認すること。", ""]
    for cid, members in order:
        head = members[0]
        total = head.cluster_area
        lines += [f"### {cid} — {head.city}{head.place}", "",
                  f"団地全体 {head.cluster_size}筆 / {total:.0f}㎡ "
                  f"({total / 100:.1f}a) — うち上位 {len(members)}筆を掲載", "",
                  "| 候補ID | 地番 | 地目 | 面積(㎡) | 点 | 遊休 | 所有者の意向 | 根拠 |",
                  "|---|---|---|---|---|---|---|---|"]
        for m in members:
            intent = m.idle_intent or m.owner_intent or "-"
            area = f"{m.area_sqm:.0f}" if m.area_sqm is not None else "-"
            lines.append(f"| {m.cid} | {m.parcel_label} | {m.landuse or '-'} | "
                         f"{area} | {m.score} | {m.idle or '-'} | {intent} | "
                         f"{' / '.join(m.reasons)} |")
        lines.append("")

    lines += ["## 次の一手", "",
              "1. `parcels_to_query.csv` の上位から登記情報提供サービスで全部事項を取得する",
              "2. 同じ地番を持って農業委員会へ（`templates/農業委員会_照会文.md`）",
              "3. `survey/` の調査シートを持って現地へ（`templates/現地調査シート.md`）", ""]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


SURVEY_TMPL = """# 現地調査シート — {cid}

| | |
|---|---|
| 候補ID | {cid}（点数 {score}） |
| 所在 | {city}{place} |
| 地番 | {parcel_label} |
| 地目 / 面積 | {landuse} / {area}㎡（{area_a}a） |
| 団地 | {cluster_id}（{cluster_size}筆 / {cluster_area}㎡） |
| 農振法区分 | {shinko} |
| 都市計画法区分 | {toshi} |
| 遊休農地の判定 | {idle}（調査日 {survey_date}） |
| 所有者の意向 | {intent}（調査日 {intent_date}） |
| 所有者等の確知 | {owner_known} |
| 中間管理権 | {bank} |
| 機械採点の根拠 | {reasons} |
| 調査日 / 調査者 | 　　　　　／ |

## 1. 荒廃の程度
- [ ] A分類（再生可能）　- [ ] B分類（復元困難 → 撤退）　- [ ] 保留

主な植生：　　　　　／ 樹木　　本(最大　　cm)／ 竹：なし・縁のみ・全面
推定放棄年数：　　年　／ 再生費用概算：　　万円

## 2. 立地と作業性
進入路：幅　　m（軽トラ／2t／重機）　所有：公道・里道・私有（　　　　）
傾斜　　度／段数　　段／法面 長さ　　m×高さ　　m
日照：全日・午前・午後　／ 標高　　m ／ 電気・水：　　　／ 拠点から　　分

## 3. 水
水源：沢・井戸・用水路・雨水・なし
**用水路の上流に慣行圃場：あり（リスク高）・なし**
排水：良・停滞水・湧水　／ 水利費　　円/年

## 4. ドリフトリスク（自然栽培の要）

| 方位 | 隣接地 | 栽培方法 | 距離 | 緩衝帯 |
|---|---|---|---|---|
| 北 | | | m | m |
| 東 | | | m | m |
| 南 | | | m | m |
| 西 | | | m | m |

卓越風向：　　　／ 空中散布：あり・なし・要確認 ／ 総合判定：低・中・高

## 5. 獣害
イノシシ・シカ・サル・その他（　　　）／ 既存柵：あり・なし／ 必要延長　　m

## 6. 土壌サンプル
採取区：A・B・C　各5点以上、深さ0-15cm　／ 依頼先　　　　　／ 依頼日　　　　

## 7. 総合判定
- [ ] A：交渉に進む　- [ ] B：条件付き（　　　　）　- [ ] C：見送り（　　　　）

所見：

```


```
"""


def write_surveys(parcels, outdir, top):
    os.makedirs(outdir, exist_ok=True)
    for p in parcels[:top]:
        body = SURVEY_TMPL.format(
            cid=p.cid, score=p.score, city=p.city or "", place=p.place or "",
            parcel_label=p.parcel_label or "", landuse=p.landuse or "-",
            area=f"{p.area_sqm:.0f}" if p.area_sqm is not None else "-",
            area_a=f"{p.area_sqm/100:.1f}" if p.area_sqm is not None else "-",
            cluster_id=p.cluster_id, cluster_size=p.cluster_size,
            cluster_area=f"{p.cluster_area:.0f}",
            shinko=p.shinko or "-", toshi=p.toshi or "-", idle=p.idle or "-",
            survey_date=p.survey_date or "-",
            intent=p.idle_intent or p.owner_intent or "-",
            intent_date=p.intent_date or "-", owner_known=p.owner_known or "-",
            bank=p.bank or "-", reasons=" / ".join(p.reasons) or "-")
        with open(os.path.join(outdir, f"{p.cid}.md"), "w", encoding="utf-8") as fh:
            fh.write(body)


# ------------------------------------------------------------ CLI

def load_rows(paths, override):
    rows, mapping = [], {}
    for path in paths:
        part, enc = read_table(path)
        print(f"  読み込み: {path} ({enc}, {len(part)}行)", file=sys.stderr)
        if not mapping:
            mapping = guess_mapping(list(part[0].keys()), override)
        rows.extend(part)
    return rows, mapping


def cmd_inspect(args):
    rows, enc = read_table(args.csv)
    headers = list(rows[0].keys()) if rows else []
    mapping = guess_mapping(headers, load_override(args.map))
    print(f"文字コード: {enc} / 行数: {len(rows)} / 列数: {len(headers)}\n")
    print("== 認識できた列 ==")
    for f, _ in COLUMN_HINTS:
        col = mapping.get(f)
        sample = ""
        if col and rows:
            vals = [r.get(col, "") for r in rows[:20] if (r.get(col) or "").strip()]
            sample = vals[0] if vals else ""
        mark = "OK " if col else "-- "
        print(f"  {mark}{f:<12} <- {col or '(未検出)':<24} 例: {sample}")
    unused = [h for h in headers if h not in mapping.values()]
    if unused:
        print("\n== 未使用の列 ==")
        for h in unused:
            print(f"  {h}")
    print("\n未検出の列があれば --map で対応表(JSON)を渡す。例:")
    print('  {"idle": "遊休農地の別", "area": "登記地積"}')


def load_override(path):
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def cmd_rank(args):
    cfg = load_config(args.config)
    rows, mapping = load_rows(args.csv, load_override(args.map))
    if not rows:
        raise SystemExit("行が0件だった。CSVを確認すること。")
    missing = [f for f in ("location", "area") if f not in mapping]
    if missing:
        print(f"警告: 列を認識できなかった: {missing}。inspect で確認を。", file=sys.stderr)

    parcels = build_parcels(rows, mapping, cfg)
    parcels = cluster(parcels, cfg["cluster_gap"])
    parcels = score(parcels, cfg)
    if args.min_score is not None:
        parcels = [p for p in parcels if p.score >= args.min_score]

    os.makedirs(args.out, exist_ok=True)
    write_candidates(parcels, os.path.join(args.out, "candidates.csv"))
    write_parcels_to_query(parcels[:args.top],
                           os.path.join(args.out, "parcels_to_query.csv"))
    write_shortlist(parcels, os.path.join(args.out, "shortlist.md"), cfg, args.top)
    write_surveys(parcels, os.path.join(args.out, "survey"), args.top)

    print(f"\n{len(parcels)}筆を採点した。上位{min(args.top, len(parcels))}件を出力:")
    print(f"  {args.out}/shortlist.md")
    print(f"  {args.out}/candidates.csv")
    print(f"  {args.out}/parcels_to_query.csv   ← 登記請求はここから")
    print(f"  {args.out}/survey/*.md")
    print("\n所有者の氏名は出力しない。docs/03-地主を特定する.md を読むこと。")


def cmd_parcels(args):
    rows, _ = read_table(args.csv)
    parcels = []
    for r in rows:
        p = Parcel(
            cid=r.get("cid", ""), city_code=r.get("city_code", ""),
            city=r.get("city", ""), place=r.get("place", ""),
            parcel_label=r.get("parcel_label", ""), landuse=r.get("landuse", ""),
            area_sqm=to_float(r.get("area_sqm")), cluster_id=r.get("cluster_id", ""),
            reasons=[r.get("reasons", "")] if r.get("reasons") else [])
        parcels.append(p)
    os.makedirs(args.out, exist_ok=True)
    dest = os.path.join(args.out, "parcels_to_query.csv")
    write_parcels_to_query(parcels[:args.top], dest)
    print(f"{min(args.top, len(parcels))}筆を {dest} に書き出した。")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="耕作放棄地の候補を eMAFF農地ナビのCSVから絞り込む")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("inspect", help="列名の自動対応づけを確認する")
    p1.add_argument("csv")
    p1.add_argument("--map", help="列対応表(JSON)")
    p1.set_defaults(func=cmd_inspect)

    p2 = sub.add_parser("rank", help="採点して候補一式を出力する")
    p2.add_argument("csv", nargs="+")
    p2.add_argument("--out", default="out")
    p2.add_argument("--config", default=None, help="data/config.json")
    p2.add_argument("--map", help="列対応表(JSON)")
    p2.add_argument("--top", type=int, default=30)
    p2.add_argument("--min-score", type=int, default=None)
    p2.set_defaults(func=cmd_rank)

    p3 = sub.add_parser("parcels", help="candidates.csv から地番リストを作る")
    p3.add_argument("csv")
    p3.add_argument("--out", default="out")
    p3.add_argument("--top", type=int, default=100)
    p3.set_defaults(func=cmd_parcels)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
