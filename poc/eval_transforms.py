#!/usr/bin/env python3
"""
AIMORI PoC — 変換ロバストネス分離性評価（pHash vs aHash）

「フェーズ2の二次リランキングに、安価な知覚ハッシュで足りるか。どの加工で破綻するか」を
**distractor（無関係画像）付き・ランク/マージンベース**で測る、完全ローカル・再現可能な実験。

独立監査の指摘を反映した設計:
  - 陽性のみの自己類似カーブは分離性を測れない → 各 original に対し、コーパス内の他画像を
    distractor プールとして必ず併置し、「陽性が全 distractor を上回るか（retrieval）」で評価する。
  - 「pHash評価」を名乗るには本物の pHash（imagehash / DCT）が必須。imagehash 未導入時は
    **エラーで停止**（aHash を pHash と誤ラベルしない）。aHash は対照として明示ラベルで併記する。
  - pHash と aHash は同じ正規化ハミング尺度なので相互比較可（0.5≒無相関床）。
    CLIP/DINO は尺度が異なるため本スクリプトでは扱わない（比較するならランク指標か手法別較正で別途）。

コーパス: ローカルの実画像（既定は book.jpg ＋ poc/out/rerank_cache/ の取得済み候補画像）。
  各画像を順に「original」とし、加工版を陽性、他画像を distractor として all-vs-all で評価する。

使い方:
    python3 poc/eval_transforms.py
    python3 poc/eval_transforms.py --corpus poc/test_images/book.jpg poc/out/rerank_cache/*.img
    python3 poc/eval_transforms.py --outdir poc/out

依存: Pillow, numpy, imagehash（pip install imagehash / requirements-ml.txt）。torch 不要。
"""

import argparse
import csv
import glob
import statistics
import sys
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rerank import _ahash_bits, _hamming  # aHash は rerank に一元化済み  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "out"
CACHE_DIR = OUT_DIR / "rerank_cache"


def require(mod, pip_name=None):
    try:
        return __import__(mod)
    except ImportError:
        print(f"エラー: '{mod}' が必要です。 pip install {pip_name or mod}\n"
              f"  （pHash評価には本物の imagehash が必須。aHash で代用しない設計です）",
              file=sys.stderr)
        sys.exit(2)


# ---------------------------------------------------------------------------
# 画像ロードと加工（Pillow）
# ---------------------------------------------------------------------------

def load_image(path):
    from PIL import Image
    return Image.open(path).convert("RGB")


def _jpeg(img, quality):
    from PIL import Image
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def _crop_frac(img, frac):
    w, h = img.size
    dx, dy = int(w * frac / 2), int(h * frac / 2)
    return img.crop((dx, dy, w - dx, h - dy))


def _collage(img):
    """original を一回り大きい別背景キャンバスに貼る（コラージュ/転載加工の模擬）。"""
    from PIL import Image
    w, h = img.size
    canvas = Image.new("RGB", (int(w * 1.4), int(h * 1.4)), (200, 30, 30))
    canvas.paste(img, (int(w * 0.2), int(h * 0.2)))
    return canvas


def _occlude(img):
    from PIL import Image, ImageDraw
    im = img.copy()
    d = ImageDraw.Draw(im)
    w, h = im.size
    d.rectangle([int(w*0.55), int(h*0.55), int(w*0.9), int(h*0.9)], fill=(0, 0, 0))
    return im


def build_transforms():
    """名前 -> (Image -> Image)。製品で問題になる加工（色変え・反転・クロップ・コラージュ）を中心に。"""
    from PIL import Image, ImageEnhance
    return {
        "identity": lambda im: im,  # サニティ（陽性≈1.0のはず）
        "jpeg_q50": lambda im: _jpeg(im, 50),
        "jpeg_q30": lambda im: _jpeg(im, 30),
        "bright_0.8": lambda im: ImageEnhance.Brightness(im).enhance(0.8),
        "bright_1.2": lambda im: ImageEnhance.Brightness(im).enhance(1.2),
        "contrast_1.3": lambda im: ImageEnhance.Contrast(im).enhance(1.3),
        "saturate_0.5": lambda im: ImageEnhance.Color(im).enhance(0.5),
        "hue_shift": lambda im: _hue_shift(im, 60),
        "grayscale": lambda im: im.convert("L").convert("RGB"),
        "resize_50": lambda im: im.resize((max(1, im.size[0]//2), max(1, im.size[1]//2))).resize(im.size),
        "blur": lambda im: _blur(im, 2),
        "hflip": lambda im: im.transpose(Image.FLIP_LEFT_RIGHT),
        "rotate_5": lambda im: im.rotate(5, expand=False),
        "rotate_15": lambda im: im.rotate(15, expand=False),
        "crop_10": lambda im: _crop_frac(im, 0.10),
        "crop_25": lambda im: _crop_frac(im, 0.25),
        "collage": _collage,
        "occlude": _occlude,
    }


def _hue_shift(img, deg):
    from PIL import Image
    shift = int(deg / 360 * 255)
    h, s, v = img.convert("HSV").split()
    h = h.point(lambda x: (x + shift) % 256)
    return Image.merge("HSV", (h, s, v)).convert("RGB")


def _blur(img, radius):
    from PIL import ImageFilter
    return img.filter(ImageFilter.GaussianBlur(radius))


# ---------------------------------------------------------------------------
# スコアラ（両方とも正規化ハミング類似度 = 同一尺度で相互比較可）
# ---------------------------------------------------------------------------

class PhashReal:
    name = "phash(imagehash/DCT)"

    def __init__(self):
        self._ih = require("imagehash")
        self._bits = None

    def bits(self, img):
        h = self._ih.phash(img, hash_size=16)  # 256bit DCT
        flat = h.hash.flatten().tolist()
        self._bits = len(flat)
        return flat

    def sim(self, a_bits, b_img):
        b = self.bits(b_img)
        n = min(len(a_bits), len(b))
        return 1.0 - _hamming(a_bits[:n], b[:n]) / n


class AhashCtl:
    name = "ahash(control)"

    def bits(self, img):
        return _ahash_bits(img)  # rerank と同一実装（16x16 平均ハッシュ）

    def sim(self, a_bits, b_img):
        b = self.bits(b_img)
        n = min(len(a_bits), len(b))
        return 1.0 - _hamming(a_bits[:n], b[:n]) / n


# ---------------------------------------------------------------------------
# 評価本体
# ---------------------------------------------------------------------------

def evaluate(corpus_paths, outdir):
    imgs = []
    for p in corpus_paths:
        try:
            imgs.append((p, load_image(p)))
        except Exception as e:  # noqa: BLE001
            print(f"警告: 読み込み失敗をスキップ: {p} ({type(e).__name__})", file=sys.stderr)
    if len(imgs) < 3:
        print("エラー: コーパス画像が3枚未満です（distractor を成立させられません）。", file=sys.stderr)
        sys.exit(1)

    transforms = build_transforms()
    scorers = [PhashReal(), AhashCtl()]

    # rows[(method, transform)] = list of per-original dict
    rows = {(s.name, t): [] for s in scorers for t in transforms}

    for oi, (opath, oimg) in enumerate(imgs):
        distractors = [im for j, (_, im) in enumerate(imgs) if j != oi]
        for s in scorers:
            o_bits = s.bits(oimg)
            neg_scores = [s.sim(o_bits, d) for d in distractors]
            neg_max = max(neg_scores)
            neg_p95 = _percentile(neg_scores, 95)
            for tname, tf in transforms.items():
                pos = s.sim(o_bits, tf(oimg))
                rows[(s.name, tname)].append({
                    "pos": pos,
                    "neg_max": neg_max,
                    "neg_p95": neg_p95,
                    "retrieved": pos > neg_max,          # 全 distractor を上回るか
                    "margin": pos - neg_max,
                })

    return _aggregate(rows, scorers, transforms, len(imgs)), scorers, transforms


def _percentile(vals, pct):
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * pct / 100
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _aggregate(rows, scorers, transforms, n):
    agg = {}
    for (method, tname), lst in rows.items():
        agg[(method, tname)] = {
            "mean_pos": statistics.mean(r["pos"] for r in lst),
            "mean_negmax": statistics.mean(r["neg_max"] for r in lst),
            "mean_margin": statistics.mean(r["margin"] for r in lst),
            "retrieval_rate": sum(r["retrieved"] for r in lst) / len(lst),
        }
    return agg


# ---------------------------------------------------------------------------
# レポート
# ---------------------------------------------------------------------------

def write_report(agg, scorers, transforms, n, outdir):
    method_names = [s.name for s in scorers]
    tnames = list(transforms)

    lines = ["# 変換ロバストネス分離性評価 — pHash vs aHash", ""]
    lines.append(f"- コーパス: ローカル実画像 **{n} 枚**（各画像を original とし、他 {n-1} 枚を distractor）")
    lines.append(f"- 尺度: 正規化ハミング類似度（0-1、0.5≒無相関床）。pHash/aHash は同尺度なので相互比較可。")
    lines.append("- **retrieval_rate** = 加工版（陽性）が全 distractor を上回った original の割合"
                 "（＝この加工でも元画像を取り違えず引ける率）。**これが主指標**。")
    lines.append("- margin = 陽性スコア − 最大 distractor スコア（正なら分離、負なら埋没）。")
    lines.append("")
    lines.append("> ⚠️ **方向性シグナル（DIRECTIONAL）**: コーパスは surrogate（実際のターゲットPF加工"
                 "リポストではない）。本結果は「どの加工で安価ハッシュが破綻するか」の傾向把握と手法の"
                 "妥当性検証が目的であり、本番閾値やアーキ最終判断の根拠ではない。実判定には実PF陽性が必要"
                 "（下部『本番評価の仕様』参照）。")
    lines.append("")

    # 主表: retrieval_rate（手法×加工）
    lines.append("## 主表: retrieval_rate（高いほど頑健。加工しても元を引ける率）")
    lines.append("")
    lines.append("| 加工 | " + " | ".join(method_names) + " |")
    lines.append("|" + "|".join(["---"] * (len(method_names) + 1)) + "|")
    for t in tnames:
        cells = [f"{agg[(m, t)]['retrieval_rate']:.2f}" for m in method_names]
        lines.append(f"| {t} | " + " | ".join(cells) + " |")
    lines.append("")

    # 補助表: mean margin
    lines.append("## 補助表: 平均マージン（陽性 − 最大distractor。正=分離）")
    lines.append("")
    lines.append("| 加工 | " + " | ".join(method_names) + " |")
    lines.append("|" + "|".join(["---"] * (len(method_names) + 1)) + "|")
    for t in tnames:
        cells = [f"{agg[(m, t)]['mean_margin']:+.3f}" for m in method_names]
        lines.append(f"| {t} | " + " | ".join(cells) + " |")
    lines.append("")

    # 自動所見
    lines.append("## 自動所見")
    ph = scorers[0].name
    ah = scorers[1].name
    robust = [t for t in tnames if agg[(ph, t)]["retrieval_rate"] >= 0.9 and t != "identity"]
    broken = [t for t in tnames if agg[(ph, t)]["retrieval_rate"] < 0.5]
    gap = [t for t in tnames if agg[(ph, t)]["retrieval_rate"] - agg[(ah, t)]["retrieval_rate"] >= 0.2]
    lines.append(f"- **pHash が頑健な加工**（retrieval_rate ≥ 0.9）: {', '.join(robust) or '（なし）'}")
    lines.append(f"  → この範囲は**安価な pHash で回収可能**。CLIP を持ち出す必要は薄い。")
    lines.append(f"- **pHash が破綻する加工**（retrieval_rate < 0.5）: {', '.join(broken) or '（なし）'}")
    lines.append(f"  → ここは pHash では不十分。CLIP/DINO 等の埋め込み、または加工前正規化"
                 f"（反転・回転のマルチハッシュ）が候補。**次段で検証すべき領域**。")
    lines.append(f"- **pHash が aHash を明確に上回る加工**（retrieval_rate 差 ≥ 0.2）: {', '.join(gap) or '（なし）'}")
    lines.append(f"  → 「pHash」を名乗らず aHash で測っていたら、この差ぶんだけ**過小評価**し"
                 f"「安価ハッシュはダメ→CLIP必須」と誤結論する危険があった（監査指摘の実証）。")
    lines.append("")

    # 本番評価の仕様（decision ではなく spec）
    lines.append("## 本番評価の仕様（この結果を『判定』に格上げするために必要なもの）")
    lines.append("- **実 PF 陽性データ**: メルカリ/pixiv 等で実際に加工転載された作品と原画のペア。"
                 "現状の保存レスポンスには存在しない（前回監査の確定事項）。")
    lines.append(f"- **標本下限**: `eval_rerank.py` の PROVISIONAL 閾値（クエリ ≥5・陽性 ≥20）以上。")
    lines.append("- **distractor 混入の統制**: 本コーパスは同一書籍の別版など near-dup を含み得るため、"
                 "max distractor が過大評価される。実運用では既知 near-dup を除くか別クラス化する。")
    lines.append("- **CLIP を比較する場合**: 生スコアは尺度が異なるため比較不可。ランク指標"
                 "（Recall@k/mAP、`eval_rerank.py`）または手法別較正（各手法の null 分布に対する z 値）で行う。")
    return "\n".join(lines)


def write_csv(agg, scorers, transforms, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["method", "transform", "retrieval_rate", "mean_pos", "mean_negmax", "mean_margin"])
        for s in scorers:
            for t in transforms:
                a = agg[(s.name, t)]
                w.writerow([s.name, t, f"{a['retrieval_rate']:.4f}", f"{a['mean_pos']:.4f}",
                            f"{a['mean_negmax']:.4f}", f"{a['mean_margin']:.4f}"])


def main():
    parser = argparse.ArgumentParser(
        description="変換ロバストネス分離性評価 pHash vs aHash (AIMORI PoC / フェーズ2検証)")
    parser.add_argument("--corpus", nargs="*", default=None,
                        help="コーパス画像パス（既定: book.jpg + poc/out/rerank_cache/*.img）")
    parser.add_argument("--outdir", default=str(OUT_DIR))
    args = parser.parse_args()

    if args.corpus:
        paths = []
        for pat in args.corpus:
            paths.extend(glob.glob(pat))
    else:
        paths = ["poc/test_images/book.jpg"] + sorted(glob.glob(str(CACHE_DIR / "*.img")))
    paths = [p for p in paths if Path(p).is_file()]
    if not paths:
        print("エラー: コーパス画像が見つかりません。先に poc/rerank.py を一度実行して"
              "候補画像をキャッシュするか、--corpus を指定してください。", file=sys.stderr)
        sys.exit(1)

    print(f"■ コーパス {len(paths)} 枚で評価中…（pHash vs aHash、distractor 付き）")
    agg, scorers, transforms = evaluate(paths, args.outdir)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    report = write_report(agg, scorers, transforms, len(paths), outdir)
    (outdir / "transform_eval.md").write_text(report, encoding="utf-8")
    write_csv(agg, scorers, transforms, outdir / "transform_eval.csv")
    print(report)
    print(f"\n出力: {outdir/'transform_eval.md'} / transform_eval.csv")


if __name__ == "__main__":
    main()
