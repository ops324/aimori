#!/usr/bin/env python3
"""
AIMORI PoC — Google Vision API Web Detection による逆画像検索

登録作品の画像を Google Vision API に渡し、ネット上のどこに同一・類似画像が
掲載されているかを取得する。

検知モード:
  デフォルト: メルカリ・minne・Creema 等のターゲットPFでのヒットを強調表示
  --all     : ターゲットPFに限らず、類似画像・掲載ページを全件表示（デザインパクリ検知用）

使い方:
    export GOOGLE_VISION_API_KEY=<your-key>
    python3 reverse_search.py path/to/image.jpg [image2.png ...]
    python3 reverse_search.py --url https://example.com/image.jpg
    python3 reverse_search.py --all path/to/image.jpg   # デザインパクリ検知

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
    "BOOTH": ["booth.pm"],
    "pixivFANBOX": ["fanbox.cc"],
    "X(Twitter)": ["x.com", "twitter.com", "twimg.com"],
    "楽天市場": ["rakuten.co.jp"],
    "Yahoo!フリマ": ["paypayfleamarket.yahoo.co.jp"],
}

OUT_DIR = Path(__file__).resolve().parent / "out"


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


class VisionAPIError(Exception):
    """Vision API 呼び出し失敗（HTTP/ネットワーク/レスポンス内エラー）。message は die() にそのまま渡せる形式。"""


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
        raise VisionAPIError(_explain_http_error(e.code, detail))
    except urllib.error.URLError as e:
        raise VisionAPIError(f"ネットワークエラー: {e.reason}")


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
    return msg


def explain_response_error(err):
    """レスポンス内 "error" フィールドをユーザー向けメッセージに変換する。
    HTTP自体は200でもクォータ超過を返すケースを、429と同じ文言で判別可能にする。"""
    message = err.get("message", str(err))
    if err.get("status") == "RESOURCE_EXHAUSTED" or "quota" in message.lower():
        return f"APIエラー (クォータ超過): {message}"
    return f"APIエラー: {message}"


def summarize_web_detection(web):
    """webDetection dict を分類済みサマリ dict に変換。API呼び出し・I/Oなしの純粋関数。"""
    pages = web.get("pagesWithMatchingImages", []) or []
    full = web.get("fullMatchingImages", []) or []
    partial = web.get("partialMatchingImages", []) or []
    similar = web.get("visuallySimilarImages", []) or []
    entities = web.get("webEntities", []) or []
    best_labels = web.get("bestGuessLabels", []) or []
    best_guess = best_labels[0].get("label") if best_labels else None

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

    # ② 直接一致（画像そのものがターゲットPFのCDN上に完全/部分一致で存在——掲載ページが
    #    Googleに未インデックスのケースも拾える。ページレベルのkindと同じ語彙を使うが、
    #    区別は「どちらのリストに入っているか」という構造で行う）
    flagged_direct = []
    for img, kind in [(u, "完全一致") for u in full] + [(u, "部分一致") for u in partial]:
        m = match_platform(img.get("url", ""))
        if m:
            flagged_direct.append((m[0], img.get("url", ""), kind))

    # ③ 類似画像URL（模倣品検知 — 別画像だが視覚的に似ているものがターゲットPFにある）
    flagged_similar = []
    for img in similar:
        url = img.get("url", "")
        m = match_platform(url)
        if m:
            flagged_similar.append((m[0], url))

    other_pages = [p for p in pages if not match_platform(p.get("url", ""))]

    return {
        "pages": pages,
        "full": full,
        "partial": partial,
        "similar": similar,
        "entities": entities,
        "best_guess": best_guess,
        "flagged_pages": flagged_pages,
        "flagged_direct": flagged_direct,
        "flagged_similar": flagged_similar,
        "flagged_all": len(flagged_pages) + len(flagged_direct) + len(flagged_similar),
        "other_pages": other_pages,
    }


def platform_hit_breakdown(summary):
    """summarize_web_detection() の flagged_* を PF別 dict に再グルーピングする補助関数。
    batch_verify.py など、PF別の内訳・has_page_hit 等が必要な呼び出し元向け。"""
    platform_page_hits = {pf: [] for pf in TARGET_PLATFORMS}
    platform_direct_hits = {pf: [] for pf in TARGET_PLATFORMS}
    platform_similar_hits = {pf: [] for pf in TARGET_PLATFORMS}
    for pf, url, kind, title in summary["flagged_pages"]:
        platform_page_hits[pf].append({"url": url, "kind": kind, "title": title})
    for pf, url, kind in summary["flagged_direct"]:
        platform_direct_hits[pf].append({"url": url, "kind": kind})
    for pf, url in summary["flagged_similar"]:
        platform_similar_hits[pf].append({"url": url})

    has_page_hit = any(platform_page_hits[pf] for pf in TARGET_PLATFORMS)
    has_direct_hit = any(platform_direct_hits[pf] for pf in TARGET_PLATFORMS)
    has_similar_only_hit = (
        any(platform_similar_hits[pf] for pf in TARGET_PLATFORMS)
        and not has_page_hit and not has_direct_hit
    )
    target_urls = {
        h["url"]
        for d in (platform_page_hits, platform_direct_hits, platform_similar_hits)
        for pf in TARGET_PLATFORMS
        for h in d[pf]
    }
    return {
        "platform_page_hits": platform_page_hits,
        "platform_direct_hits": platform_direct_hits,
        "platform_similar_hits": platform_similar_hits,
        "has_page_hit": has_page_hit,
        "has_direct_hit": has_direct_hit,
        "has_similar_only_hit": has_similar_only_hit,
        "target_url_count": len(target_urls),
    }


def analyze_one(api_key, label, image_source, save_name, show_all=False):
    """1画像を解析して結果を表示。生JSONを out/ に保存。"""
    body = build_request_body(image_source)
    result = call_vision_api(api_key, body)

    responses = result.get("responses", [{}])
    resp0 = responses[0] if responses else {}

    # APIがレスポンス単位でエラーを返す場合
    if "error" in resp0:
        raise VisionAPIError(explain_response_error(resp0["error"]))

    web = resp0.get("webDetection", {})

    # 生JSONを保存
    OUT_DIR.mkdir(exist_ok=True)
    out_path = OUT_DIR / f"{save_name}.web.json"
    out_path.write_text(json.dumps(web, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = summarize_web_detection(web)
    pages = summary["pages"]
    full = summary["full"]
    partial = summary["partial"]
    similar = summary["similar"]
    entities = summary["entities"]
    best_guess = summary["best_guess"]
    flagged_pages = summary["flagged_pages"]
    flagged_direct = summary["flagged_direct"]
    flagged_similar = summary["flagged_similar"]
    flagged_all = summary["flagged_all"]

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
          f"  （掲載ページ {len(flagged_pages)} 件 ／ 直接一致 {len(flagged_direct)} 件"
          f" ／ 類似画像 {len(flagged_similar)} 件）")

    if best_guess:
        print(f"  推定内容: {best_guess}")

    if entities:
        top = [e.get("description", "") for e in entities[:5] if e.get("description")]
        if top:
            print(f"  推定ラベル: {', '.join(top)}")

    if show_all:
        # --all モード: デザインパクリ検知 — ターゲットPF問わず全件表示
        if pages:
            print(f"\n📄【掲載ページ一覧】全 {len(pages)} 件")
            for p in pages:
                url = p.get("url", "")
                m = match_platform(url)
                pf_tag = f" [{m[0]}]" if m else ""
                title = p.get("pageTitle", "")
                has_full = bool(p.get("fullMatchingImages"))
                has_partial = bool(p.get("partialMatchingImages"))
                kind = "完全一致" if has_full else ("部分一致" if has_partial else "掲載")
                print(f"  ({kind}){pf_tag} {url}")
                if title:
                    print(f"      └ {title}")

        if full or partial:
            print(f"\n🎯【直接一致画像 全件】画像自体が完全/部分一致（掲載ページ非経由）")
            for img, kind in [(u, "完全一致") for u in full] + [(u, "部分一致") for u in partial]:
                url = img.get("url", "")
                m = match_platform(url)
                pf_tag = f" [{m[0]}]" if m else ""
                print(f"  ({kind}){pf_tag} {url}")

        if similar:
            print(f"\n🔍【類似画像一覧】全 {len(similar)} 件  ← デザインパクリ候補")
            for img in similar:
                url = img.get("url", "")
                m = match_platform(url)
                pf_tag = f" [{m[0]}]" if m else ""
                print(f"  {pf_tag} {url}")
    else:
        # デフォルトモード: ターゲットPFのヒットのみ強調
        if flagged_pages:
            print("\n🚨【要注意】ターゲットPFの掲載ページ（転載・転売の疑い）")
            for pf, url, kind, title in flagged_pages:
                print(f"  ● [{pf}] ({kind}) {url}")
                if title:
                    print(f"      └ {title}")

        if flagged_direct:
            print("\n🎯【直接一致】ターゲットPFで画像自体が完全/部分一致（掲載ページ未検出のケースを含む）")
            for pf, url, kind in flagged_direct:
                print(f"  ● [{pf}] ({kind}) {url}")

        if flagged_similar:
            print("\n⚠️ 【模倣品候補】ターゲットPFで見つかった類似画像")
            for pf, url in flagged_similar:
                print(f"  ● [{pf}] {url}")

        if not flagged_pages and not flagged_direct and not flagged_similar:
            print("\n（ターゲットPFでの一致・類似は見つかりませんでした）")
            print("  ヒント: --all オプションで全件の類似画像を確認できます")

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
    parser.add_argument("--all", action="store_true", dest="show_all",
                        help="ターゲットPFに限らず類似画像・掲載ページを全件表示（デザインパクリ検知用）")
    args = parser.parse_args()

    if not args.images and not args.url:
        parser.print_help()
        die("\nエラー: 画像ファイルまたは --url を1つ以上指定してください。")

    api_key = get_api_key()

    try:
        for path_str in args.images:
            src = load_local_image(path_str)
            analyze_one(api_key, path_str, src, safe_name(Path(path_str).name), show_all=args.show_all)

        for url in args.url:
            src = {"source": {"imageUri": url}}
            analyze_one(api_key, f"URL: {url}", src, safe_name(Path(urlparse(url).path).name or "url"), show_all=args.show_all)
    except VisionAPIError as e:
        die(str(e))


if __name__ == "__main__":
    main()
