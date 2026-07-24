#!/usr/bin/env python3
"""
AIMORI PoC — リランキング評価ハーネス（フェーズ2 効果検証用）

人手ラベル（0/1）付きの候補セットに対して、
  - Vision のカテゴリ順（ベースライン）
  - rerank.py の自前スコア順（phash / clip / dino）
を Recall@k / mAP で比較し、レポート（Markdown + CSV + 並置サムネHTML）を出力する。

★ 重要な但し書き（レポート冒頭に必ず明記）:
  本ハーネスの指標は、標本サイズ（クエリ数・陽性数・取得成功率）に強く依存する。
  小標本では mAP / Recall@k は方向性の目安に過ぎず、本番運用の閾値は提案しない。
  目安として クエリ ≥5・陽性 ≥20 が揃うまでは指標を「暫定」として扱う。

入力ラベル (JSONL, 1行1候補):
  {"query": "poc/test_images/book.jpg", "source": "poc/out/book.jpg.web.json",
   "url": "https://.../x.jpg", "label": 1}
  - query/source は rerank 対象を特定するキー。label は 1=侵害/一致 の陽性、0=無関係。
  - 雛形は poc/eval_labels.example.jsonl を参照。

使い方:
    python3 poc/eval_rerank.py --labels poc/out/eval_labels.jsonl --method phash
    python3 poc/eval_rerank.py --labels ... --method clip --ks 1,3,5,10

依存: phash 評価は Pillow のみ。clip/dino 評価は poc/requirements-ml.txt。
"""

import argparse
import csv
import html
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rerank import rerank  # noqa: E402
from reverse_search import _SEVERITY_ORDER  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "out"
MIN_QUERIES = 5
MIN_POSITIVES = 20

# Vision バケット → ベースライン序列（小さいほど上位＝重要）。rerank の対象3バケットのみ。
_BUCKET_ORDER = {"full": 0, "partial": 1, "similar": 2}


# ---------------------------------------------------------------------------
# ラベル読み込み
# ---------------------------------------------------------------------------

def load_labels(path):
    """JSONL を (query, source) 単位のグループに束ねる。戻り値: {(query,source): {url: label}}"""
    groups = defaultdict(dict)
    for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"警告: {path}:{lineno} を JSON として解釈できません（スキップ）: {e}", file=sys.stderr)
            continue
        key = (rec["query"], rec["source"])
        groups[key][rec["url"]] = int(rec["label"])
    return groups


# ---------------------------------------------------------------------------
# 指標
# ---------------------------------------------------------------------------

def recall_at_k(ranked_labels, total_positives, k):
    """ranked_labels: 順位順の 0/1 列。total_positives: そのクエリの全陽性数。"""
    if total_positives == 0:
        return None
    hit = sum(ranked_labels[:k])
    return hit / total_positives


def average_precision(ranked_labels):
    """AP。陽性が無ければ None。"""
    positives = sum(ranked_labels)
    if positives == 0:
        return None
    hits = 0
    score_sum = 0.0
    for i, lbl in enumerate(ranked_labels, 1):
        if lbl:
            hits += 1
            score_sum += hits / i
    return score_sum / positives


def baseline_order(candidates):
    """Vision カテゴリ順（ベースライン）に candidates を並べた URL 列。
    candidates: rerank() の ranked（各要素に bucket/url/score）。score は無視し bucket 順のみ使用。
    同カテゴリ内は元の順序（＝Vision の関連度順）を安定維持。"""
    indexed = list(enumerate(candidates))
    indexed.sort(key=lambda t: (_BUCKET_ORDER.get(t[1]["bucket"], 99), t[0]))
    return [c["url"] for _, c in indexed]


def labels_in_order(url_order, label_map):
    """url_order に沿って 0/1 列を返す。ラベル未付与 URL は評価対象外として除外。"""
    return [label_map[u] for u in url_order if u in label_map]


# ---------------------------------------------------------------------------
# 評価本体
# ---------------------------------------------------------------------------

def evaluate(labels_path, method, ks):
    groups = load_labels(labels_path)
    per_query = []
    total_positives_all = 0

    for (query, source), label_map in groups.items():
        if not Path(query).is_file() or not Path(source).is_file():
            print(f"警告: query/source が見つからずスキップ: {query} / {source}", file=sys.stderr)
            continue
        result = rerank(query, source, method=method, verbose=False)
        cands = result["ranked"]

        total_pos = sum(label_map.values())
        total_positives_all += total_pos

        # ベースライン（Vision 順）と自前スコア順、それぞれの 0/1 列
        base_labels = labels_in_order(baseline_order(cands), label_map)
        # 自前スコア順: score=None（未取得）は末尾。ラベル付き URL のみ残す。
        rer_urls = [c["url"] for c in cands]  # rerank() は既にスコア降順+未取得末尾
        rer_labels = labels_in_order(rer_urls, label_map)

        row = {
            "query": query,
            "source": source,
            "labeled": len(label_map),
            "positives": total_pos,
            "fetch_rate": result["fetch_success_rate"],
            "base_ap": average_precision(base_labels),
            "rer_ap": average_precision(rer_labels),
        }
        for k in ks:
            row[f"base_r@{k}"] = recall_at_k(base_labels, total_pos, k)
            row[f"rer_r@{k}"] = recall_at_k(rer_labels, total_pos, k)
        row["_base_labels"] = base_labels
        row["_rer_labels"] = rer_labels
        row["_ranked"] = cands
        row["_label_map"] = label_map
        per_query.append(row)

    return per_query, total_positives_all


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _fmt(v):
    return f"{v:.3f}" if isinstance(v, float) else ("-" if v is None else str(v))


def build_report(per_query, total_positives, method, ks, labels_path):
    n_q = len(per_query)
    provisional = n_q < MIN_QUERIES or total_positives < MIN_POSITIVES
    lines = []
    lines.append(f"# リランキング評価レポート — method=`{method}`")
    lines.append("")
    lines.append(f"- ラベル: `{labels_path}`")
    lines.append(f"- クエリ数: **{n_q}** / 総陽性数: **{total_positives}**")
    if provisional:
        lines.append("")
        lines.append(f"> ⚠️ **暫定 (PROVISIONAL)**: 標本が下限（クエリ ≥{MIN_QUERIES}・陽性 ≥{MIN_POSITIVES}）に"
                     f"満たない。以下の mAP / Recall@k は方向性の目安であり、統計的に信頼できる値ではない。"
                     f"**本番運用の閾値は本レポートから決定しないこと。**")
    lines.append("")
    lines.append("## 集計（全クエリ平均）")
    lines.append("")
    header = ["指標", "Vision順(baseline)", f"{method}順(rerank)"]
    rows = [["mAP",
             _fmt(_mean([r["base_ap"] for r in per_query])),
             _fmt(_mean([r["rer_ap"] for r in per_query]))]]
    for k in ks:
        rows.append([f"Recall@{k}",
                     _fmt(_mean([r[f"base_r@{k}"] for r in per_query])),
                     _fmt(_mean([r[f"rer_r@{k}"] for r in per_query]))])
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for r in rows:
        lines.append("| " + " | ".join(r) + " |")
    lines.append("")
    lines.append("## クエリ別")
    lines.append("")
    qh = ["query", "labeled", "positives", "fetch率", "base mAP", f"{method} mAP"]
    lines.append("| " + " | ".join(qh) + " |")
    lines.append("|" + "|".join(["---"] * len(qh)) + "|")
    for r in per_query:
        lines.append("| " + " | ".join([
            Path(r["query"]).name, str(r["labeled"]), str(r["positives"]),
            _fmt(r["fetch_rate"]), _fmt(r["base_ap"]), _fmt(r["rer_ap"]),
        ]) + " |")
    lines.append("")
    lines.append("## 読み方")
    lines.append("- **Recall@k を主指標**とする（侵害の見逃し最小化）。mAP は補助。")
    lines.append("- rerank 列が baseline 列を上回れば、その手法が Vision のカテゴリ順より"
                 "陽性を上位に集められている＝二次リランキングの効果があることを示す。")
    lines.append("- `fetch率` が低いクエリは、サードパーティ画像の取得失敗で評価母数が痩せている点に注意。")
    return "\n".join(lines), provisional


def write_csv(per_query, ks, path):
    fields = ["query", "source", "labeled", "positives", "fetch_rate",
              "base_ap", "rer_ap"] + \
             [f"base_r@{k}" for k in ks] + [f"rer_r@{k}" for k in ks]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in per_query:
            w.writerow(r)


def write_thumbs_html(per_query, method, path):
    """クエリごとに、baseline 順 vs rerank 順の上位を並置。画像は third-party URL 直リンク
    （no-referrer・壊れは非表示）。ローカルに画像は保存しない。"""
    parts = ["<meta charset='utf-8'><title>rerank thumbnails</title>",
             "<style>body{font-family:sans-serif}img{width:96px;height:96px;object-fit:cover;"
             "border:1px solid #ccc;margin:2px}.pos{outline:3px solid #d33}"
             ".row{display:flex;flex-wrap:wrap;align-items:center;gap:2px;margin:6px 0}"
             ".lbl{width:120px;font-size:12px;color:#555}</style>",
             f"<h1>rerank thumbnails — method={html.escape(method)}</h1>",
             "<p>赤枠 = 陽性ラベル(1)。上段 Vision順 / 下段 rerank順。</p>"]
    for r in per_query:
        parts.append(f"<h3>{html.escape(Path(r['query']).name)} "
                     f"(陽性 {r['positives']}/{r['labeled']})</h3>")
        label_map = r["_label_map"]
        # baseline 順の URL 列
        base_urls = [u for u in baseline_order(r["_ranked"]) if u in label_map]
        rer_urls = [c["url"] for c in r["_ranked"] if c["url"] in label_map]
        for name, urls in [("Vision順", base_urls[:12]), (f"{method}順", rer_urls[:12])]:
            parts.append("<div class='row'><span class='lbl'>" + name + "</span>")
            for u in urls:
                cls = "pos" if label_map.get(u) == 1 else ""
                eu = html.escape(u, quote=True)
                parts.append(f"<img class='{cls}' src='{eu}' referrerpolicy='no-referrer' "
                             f"loading='lazy' onerror=\"this.style.display='none'\">")
            parts.append("</div>")
    Path(path).write_text("\n".join(parts), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(
        description="リランキング評価ハーネス (AIMORI PoC / フェーズ2検証)")
    parser.add_argument("--labels", required=True, help="ラベル JSONL のパス")
    parser.add_argument("--method", choices=["phash", "clip", "dino"], default="phash")
    parser.add_argument("--ks", default="1,3,5,10", help="Recall@k の k（カンマ区切り）")
    parser.add_argument("--outdir", default=str(OUT_DIR), help="出力先ディレクトリ")
    args = parser.parse_args()

    if not Path(args.labels).is_file():
        print(f"エラー: ラベルファイルが見つかりません: {args.labels}\n"
              f"  雛形: poc/eval_labels.example.jsonl", file=sys.stderr)
        sys.exit(1)
    ks = [int(x) for x in args.ks.split(",") if x.strip()]

    per_query, total_pos = evaluate(args.labels, args.method, ks)
    if not per_query:
        print("エラー: 評価対象のクエリがありません（ラベル/画像/レスポンスのパスを確認）。",
              file=sys.stderr)
        sys.exit(1)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    report_md, provisional = build_report(per_query, total_pos, args.method, ks, args.labels)
    (outdir / "rerank_report.md").write_text(report_md, encoding="utf-8")
    write_csv(per_query, ks, outdir / "rerank_report.csv")
    write_thumbs_html(per_query, args.method, outdir / "rerank_thumbs.html")

    print(report_md)
    print(f"\n出力: {outdir/'rerank_report.md'} / rerank_report.csv / rerank_thumbs.html")
    if provisional:
        print("※ 暫定レポート（標本が下限未満）。判定には使わないこと。", file=sys.stderr)


if __name__ == "__main__":
    main()
