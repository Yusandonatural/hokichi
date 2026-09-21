#!/usr/bin/env python3
"""農林水産省の筆ポリゴン（農地の区画形状）を都道府県単位で取得する。

配布元: 農林水産省「地域の農業を見て・知って・活かすDB」
  https://www.machimura.maff.go.jp/shurakudata/<rcom_year>/mb/MB0001_<year>_<rcom_year>_<pref>.zip
  中身は FlatGeobuf (.fgb)。属性: polygon_uuid, land_type(100=田/200=畑),
  local_government_cd, issue_year, edit_year, point_lat, point_lng ほか。
  利用条件: 出典明記（「筆ポリゴンデータ（○年度公開）」農林水産省）。

    python3 pipeline/fetch_fude.py --pref 29            # 奈良県
    python3 pipeline/fetch_fude.py --pref 29 --year 2025 --rcom-year 2020 --out data/fude
"""
import argparse
import os
import sys
import urllib.error
import urllib.request
import zipfile

URL = "https://www.machimura.maff.go.jp/shurakudata/{rcom}/mb/MB0001_{year}_{rcom}_{pref:02d}.zip"

# 配布サーバーが UA で弾くことがあるため、ブラウザ相当の UA から順に試す
UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "hokichi/1.0 (+https://github.com/Yusandonatural/hokichi)",
]


def download(url, dest, ua):
    req = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Accept": "application/zip,application/octet-stream,*/*;q=0.8",
        "Accept-Language": "ja,en;q=0.8",
        "Referer": "https://www.machimura.maff.go.jp/",
    })
    tmp = dest + ".part"
    with urllib.request.urlopen(req, timeout=600) as r, open(tmp, "wb") as fh:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
    os.replace(tmp, dest)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pref", type=int, required=True, help="都道府県コード（奈良県=29）")
    ap.add_argument("--year", type=int, default=2025, help="筆ポリゴンの公開年度")
    ap.add_argument("--rcom-year", type=int, default=2020, help="農業集落境界の年度")
    ap.add_argument("--out", default="data/fude")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--release-repo", default=os.environ.get("GITHUB_REPOSITORY", "Yusandonatural/hokichi"),
                    help="GitHub Release（タグ fude-data）に手動アップロードした zip を先に探す")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    url = URL.format(rcom=a.rcom_year, year=a.year, pref=a.pref)
    zpath = os.path.join(a.out, os.path.basename(url))
    if a.force or not os.path.exists(zpath):
        # 農水省の配布サーバーは GitHub Actions からのアクセスを 403 で拒否する。
        # 手動でブラウザからダウンロードし、GitHub Release（タグ fude-data）に添付した
        # zip を最優先で探し、無ければ配布サーバーを UA と年度を変えて試す。
        candidates = []
        if a.release_repo:
            for year in (a.year, a.year - 1):
                name = os.path.basename(URL.format(rcom=a.rcom_year, year=year, pref=a.pref))
                candidates.append((f"https://github.com/{a.release_repo}/releases/download/fude-data/{name}", UAS[-1]))
        for year in (a.year, a.year - 1):
            u = URL.format(rcom=a.rcom_year, year=year, pref=a.pref)
            for ua in UAS:
                candidates.append((u, ua))
        last = None
        for u, ua in candidates:
            try:
                print(f"取得: {u}  (UA: {ua[:40]}…)", file=sys.stderr)
                download(u, zpath, ua)
                url, zpath = u, os.path.join(a.out, os.path.basename(u))
                break
            except urllib.error.HTTPError as exc:
                last = exc
                print(f"  → HTTP {exc.code} {exc.reason}", file=sys.stderr)
                if exc.code not in (403, 404, 406, 429):
                    raise
            except urllib.error.URLError as exc:
                last = exc
                print(f"  → {exc.reason}", file=sys.stderr)
        else:
            raise SystemExit(
                f"筆ポリゴンを取得できなかった（最後のエラー: {last}）。\n"
                f"  ブラウザで {URL.format(rcom=a.rcom_year, year=a.year, pref=a.pref)} をダウンロードし、\n"
                f"  GitHub Release（タグ fude-data）に添付するか、{a.out}/ に置くこと。docs/07-Webサービス.md を参照。")
    else:
        print(f"既存を使用: {zpath}", file=sys.stderr)

    with zipfile.ZipFile(zpath) as z:
        names = [n for n in z.namelist()
                 if n.lower().endswith((".fgb", ".geojson", ".json"))
                 and not os.path.basename(n).startswith("._")      # macOS の資源フォーク
                 and "__MACOSX/" not in n]
        if not names:
            raise SystemExit(f"zip内に空間データが見当たらない: {z.namelist()[:10]}")
        for n in names:
            dest = os.path.join(a.out, os.path.basename(n))
            if a.force or not os.path.exists(dest):
                print(f"展開: {n} -> {dest}", file=sys.stderr)
                with z.open(n) as src, open(dest, "wb") as dst:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        dst.write(chunk)
    print("\n".join(os.path.join(a.out, os.path.basename(n)) for n in names))


if __name__ == "__main__":
    main()
