#!/usr/bin/env python3
"""
AIMORI PoC — バッチ検証スクリプト

ディレクトリ内の全画像を順次 reverse_search.py で検索し、
ターゲットPFごとのヒット率・URL一覧を VERIFICATION_SUMMARY.md にまとめる。

使い方:
    export GOOGLE_VISION_API_KEY=<your-key>
    python3 poc/batch_verify.py ./test_images/
    python3 poc/batch_verify.py ./test_images/ --force            # キャッシュ無視
    python3 poc/batch_verify.py ./test_images/ --delay 2          # API間隔（秒）
    python3 poc/batch_verify.py ./test_images/ --max 20           # 最大処理枚数
    python3 poc/batch_verify.py ./test_images/ --output PATH      # 出力先指定
    python3 poc/batch_verify.py ./test_images/ --yes              # 確認プロンプトスキップ

依存: 標準ライブラリのみ

注意:
- checkpoint.jsonl は resume 用ではなくクラッシュ時の部分レポート復元用
  （過去 checkpoint を新規実行時に読むことはしない — stale データ汚染防止）
- subprocess timeout=90秒は API 側 60秒 + マージン
  timeout 発火時も API 課金は発生済みの可能性あり
- 月次累計トラッキングは行わない（PoC スコープ外）
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))  # `python3 -m` 実行も許容

from reverse_search import (  # noqa: E402
    OUT_DIR,
    TARGET_PLATFORMS,
    get_api_key,
    match_platform,
    safe_name,
)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
REVERSE_SEARCH_PY = _HERE / "reverse_search.py"
DEFAULT_REPORT = OUT_DIR / "VERIFICATION_SUMMARY.md"
COST_PROMPT_THRESHOLD = 5
SUBPROCESS_TIMEOUT_SEC = 90


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def sha1_of(path):
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_images(dir_path):
    return sorted(
        p for p in dir_path.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    )


def detect_safe_name_collisions(images):
    """バッチ内の safe_name 衝突を検知。衝突があれば die。"""
    seen = {}
    collisions = {}
    for img in images:
        sn = safe_name(img.name)
        if sn in seen:
            collisions.setdefault(sn, [seen[sn]]).append(img.name)
        else:
            seen[sn] = img.name
    if collisions:
        lines = ["エラー: safe_name の衝突を検知しました（同じキャッシュファイルに書き込まれます）:"]
        for sn, names in collisions.items():
            lines.append(f"  {sn} ← {', '.join(names)}")
        lines.append("ファイル名を変更してから再実行してください。")
        die("\n".join(lines))


def json_path_for(sname):
    return OUT_DIR / f"{sname}.web.json"


def meta_path_for(sname):
    return OUT_DIR / f"{sname}.web.json.meta"


def read_meta(sname):
    """meta サイドカーを読む。無ければ None。"""
    p = meta_path_for(sname)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_meta(sname, image_path):
    meta = {"src_sha1": sha1_of(image_path), "src_size": image_path.stat().st_size}
    meta_path_for(sname).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def scan_cache_status(images, force):
    """
    各画像に対しキャッシュ状態を判定して返す。

    戻り値: list of dict [{"image": Path, "sname": str, "action": str, "note": str}]
      action: "cache" | "cache_legacy" | "cache_mismatch" | "no_cache" | "force"
    """
    statuses = []
    for img in images:
        sname = safe_name(img.name)
        entry = {"image": img, "sname": sname, "note": ""}

        if force:
            entry["action"] = "force"
        elif not json_path_for(sname).exists():
            entry["action"] = "no_cache"
        else:
            meta = read_meta(sname)
            if meta is None:
                entry["action"] = "cache_legacy"
            else:
                current_sha = sha1_of(img)
                if meta.get("src_sha1") == current_sha:
                    entry["action"] = "cache"
                else:
                    entry["action"] = "cache_mismatch"
                    entry["note"] = "同名 safe_name で別画像がキャッシュ済み"
        statuses.append(entry)
    return statuses


def confirm_cost(uncached_count, yes_flag):
    if uncached_count <= COST_PROMPT_THRESHOLD or yes_flag:
        return
    print(
        f"\n{uncached_count} 件の画像について新規に Vision API を呼びます。\n"
        "  Vision API は月 1000 ユニットまで無料、超過分 $3.50/1000 ユニット。\n"
        "続行しますか？ [y/N]: ",
        end="",
        flush=True,
    )
    try:
        ans = input().strip().lower()
    except EOFError:
        ans = ""
    if ans not in ("y", "yes"):
        die("中断しました。--yes で確認プロンプトをスキップできます。", code=0)


def parse_web_json(sname):
    """.web.json をパース。無ければ None。"""
    p = json_path_for(sname)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def build_result_from_web(image_path, sname, status, web):
    """webDetection dict から result dict を構築。"""
    pages = web.get("pagesWithMatchingImages", []) or []
    full = web.get("fullMatchingImages", []) or []
    partial = web.get("partialMatchingImages", []) or []
    similar = web.get("visuallySimilarImages", []) or []
    best_labels = web.get("bestGuessLabels", []) or []

    best_guess = None
    if best_labels:
        best_guess = best_labels[0].get("label") or None

    # PF別ヒットの抽出（ページ）
    platform_page_hits = {pf: [] for pf in TARGET_PLATFORMS}
    for p in pages:
        url = p.get("url", "")
        m = match_platform(url)
        if not m:
            continue
        has_full = bool(p.get("fullMatchingImages"))
        has_partial = bool(p.get("partialMatchingImages"))
        kind = "完全一致" if has_full else ("部分一致" if has_partial else "掲載")
        platform_page_hits[m[0]].append({
            "url": url,
            "kind": kind,
            "title": p.get("pageTitle", "") or "",
        })

    # PF別ヒットの抽出（類似画像）
    platform_similar_hits = {pf: [] for pf in TARGET_PLATFORMS}
    for img in similar:
        url = img.get("url", "")
        m = match_platform(url)
        if not m:
            continue
        platform_similar_hits[m[0]].append({"url": url})

    # 画像内 URL 重複排除で target_url_count 計算
    target_urls_in_image = set()
    for pf in TARGET_PLATFORMS:
        for h in platform_page_hits[pf]:
            target_urls_in_image.add(h["url"])
        for h in platform_similar_hits[pf]:
            target_urls_in_image.add(h["url"])

    has_page_hit = any(platform_page_hits[pf] for pf in TARGET_PLATFORMS)
    has_similar_only_hit = (
        any(platform_similar_hits[pf] for pf in TARGET_PLATFORMS) and not has_page_hit
    )

    return {
        "filename": image_path.name,
        "save_name": sname,
        "status": status,
        "best_guess": best_guess,
        "pages_total": len(pages),
        "full_total": len(full),
        "partial_total": len(partial),
        "similar_total": len(similar),
        "platform_page_hits": platform_page_hits,
        "platform_similar_hits": platform_similar_hits,
        "has_page_hit": has_page_hit,
        "has_similar_only_hit": has_similar_only_hit,
        "target_url_count": len(target_urls_in_image),
        "error": None,
    }


def failure_result(image_path, sname, error_msg):
    return {
        "filename": image_path.name,
        "save_name": sname,
        "status": "failed",
        "best_guess": None,
        "pages_total": 0,
        "full_total": 0,
        "partial_total": 0,
        "similar_total": 0,
        "platform_page_hits": {pf: [] for pf in TARGET_PLATFORMS},
        "platform_similar_hits": {pf: [] for pf in TARGET_PLATFORMS},
        "has_page_hit": False,
        "has_similar_only_hit": False,
        "target_url_count": 0,
        "error": error_msg,
    }


def run_subprocess_for(image_path):
    """
    reverse_search.py をサブプロセスで実行。
    戻り値: (ok: bool, error_msg: str | None, rate_limited: bool)
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(REVERSE_SEARCH_PY), str(image_path)],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return (
            False,
            f"タイムアウト（{SUBPROCESS_TIMEOUT_SEC}秒）— API 課金は発生済みの可能性あり",
            False,
        )

    if proc.returncode == 0:
        return True, None, False

    stderr = (proc.stderr or "").strip()
    # 429 検知（reverse_search.py:127 の実出力フォーマットに厳密一致）
    rate_limited = "APIエラー (HTTP 429)" in stderr
    return False, stderr or "不明なエラー", rate_limited


def summary_tag_for(status_entry):
    label = {
        "cache": "キャッシュ",
        "cache_legacy": "キャッシュ(legacy)",
        "cache_mismatch": "SHA1不一致→新規",
        "no_cache": "新規",
        "force": "強制新規",
    }
    return label.get(status_entry["action"], status_entry["action"])


def print_progress(idx, total, image_path, action_tag, result):
    if result["status"] == "failed":
        err = result["error"] or ""
        head = err.splitlines()[0] if err else ""
        print(f"  [{idx}/{total}] {image_path.name} ({action_tag}) 失敗: {head}", flush=True)
        return

    hits = []
    for pf in TARGET_PLATFORMS:
        c = len(result["platform_page_hits"][pf]) + len(result["platform_similar_hits"][pf])
        if c > 0:
            hits.append(f"{pf}×{c}")
    hit_str = ", ".join(hits) if hits else "PFヒット無し"
    print(
        f"  [{idx}/{total}] {image_path.name} ({action_tag}) → {hit_str}",
        flush=True,
    )


def resolve_display_path(dir_path):
    try:
        return str(dir_path.relative_to(Path.cwd()))
    except ValueError:
        return dir_path.name  # basename のみ


def aggregate(results):
    total = len(results)
    processed = [r for r in results if r["status"] != "failed"]
    failed = [r for r in results if r["status"] == "failed"]
    cached = [r for r in results if r["status"] == "cached"]
    processed_new = [r for r in results if r["status"] == "processed"]

    api_hit = [r for r in processed if r["pages_total"] > 0]
    page_hit = [r for r in processed if r["has_page_hit"]]
    similar_only_hit = [r for r in processed if r["has_similar_only_hit"]]
    full_match_images = [r for r in processed if r["full_total"] > 0]

    per_platform = {}
    for pf in TARGET_PLATFORMS:
        images_with_hit = 0
        unique_urls = set()
        for r in processed:
            page = r["platform_page_hits"][pf]
            sim = r["platform_similar_hits"][pf]
            if page or sim:
                images_with_hit += 1
            for h in page:
                unique_urls.add(h["url"])
            for h in sim:
                unique_urls.add(h["url"])
        per_platform[pf] = {
            "image_count": images_with_hit,
            "unique_url_count": len(unique_urls),
        }

    return {
        "total": total,
        "cached": len(cached),
        "processed_new": len(processed_new),
        "failed": len(failed),
        "api_hit_count": len(api_hit),
        "page_hit_count": len(page_hit),
        "similar_only_hit_count": len(similar_only_hit),
        "full_match_image_count": len(full_match_images),
        "per_platform": per_platform,
        "processed_count": len(processed),
    }


def percent(numerator, denominator):
    if denominator == 0:
        return "0%"
    return f"{numerator * 100 // denominator}%"


def generate_report(results, stats, dir_path, run_at):
    display_path = resolve_display_path(dir_path)
    lines = []
    lines.append("# AIMORI PoC 検証サマリ")
    lines.append("")
    lines.append(f"- **実行日時**: {run_at}")
    lines.append(f"- **対象**: `{display_path}`")
    lines.append(
        f"- **処理**: {stats['total']} 枚 "
        f"（キャッシュ {stats['cached']} / 新規API {stats['processed_new']} / 失敗 {stats['failed']}）"
    )
    lines.append("")

    lines.append("## 集計結果（画像単位）")
    lines.append("")
    lines.append("| 項目 | 値 |")
    lines.append("|------|----|")
    denom = stats["processed_count"]
    lines.append(
        f"| APIヒット率（掲載ページ1件以上） | "
        f"{stats['api_hit_count']}/{denom} ({percent(stats['api_hit_count'], denom)}) |"
    )
    lines.append(
        f"| ターゲットPF ページヒット率（強シグナル・転載/転売） | "
        f"{stats['page_hit_count']}/{denom} ({percent(stats['page_hit_count'], denom)}) |"
    )
    lines.append(
        f"| ターゲットPF 類似のみヒット率（模倣品候補） | "
        f"{stats['similar_only_hit_count']}/{denom} "
        f"({percent(stats['similar_only_hit_count'], denom)}) |"
    )
    lines.append(
        f"| 完全一致検出画像数 | {stats['full_match_image_count']}/{denom} "
        f"({percent(stats['full_match_image_count'], denom)}) |"
    )
    lines.append("")

    lines.append("## プラットフォーム別ヒット数（URL重複排除、画像間 unique）")
    lines.append("")
    lines.append("| PF | ヒット画像数 | Unique ヒットURL数 | ヒット率 |")
    lines.append("|----|-----------:|-----------------:|---------:|")
    for pf, info in stats["per_platform"].items():
        rate = percent(info["image_count"], denom)
        lines.append(
            f"| {pf} | {info['image_count']} | {info['unique_url_count']} | {rate} |"
        )
    lines.append("")

    lines.append("## 画像別詳細")
    lines.append("")
    lines.append("| ファイル | 推定ラベル | ステータス | 掲載 | 完全 | 部分 | 類似 | PF計 |")
    lines.append("|---------|----------|----------|----:|----:|----:|----:|----:|")
    status_ja = {"cached": "キャッシュ", "processed": "処理済", "failed": "失敗"}
    for r in results:
        label = r["best_guess"] or "-"
        lines.append(
            f"| {r['filename']} | {label} | {status_ja.get(r['status'], r['status'])} | "
            f"{r['pages_total']} | {r['full_total']} | {r['partial_total']} | "
            f"{r['similar_total']} | {r['target_url_count']} |"
        )
    lines.append("")

    lines.append("## 要注意URL一覧（手動確認用）")
    lines.append("")
    any_hits = False
    kind_icon = {"完全一致": "🔴", "部分一致": "🟡", "掲載": "🔵"}
    for r in results:
        if r["status"] == "failed":
            continue
        if not (r["has_page_hit"] or r["has_similar_only_hit"]):
            continue
        any_hits = True
        lines.append(f"### {r['filename']}")
        lines.append("")
        for pf in TARGET_PLATFORMS:
            for h in r["platform_page_hits"][pf]:
                icon = kind_icon.get(h["kind"], "")
                title = f" — {h['title']}" if h["title"] else ""
                lines.append(f"- {icon} [{pf}] {h['kind']} {h['url']}{title}")
            for h in r["platform_similar_hits"][pf]:
                lines.append(f"- ⚪ [{pf}] 類似画像 {h['url']}")
        lines.append("")
    if not any_hits:
        lines.append("（該当なし）")
        lines.append("")

    failures = [r for r in results if r["status"] == "failed"]
    if failures:
        lines.append("## エラー一覧")
        lines.append("")
        lines.append("| ファイル | エラー |")
        lines.append("|---------|--------|")
        for r in failures:
            err = (r["error"] or "").replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {r['filename']} | {err} |")
        lines.append("")

    lines.append("---")
    lines.append(
        "※ このレポートには第三者サイトのURL・ページタイトル・画像推定ラベル・APIエラー"
        "詳細が含まれます。共有前に内容をご確認ください。"
    )
    lines.append("")
    return "\n".join(lines)


def write_report(output_path, content):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")


def append_checkpoint(checkpoint_path, result):
    with checkpoint_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")


def print_final_summary(stats, output_path):
    print()
    print("=" * 60)
    print("検証完了")
    print(
        f"  画像数: {stats['total']} / 処理済: {stats['processed_count']} / "
        f"失敗: {stats['failed']}"
    )
    denom = stats["processed_count"]
    print(
        f"  APIヒット率: {stats['api_hit_count']}/{denom} "
        f"({percent(stats['api_hit_count'], denom)})"
    )
    print(
        f"  ターゲットPF ページヒット: {stats['page_hit_count']}/{denom} "
        f"({percent(stats['page_hit_count'], denom)})"
    )
    hit_platforms = [
        (pf, info["image_count"])
        for pf, info in stats["per_platform"].items()
        if info["image_count"] > 0
    ]
    if hit_platforms:
        for pf, cnt in hit_platforms:
            print(f"    {pf}: {cnt} 画像")
    print(f"  レポート: {output_path}")
    print("=" * 60)


def parse_args():
    parser = argparse.ArgumentParser(
        description="AIMORI PoC バッチ検証（ディレクトリ内の全画像を Vision API 逆画像検索し集計）"
    )
    parser.add_argument("image_dir", help="検証対象の画像ディレクトリ")
    parser.add_argument("--force", action="store_true", help="キャッシュを無視して再度APIを呼ぶ")
    parser.add_argument("--delay", type=float, default=1.0, help="API呼び出し間隔（秒、デフォルト1）")
    parser.add_argument("--max", type=int, dest="max_images", default=None,
                        help="最大処理枚数（先頭からこの枚数のみ処理）")
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT,
                        help=f"レポート出力先（デフォルト: {DEFAULT_REPORT}）")
    parser.add_argument("--yes", action="store_true", help="コスト確認プロンプトをスキップ")
    return parser.parse_args()


def main():
    args = parse_args()

    # 1. 引数バリデーション
    dir_path = Path(args.image_dir).resolve()
    if not dir_path.is_dir():
        die(f"エラー: ディレクトリが見つかりません: {args.image_dir}")

    output_path = Path(args.output).resolve()
    if not output_path.parent.exists():
        die(f"エラー: --output の親ディレクトリが存在しません: {output_path.parent}")

    # 2. 画像収集
    all_images = collect_images(dir_path)
    if not all_images:
        die(f"エラー: {dir_path} 内に対応拡張子の画像が見つかりません "
            f"({', '.join(sorted(IMAGE_EXTENSIONS))})", code=0)

    if args.max_images is not None and args.max_images > 0:
        images = all_images[: args.max_images]
    else:
        images = all_images

    print(f"対象ディレクトリ: {dir_path}")
    print(f"検出画像: {len(all_images)} 枚"
          + (f"（--max により {len(images)} 枚を処理）" if len(images) != len(all_images) else ""))

    # 3. safe_name 衝突検知（--force 有無を問わず必ず実行）
    detect_safe_name_collisions(images)

    # 4. API キー プリフライトチェック
    get_api_key()

    # 5. OUT_DIR 事前作成
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 6. プリフライトキャッシュスキャン
    print("キャッシュ状態を確認中...", flush=True)
    cache_statuses = scan_cache_status(images, force=args.force)
    uncached_count = sum(
        1 for s in cache_statuses if s["action"] in ("no_cache", "cache_mismatch", "force")
    )
    print(
        f"  キャッシュ利用: {sum(1 for s in cache_statuses if s['action'] in ('cache', 'cache_legacy'))} / "
        f"新規API: {uncached_count}",
        flush=True,
    )

    # 7. コスト警告
    confirm_cost(uncached_count, args.yes)

    # 8. 画像ループ
    checkpoint_path = OUT_DIR / f".batch_checkpoint.{os.getpid()}.jsonl"
    results = []
    aborted_by_429 = False
    interrupted = False

    print("\n=== 検証開始 ===")
    try:
        for idx, status_entry in enumerate(cache_statuses, start=1):
            image_path = status_entry["image"]
            sname = status_entry["sname"]
            action = status_entry["action"]
            action_tag = summary_tag_for(status_entry)

            if action == "cache_mismatch" and status_entry["note"]:
                print(f"  ⚠️ {status_entry['note']}: {image_path.name}", flush=True)

            if action == "cache":
                web = parse_web_json(sname)
                result = build_result_from_web(image_path, sname, "cached", web or {})
            elif action == "cache_legacy":
                web = parse_web_json(sname)
                result = build_result_from_web(image_path, sname, "cached", web or {})
                # 将来の衝突検知のためサイドカーをその場で書き出す
                try:
                    write_meta(sname, image_path)
                    print(f"  ℹ️ レガシーキャッシュ利用 → メタ生成: {image_path.name}", flush=True)
                except OSError as e:
                    print(f"  ⚠️ メタ書き込み失敗（無視）: {e}", flush=True)
            else:
                # 新規 API 呼び出し
                ok, err, rate_limited = run_subprocess_for(image_path)
                if rate_limited:
                    print(f"  🚨 HTTP 429 (レート制限/無料枠超過) を検知。バッチを中断します。",
                          flush=True)
                    results.append(failure_result(image_path, sname, err))
                    append_checkpoint(checkpoint_path, results[-1])
                    aborted_by_429 = True
                    break
                if not ok:
                    result = failure_result(image_path, sname, err)
                else:
                    web = parse_web_json(sname)
                    if web is None:
                        result = failure_result(
                            image_path, sname, "JSONファイルが生成されませんでした"
                        )
                    else:
                        result = build_result_from_web(image_path, sname, "processed", web)
                        try:
                            write_meta(sname, image_path)
                        except OSError as e:
                            print(f"  ⚠️ メタ書き込み失敗（無視）: {e}", flush=True)

                # API 呼び出し後はディレイ
                if args.delay > 0:
                    time.sleep(args.delay)

            results.append(result)
            append_checkpoint(checkpoint_path, result)
            print_progress(idx, len(cache_statuses), image_path, action_tag, result)

    except KeyboardInterrupt:
        interrupted = True
        print("\n中断シグナルを受信。ここまでの結果でレポートを生成します。", flush=True)

    # 9. レポート生成
    stats = aggregate(results)
    run_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report = generate_report(results, stats, dir_path, run_at)
    write_report(output_path, report)

    # 完了時: checkpoint を削除（中断/中断時は残す）
    if not aborted_by_429 and not interrupted:
        try:
            checkpoint_path.unlink(missing_ok=True)
        except OSError:
            pass

    print_final_summary(stats, output_path)

    if aborted_by_429:
        sys.exit(2)
    if interrupted:
        sys.exit(130)


if __name__ == "__main__":
    main()
