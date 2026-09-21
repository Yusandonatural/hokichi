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
| 筆ポリゴン（区画形状） | 農林水産省の配布URLから県単位ZIP | **初回だけ手動**（配布サーバーが Actions を拒否するため Release に添付。以後は自動） |
| eMAFF農地ナビの公表情報（遊休農地フラグ・意向・緯度経度） | eMAFF農地ナビの画面から「ピン情報」GeoJSON をダウンロード | **手動**（下記） |
| 地理院タイル（背景地図・航空写真） | 国土地理院のタイル配信 | 自動（ブラウザが直接読む） |

### eMAFF農地ナビからのデータ取得（手動・表示範囲ごと）

一括ダウンロードの公開データが見当たらないため、画面から取得する。
**「ピン情報」の GeoJSON がそのまま使える**（2026-09-21 に奈良市東部で実データ確認済み）。

1. https://map.maff.go.jp/ を開き、奈良県 → 市町村 → 見たい範囲を表示する
2. 農地詳細情報の画面から **ピン情報（GeoJSON）** をダウンロードする
   （表示範囲の全筆が入る。遊休農地以外も含まれるがビルド時に絞られる）
3. `data/emaff/<市町村コード>-<地区>-<日付>.geojson` として保存して push
   例: `data/emaff/29201-nara-20260921.geojson`
4. Actions が自動でビルド・デプロイする（`pages-preview`）

同じ市町村を複数回に分けて取得しても、市町村コードでまとめて1つの地図になる。
古いファイルを置き換えるときは同じ範囲の旧ファイルを消す（同じ筆が二重に載るのを防ぐ）。

ピン情報の主な項目とアプリ内での扱い:

| GeoJSON のキー | 意味 | 扱い |
|---|---|---|
| `Tiban`, `Address` | 地番、所在 | 所在から県名・市名を除いて「大字・字」に |
| `UsageSituationInvestigationResult` | 利用状況調査の結果コード（1=遊休農地(不耕作) 2=遊休農地(低利用) 3=遊休農地ではない） | 1・2 だけを表示対象にする |
| `OwnerStatementIntentSurveyResultsCodeName` | 利用意向調査の回答（例: 農地中間管理事業の利用） | 「貸付意向」の加点に使う |
| `OwnerFarmIntentionCodeName` | 所有者の農地に関する意向（多くは「非公表」） | 非公表は空扱い |
| `UseIntentionAscertainmentResultCodeName` | 所有者等の確知の状況 | 「不確知」なら減点（裁定ルートの候補） |
| `FarmerIndicationNumberHash` | 耕作者/所有者のハッシュ値（氏名は分からない） | 同じハッシュの遊休筆数を「同じ所有者」として表示 |
| `RightSettingContents`, `KindOfRight` | 中間管理権・権利の種類のコード | 0 は「なし」 |

CSV 形式のダウンロードにも対応している（列名は `tools/hokichi.py inspect` で確認）。

### 筆ポリゴンの取得（初回だけ手動・年1回）

農林水産省の配布サーバー（machimura.maff.go.jp）は **GitHub Actions からのアクセスを 403 で拒否する**
（User-Agent や年度を変えても同じ。実測済み）。そのため初回だけブラウザで取得し、
GitHub Release に添付する。以後は Actions が Release から取る。

1. ブラウザで次の URL を開いてダウンロードする（奈良県・2025年度公開・数百MB）
   `https://www.machimura.maff.go.jp/shurakudata/2020/mb/MB0001_2025_2020_29.zip`
   （開けない場合は https://open.fude.maff.go.jp/ から奈良県を選んでダウンロード）
2. https://github.com/Yusandonatural/hokichi/releases/new を開く
   - Tag: `fude-data`（既にあれば「Edit release」で同じタグに追加）
   - Title: `筆ポリゴン 2025年度公開（奈良県）`
   - Attach files にダウンロードした zip を **ファイル名を変えずに** ドラッグ
   - 「Publish release」
3. Actions の `pages-preview` を「Run workflow」で再実行

`pipeline/fetch_fude.py` は Release の zip → 配布サーバーの順に探す。
年度が変わったら `.github/workflows/*.yml` の `FUDE_YEAR` を上げ、新しい zip を同じ Release に足す。
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
