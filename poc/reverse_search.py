#!/usr/bin/env python3
"""
AIMORI PoC — Google Vision API Web Detection による逆画像検索

登録作品の画像を Google Vision API に渡し、ネット上のどこに同一・類似画像が
掲載されているかを取得する。メルカリ・minne・Creema 等のターゲット
プラットフォームでヒットしたページを「要注意」として抽出する。

使い方:
    export GOOGLE_VISION_API_KEY=<your-key>
    python3 reverse_search.py path/to/image.jpg [image2.png ...]
    python3 reverse_search.py --url https://example.com/image.jpg

依存: 標準ライブラリのみ（pip install 不要）
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

API_ENDPOINT = "https://vision.googleapis.com/v1/images:annotate"
MAX_RESULTS = 50
SIZE_WARN_BYTES = 4 * 1024 * 1024  # 4MB を超えたら警告

# ターゲットプラットフォーム: 表示名 -> 判定に使うホスト名（末尾一致）のリスト
TARGET_PLATFORMS = {
    "メルカリ": ["mercari.com", "jp.mercari.com", "mercari-shops.com"],
    "minne": ["minne.com"],
    "Creema": ["creema.jp"],
    "BASE": [
        "base.shop", "base.ec", "thebase.in",
        "official.ec", "buyshop.jp", "shopselect.net", "theshop.jp",
    ],
    "pixiv": ["pixiv.net"],
    "X(Twitter)": ["x.com", "twitter.com"],
    "楽天市場": ["rakuten.co.jp"],
    "Yahoo!フリマ": ["paypayfleamarket.yahoo.co.jp"],
}

OUT_DIR = Path(__file__).resolve().parent / "out"


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def get_api_key():
    key = os.environ.get("GOOGLE_VISION_API_KEY", "").strip()
    if not key:
        die(
            "エラー: 環境変数 GOOGLE_VISION_API_KEY が設定されていません。\n\n"
            "設定方法:\n"
            "  export GOOGLE_VISION_API_KEY=<あなたのAPIキー>\n\n"
            "APIキーの取得手順は poc/README.md を参照してください。"
        )
    return key


def match_platform(url):
    """URL のホスト名がターゲットPFに一致すれば (PF名, host) を返す。なければ None。"""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return None
    for name, domains in TARGET_PLATFORMS.items():
        for d in domains:
            # 末尾一致（サブドメイン対応）: host == d または host が ".d" で終わる
            if host == d or host.endswith("." + d):
                return name, host
    return None


def build_request_body(image_source):
    """image_source: {"content": b64} または {"source": {"imageUri": url}}"""
    return json.dumps({
        "requests": [{
            "image": image_source,
            "features": [{"type": "WEB_DETECTION", "maxResults": MAX_RESULTS}],
        }]
    }).encode("utf-8")


def call_vision_api(api_key, body):
    req = urllib.request.Request(
        API_ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-goog-api-key": api_key,  # キーはヘッダーで送る（URLに載せない）
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            pass
        _explain_http_error(e.code, detail)
    except urllib.error.URLError as e:
        die(f"ネットワークエラー: {e.reason}")


def _explain_http_error(code, detail):
    hints = {
        400: "APIキーの形式が不正か、リクエストが不正です。キーを確認してください。",
        401: "認証に失敗しました。APIキーが正しいか確認してください。",
        403: ("Cloud Vision API が有効化されていないか、キーに権限がありません。\n"
              "  → https://console.cloud.google.com/apis/library/vision.googleapis.com "
              "で「有効にする」を押してください。"),
        429: "無料枠(月1,000ユニット)を超過したか、レート制限に達しました。",
    }
    hint = hints.get(code, "予期しないエラーです。")
    msg = f"APIエラー (HTTP {code}): {hint}"
    if detail:
        msg += f"\n--- APIからの詳細 ---\n{detail}"
    die(msg)


def analyze_one(api_key, label, image_source, save_name):
    """1画像を解析して結果を表示。生JSONを out/ に保存。"""
    body = build_request_body(image_source)
    result = call_vision_api(api_key, body)

    responses = result.get("responses", [{}])
    resp0 = responses[0] if responses else {}

    # APIがレスポンス単位でエラーを返す場合
    if "error" in resp0:
        err = resp0["error"]
        die(f"APIエラー: {err.get('message', err)}")

    web = resp0.get("webDetection", {})

    pages = web.get("pagesWithMatchingImages", []) or []
    full = web.get("fullMatchingImages", []) or []
    partial = web.get("partialMatchingImages", []) or []
    similar = web.get("visuallySimilarImages", []) or []
    entities = web.get("webEntities", []) or []

    # 生JSONを保存
    OUT_DIR.mkdir(exist_ok=True)
    out_path = OUT_DIR / f"{save_name}.web.json"
    out_path.write_text(json.dumps(web, ensure_ascii=False, indent=2), encoding="utf-8")

    # ターゲットPFヒットを抽出
    # ① 掲載ページ（転載・転売 — 同一/部分一致画像が載っているページ）
    flagged_pages = []
    for p in pages:
        url = p.get("url", "")
        m = match_platform(url)
        if m:
            has_full = bool(p.get("fullMatchingImages"))
            has_partial = bool(p.get("partialMatchingImages"))
            kind = "完全一致" if has_full else ("部分一致" if has_partial else "掲載")
            flagged_pages.append((m[0], url, kind, p.get("pageTitle", "")))

    # ② 類似画像URL（模倣品検知 — 別画像だが視覚的に似ているものがターゲットPFにある）
    flagged_similar = []
    for img in similar:
        url = img.get("url", "")
        m = match_platform(url)
        if m:
            flagged_similar.append((m[0], url))

    flagged_all = len(flagged_pages) + len(flagged_similar)

    # ---- 出力 ----
    print("=" * 70)
    print(f"■ 対象: {label}")
    print("=" * 70)

    print("\n[サマリ]")
    print(f"  掲載ページ (pagesWithMatchingImages) : {len(pages)} 件")
    print(f"  完全一致画像 (fullMatchingImages)    : {len(full)} 件")
    print(f"  部分一致画像 (partialMatchingImages) : {len(partial)} 件")
    print(f"  類似画像 (visuallySimilarImages)     : {len(similar)} 件")
    print(f"  ★ ターゲットPFヒット合計            : {flagged_all} 件"
          f"  （掲載ページ {len(flagged_pages)} 件 ／ 類似画像 {len(flagged_similar)} 件）")

    if entities:
        top = [e.get("description", "") for e in entities[:5] if e.get("description")]
        if top:
            print(f"  推定ラベル: {', '.join(top)}")

    if flagged_pages:
        print("\n🚨【要注意】ターゲットPFの掲載ページ（転載・転売の疑い）")
        for pf, url, kind, title in flagged_pages:
            print(f"  ● [{pf}] ({kind}) {url}")
            if title:
                print(f"      └ {title}")

    if flagged_similar:
        print("\n⚠️ 【模倣品候補】ターゲットPFで見つかった類似画像")
        for pf, url in flagged_similar:
            print(f"  ● [{pf}] {url}")

    if not flagged_pages and not flagged_similar:
        print("\n（ターゲットPFでの一致・類似は見つかりませんでした）")

    other = [p for p in pages if not match_platform(p.get("url", ""))]
    if other:
        print(f"\n【参考】その他の掲載ページ (上位{min(10, len(other))}件)")
        for p in other[:10]:
            print(f"  - {p.get('url', '')}")

    print(f"\n生レスポンス保存先: {out_path}")
    print()


def load_local_image(path_str):
    path = Path(path_str)
    if not path.is_file():
        die(f"エラー: ファイルが見つかりません: {path_str}")
    size = path.stat().st_size
    if size > SIZE_WARN_BYTES:
        print(f"⚠️  警告: {path.name} は {size/1024/1024:.1f}MB あります。"
              "大きい画像はエンコード後にAPI上限(20MB)を超える可能性があります。",
              file=sys.stderr)
    data = path.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return {"content": b64}


def safe_name(s):
    """保存ファイル名に使える文字列へ."""
    keep = [c if (c.isalnum() or c in "-_.") else "_" for c in s]
    return "".join(keep)[:80] or "image"


def main():
    parser = argparse.ArgumentParser(
        description="Google Vision API Web Detection で画像の転載元を探す (AIMORI PoC)"
    )
    parser.add_argument("images", nargs="*", help="ローカル画像ファイルのパス（複数可）")
    parser.add_argument("--url", action="append", default=[],
                        help="画像のURL（複数指定可）。ローカルファイルの代わりに使用")
    args = parser.parse_args()

    if not args.images and not args.url:
        parser.print_help()
        die("\nエラー: 画像ファイルまたは --url を1つ以上指定してください。")

    api_key = get_api_key()

    for path_str in args.images:
        src = load_local_image(path_str)
        analyze_one(api_key, path_str, src, safe_name(Path(path_str).name))

    for url in args.url:
        src = {"source": {"imageUri": url}}
        analyze_one(api_key, f"URL: {url}", src, safe_name(Path(urlparse(url).path).name or "url"))


if __name__ == "__main__":
    main()
