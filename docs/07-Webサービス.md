# 07 Webサービス — 耕作放棄地マップ

「どこに放棄地があるか」を地図で見る社内向けサービス。
eMAFF農地ナビの公表情報（遊休農地）を `tools/hokichi.py` の基準で採点し、
農林水産省の筆ポリゴン（区画形状）に重ねて表示する。

```
data/emaff/*.csv  ──┐
                    ├─ pipeline/build_web_data.py ─→ web/data/*.geojson ─→ Cloudflare Workers（web/ を配信）
data/fude/*.fgb   ──┘                                                     └ worker/src/index.js が Basic 認証
   ↑ pipeline/fetch_fude.py（自動）
```

## できること

- 奈良県の市町村ごとに遊休農地を表示。点数で色分け、団地ごとに一覧
- 最低点数・貸付意向・所有者不明・地目でフィルタ
- 区画をクリック → 地番・面積・意向・確知状況・採点根拠。地理院地図／Googleマップへ
- 「登記請求リスト」に地番を溜めて CSV 書出し、農業委員会の照会文をワンクリックで生成
- 背景を淡色地図／標準地図／**航空写真**に切替（荒れ具合は航空写真で当たりを付ける）

## できないこと（設計上）

- **所有者の氏名・住所は出ない。** 公表情報に含まれない（農地法52条の3）。地番から登記を引く
- 遊休農地の判定は農業委員会の調査時点のもの。現況は現地で確認する
- 区画形状が「推定」と出ている筆は、筆ポリゴンに一致しなかったので点の周りに正方形を描いている

## データの流れと、人がやること

| データ | 取得 | 自動化 |
|---|---|---|
| 筆ポリゴン（区画形状） | 農林水産省の配布URLから県単位ZIP | **自動**（GitHub Actions が取得・キャッシュ） |
| eMAFF農地ナビの公表情報（遊休農地フラグ・意向・緯度経度） | eMAFF農地ナビの画面からCSVダウンロード | **手動**（下記） |
| 地理院タイル（背景地図・航空写真） | 国土地理院のタイル配信 | 自動（ブラウザが直接読む） |

### eMAFF農地ナビからのCSV取得（手動・市町村ごと）

一括ダウンロードの公開データが見当たらないため、画面から取得する。

1. https://map.maff.go.jp/ を開き、奈良県 → 市町村を選ぶ
2. 検索条件で「遊休農地」に絞る（全件でも構わないが重くなる）
3. 表示範囲の公表情報を **CSV（ピン情報）** でダウンロード。ピン情報には緯度・経度と地番が含まれる
4. ファイルを `data/emaff/<市町村コード>.csv` として保存（例: `data/emaff/29212.csv`）
5. `python3 tools/hokichi.py inspect data/emaff/29212.csv` で列が認識されるか確認。
   認識されない列があれば `data/column_map.json` に対応表を書く
6. コミットして push → Actions がビルド・デプロイ

CSV は公表情報であり氏名を含まないので、リポジトリに置いてよい。
ただし容量が大きくなるので、県全体をまとめる段階になったら Git LFS を検討する。

### 筆ポリゴンの出典と更新

`pipeline/fetch_fude.py` は次を取得する。

```
https://www.machimura.maff.go.jp/shurakudata/2020/mb/MB0001_2025_2020_29.zip
                                              └集落境界年 └公開年度 └県コード
```

年度が変わったら `.github/workflows/build-and-deploy.yml` の `FUDE_YEAR` を上げる。
利用条件は出典明記。画面の下部に「筆ポリゴンデータ（2025年度公開）」（農林水産省）を表示している。

## ローカルで動かす

```bash
pip install -r pipeline/requirements.txt
python3 pipeline/fetch_fude.py --pref 29                   # 初回だけ（数百MB）
python3 pipeline/build_web_data.py --emaff data/emaff --fude data/fude --out web/data
# CSVがまだ無ければサンプルで:
python3 pipeline/build_web_data.py --emaff data/sample --out web/data

npx wrangler dev          # http://localhost:8787 （.dev.vars に HOKICHI_USER / HOKICHI_PASSWORD を書く）
# または認証なしで素早く見る
python3 -m http.server -d web 8000
```

## デプロイ（Cloudflare Workers）

### 1. 初回設定（手元で1回）

```bash
npx wrangler login
npx wrangler secret put HOKICHI_USER       # 社内共通のユーザー名
npx wrangler secret put HOKICHI_PASSWORD   # パスワード
npx wrangler deploy
```

`https://hokichi.<account>.workers.dev` で Basic 認証つきで開ける。
秘密情報が未設定のときは 503 を返し、何も配信しない。

### 2. GitHub Actions からの自動デプロイ

リポジトリの Settings → Secrets and variables → Actions に追加:

| Secret | 内容 |
|---|---|
| `CLOUDFLARE_API_TOKEN` | Workers の編集権限を持つ API トークン（Cloudflare ダッシュボード → My Profile → API Tokens → "Edit Cloudflare Workers" テンプレート） |
| `CLOUDFLARE_ACCOUNT_ID` | アカウントID（Workers の概要ページ右側） |

これで `data/emaff/` や `web/` を変更して main に push すると、ビルド → デプロイまで自動で走る。
毎月1日にも自動で回る（筆ポリゴンの更新取り込み用）。
Secrets が無い場合はデプロイをスキップして `web-data` をアーティファクトとして残すだけになる。

### 3. Cloudflare Access に切り替える（推奨・任意）

Basic 認証はパスワードを社内で共有する形になる。人数が増えたら
Cloudflare Zero Trust → Access → Applications で `hokichi.<account>.workers.dev` を登録し、
悠三堂のメールドメインだけ許可する（50人まで無料）。
そのうえで `wrangler.toml` の `AUTH_MODE` を `"access"` にすると Worker 側の Basic 認証を外せる。

## 一般公開に切り替えるとき

社内利用のうちは不要だが、公開する前に:

- [ ] eMAFF農地ナビの利用規約で、公表情報の二次配布・加工表示の条件を再確認する
- [ ] 筆ポリゴン・地理院タイルの出典表示が画面に出ていることを確認する
- [ ] 免責（判定は農業委員会の調査時点、現況は要確認、氏名は含まれない）を画面に載せる
- [ ] 問い合わせ先（農業委員会・農地バンク）への導線を付ける
- [ ] `<meta name="robots" content="noindex">` を外すかどうか決める
- [ ] Worker の認証を外す（`AUTH_MODE` と secrets）

## 構成ファイル

| パス | 役割 |
|---|---|
| `pipeline/fetch_fude.py` | 筆ポリゴン取得 |
| `pipeline/build_web_data.py` | CSV＋ポリゴン → `web/data/` |
| `pipeline/requirements.txt` | pyogrio / shapely / numpy |
| `web/index.html`, `app.js`, `style.css` | 地図UI（MapLibre GL JS、ビルド不要） |
| `web/data/` | 生成物（Git 管理外） |
| `worker/src/index.js` | Basic 認証つき静的配信 |
| `wrangler.toml` | Workers 設定 |
| `.github/workflows/build-and-deploy.yml` | 月次ビルド＋デプロイ |
