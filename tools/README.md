# tools/hokichi.py

eMAFF農地ナビの公表データ（CSV）を、現地調査と登記請求に直接使えるリストに変換する。
**Python 3.9以上。依存パッケージなし**（標準ライブラリのみ）。

## できること / できないこと

| | |
|---|---|
| できる | 遊休農地の抽出、自然栽培向けの採点、団地候補のクラスタリング、**地番リストの出力**、現地調査シートの生成 |
| **できない** | **地主の氏名・住所の特定。** 公表データに含まれないため（農地法52条の3）。登記でしか取れない → `docs/03-地主を特定する.md` |

## 使い方

```bash
# 1. 列の自動対応づけを確認する（最初に必ず実行する）
python3 tools/hokichi.py inspect data/sample/emaff_sample.csv

# 2. 採点して一式を出力する
python3 tools/hokichi.py rank data/sample/emaff_sample.csv \
    --config data/config.json --out out --top 30

# 3. 既存の candidates.csv を手で絞り込んだあと、地番リストだけ作り直す
python3 tools/hokichi.py parcels out/candidates.csv --out out --top 20
```

複数市町村のCSVをまとめて渡せる（列構成が同じであること）。

```bash
python3 tools/hokichi.py rank data/*.csv --out out
```

## 出力

| ファイル | 用途 |
|---|---|
| `out/shortlist.md` | 団地ごとにまとめた候補一覧。人が読んで判断する |
| `out/candidates.csv` | 全筆の採点結果。表計算で追加の絞り込みをする |
| `out/parcels_to_query.csv` | **登記情報提供サービスへの請求リスト。** 請求日・取得済の列を手で埋めていく |
| `out/survey/C0001.md` | 候補ごとの現地調査シート。印刷して持っていく |

`out/` は `.gitignore` 済み。候補地の地番は個人の資産情報に近いため、コミットしない。

## 列の対応づけ

自治体によってCSVの列名が違う。自動で拾えない場合は対応表（JSON）を渡す。

```bash
cat > data/column_map.json <<'JSON'
{
  "location": "所在地番",
  "parcel":   "地番",
  "area":     "登記地積",
  "idle":     "遊休農地の別",
  "owner_intent": "所有者意向"
}
JSON
python3 tools/hokichi.py rank <csv> --map data/column_map.json --out out
```

認識される正規化フィールド:
`city_code, city, location, parcel, landuse, area, shinko, toshi, idle,
survey_date, owner_intent, idle_intent, intent_date, owner_known, bank,
right_type, right_start`

文字コードは UTF-8 / UTF-8(BOM) / Shift_JIS(CP932) / EUC-JP を自動判定する。

## 採点の調整

`data/config.json` の `weights` を書き換える。各項目の意味は
`docs/01-耕作放棄地を探す.md` の「絞り込みの考え方」にある。

面積がアール表記のCSVなら `"area": {"unit": "a"}` にする。
対象市町村を絞るなら `"target": {"city_codes": ["29212"]}`。

## 団地クラスタリングについて

同じ大字・字の中で、地番の本番の差が `cluster_gap`（既定5）以内の筆を
同じ団地とみなす。**これは現地調査の優先順位を付けるための近似であり、
実際に隣接しているかは公図で確認すること。** 地番が近くても
飛び地になっていることはある。

## 動作確認

```bash
python3 tools/hokichi.py rank data/sample/emaff_sample.csv --out /tmp/hokichi-test --top 15
```

サンプルは実在の農地ではない。列構成と動作確認のためのダミーデータ。
