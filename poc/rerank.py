#!/usr/bin/env python3
"""
AIMORI PoC — オフライン二次リランキング（フェーズ2 効果検証用）

Vision API `WEB_DETECTION` が返した候補画像（full / partial / similar バケット）に対し、
クエリ画像との「本物の 0-1 類似度スコア」を自前で計算し、再ランキングする。

これは完全にオフラインのローカル実行ツールであり、webapp.py / Vercel 経路には一切依存しない。
新しい Vision API 呼び出しは行わず、保存済みレスポンス (poc/out/*.web.json) を入力に使う。

手法（--method）:
  phash     : 知覚ハッシュ（デフォルト／torch 不要・軽量）。imagehash があれば使い、
              無ければ本ファイル内の自前 aHash 実装にフォールバックする。
  multihash : phash に加工前正規化（左右反転×±5/10/15°回転の増幅照合）を足した版。
              torch 不要のまま反転・回転に不変な照合ができる（docs/RERANK_FINDINGS.md 参照）。
  clip      : open_clip の CLIP ViT-B/32 埋め込みのコサイン類似度（要 requirements-ml.txt）。
  dino      : transformers の DINOv2 ViT-S/14 埋め込みのコサイン類似度（要 requirements-ml.txt）。

対象バケット: full / partial / similar（各要素が画像 URL を持つ）。
  pages（pagesWithMatchingImages）はページ URL であり画像 URL ではないため対象外（設計判断）。

使い方:
    python3 poc/rerank.py --query poc/test_images/book.jpg --from poc/out/book.jpg.web.json
    python3 poc/rerank.py --method clip --query <img> --from <resp.json> --json out.json

依存: phash はほぼ標準ライブラリのみ（画像デコードに Pillow を使用。無ければ理由を表示して終了）。
      clip / dino は poc/requirements-ml.txt を参照。
"""

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# reverse_search を再利用（候補バケットの抽出をそこに一元化しておく）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from reverse_search import summarize_web_detection  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "out"
CACHE_DIR = OUT_DIR / "rerank_cache"

# 候補画像 URL を持つバケット。pages は画像 URL でないため含めない。
CANDIDATE_BUCKETS = ("full", "partial", "similar")
FETCH_TIMEOUT = 20
# 一部ホストは referer / UA チェックで 403 を返すため、ブラウザ相当のヘッダを付ける。
FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    # 逆参照を送らない（既存 UI の referrerpolicy=no-referrer 相当）
    "Referer": "",
}


# ---------------------------------------------------------------------------
# 候補の抽出
# ---------------------------------------------------------------------------

def extract_candidates(web):
    """webDetection dict から (bucket, url) のリストを返す。重複 URL は先勝ちで除去。"""
    summary = summarize_web_detection(web)
    seen = set()
    candidates = []
    for bucket in CANDIDATE_BUCKETS:
        for img in summary.get(bucket, []) or []:
            url = (img or {}).get("url", "")
            if url and url not in seen:
                seen.add(url)
                candidates.append({"bucket": bucket, "url": url})
    return candidates


def load_web_json(path):
    """保存済みレスポンスを読む。analyze_one は webDetection をそのまま保存しているが、
    生の annotate レスポンス（responses[0].webDetection）を渡された場合も許容する。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "responses" in data:  # 生 annotate レスポンス
        resp = (data.get("responses") or [{}])[0]
        return resp.get("webDetection", {}) or {}
    return data  # 既に webDetection dict


# ---------------------------------------------------------------------------
# 画像取得（キャッシュ + 成否記録）
# ---------------------------------------------------------------------------

def cache_path_for(url):
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    return CACHE_DIR / f"{h}.img"


def fetch_image_bytes(url):
    """(bytes | None, status_str) を返す。失敗は None + 理由。out/ は gitignore 下。"""
    cache = cache_path_for(url)
    if cache.is_file():
        return cache.read_bytes(), "cached"
    req = urllib.request.Request(url, headers={k: v for k, v in FETCH_HEADERS.items() if v})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        return None, f"http_{e.code}"
    except urllib.error.URLError as e:
        return None, f"neterr:{e.reason}"
    except Exception as e:  # noqa: BLE001 — 取得失敗は握って「未取得」に倒す
        return None, f"error:{type(e).__name__}"
    if not data:
        return None, "empty"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(data)
    return data, "fetched"


# ---------------------------------------------------------------------------
# 手法: pHash（デフォルト・軽量）
# ---------------------------------------------------------------------------

def _require_pillow():
    try:
        from PIL import Image  # noqa: F401
        return True
    except ImportError:
        return False


def _ahash_bits(image, hash_size=16):
    """Pillow Image -> 平均ハッシュのビット列(list[int])。imagehash 非依存の自前実装。
    回転/反転差は正規化しない（near-dup 前提）。hash_size=16 で 256bit。"""
    from PIL import Image
    img = image.convert("L").resize((hash_size, hash_size), Image.LANCZOS)
    px = list(img.getdata())
    avg = sum(px) / len(px)
    return [1 if p >= avg else 0 for p in px]


def _hamming(a, b):
    return sum(1 for x, y in zip(a, b) if x != y)


class PhashScorer:
    """平均ハッシュ距離を 0-1 類似度に正規化して返す。1.0 が最も似ている。"""

    name = "phash"

    def __init__(self):
        if not _require_pillow():
            die_dep("phash", "Pillow")
        # imagehash があればより堅牢な pHash を使う。無ければ自前 aHash。
        try:
            import imagehash  # noqa: F401
            self._impl = "imagehash"
        except ImportError:
            self._impl = "ahash"
        self._bits = 256  # hash_size=16 → 256bit

    def _hash(self, img_bytes):
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(img_bytes))
        if self._impl == "imagehash":
            import imagehash
            h = imagehash.phash(img, hash_size=16)  # 256bit
            self._bits = h.hash.size
            return h.hash.flatten().tolist()
        return _ahash_bits(img)

    def query_repr(self, query_bytes):
        return self._hash(query_bytes)

    def score(self, query_repr, cand_bytes):
        cand = self._hash(cand_bytes)
        n = min(len(query_repr), len(cand))
        dist = _hamming(query_repr[:n], cand[:n])
        return 1.0 - (dist / n)  # 0..1、1 が同一


class MultiHashScorer(PhashScorer):
    """(a) 加工前正規化。クエリ画像を {左右反転 × ±5/10/15°回転}（計14変種）に増幅し、
    候補との最大 pHash 類似度を取る。反転・回転に不変な照合を torch 無しで実現する。
    実測（docs/RERANK_FINDINGS.md）: 反転 0.00→0.90、15°回転 0.14→0.90 と回復。
    クロップ・コラージュ（幾何構造が変わる加工）は回復しない残余領域。"""

    name = "multihash"
    ANGLES = (-15, -10, -5, 0, 5, 10, 15)

    def _hash_pil(self, img):
        if self._impl == "imagehash":
            import imagehash
            h = imagehash.phash(img, hash_size=16)
            self._bits = h.hash.size
            return h.hash.flatten().tolist()
        return _ahash_bits(img)

    def _variants(self, img):
        from PIL import Image
        bases = [img, img.transpose(Image.FLIP_LEFT_RIGHT)]
        return [b if a == 0 else b.rotate(a, expand=False) for b in bases for a in self.ANGLES]

    def query_repr(self, query_bytes):
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(query_bytes)).convert("RGB")
        return [self._hash_pil(v) for v in self._variants(img)]  # 参照側だけ増幅

    def score(self, query_repr, cand_bytes):
        from io import BytesIO
        from PIL import Image
        cand = self._hash_pil(Image.open(BytesIO(cand_bytes)).convert("RGB"))
        best = 0.0
        for a in query_repr:
            n = min(len(a), len(cand))
            best = max(best, 1.0 - _hamming(a[:n], cand[:n]) / n)
        return best


# ---------------------------------------------------------------------------
# 手法: CLIP / DINOv2（重い。requirements-ml.txt が必要）
# ---------------------------------------------------------------------------

class _EmbeddingScorer:
    """埋め込みベクトルのコサイン類似度。CLIP / DINOv2 の共通土台。"""

    def _embed(self, img_bytes):  # -> list[float] (L2 正規化済み)
        raise NotImplementedError

    def query_repr(self, query_bytes):
        return self._embed(query_bytes)

    def score(self, query_repr, cand_bytes):
        cand = self._embed(cand_bytes)
        # 双方 L2 正規化済み前提 → 内積 = コサイン類似度。[-1,1] を [0,1] に写像。
        dot = sum(a * b for a, b in zip(query_repr, cand))
        return (dot + 1.0) / 2.0


class ClipScorer(_EmbeddingScorer):
    name = "clip"

    def __init__(self):
        try:
            import open_clip
            import torch
        except ImportError:
            die_dep("clip", "open_clip_torch torch")
        self._torch = torch
        self._model, _, self._preprocess = open_clip.create_model_and_transforms(
            "ViT-B-32", pretrained="laion2b_s34b_b79k"
        )
        self._model.eval()

    def _embed(self, img_bytes):
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(img_bytes)).convert("RGB")
        x = self._preprocess(img).unsqueeze(0)
        with self._torch.no_grad():
            v = self._model.encode_image(x)
            v = v / v.norm(dim=-1, keepdim=True)
        return v.squeeze(0).tolist()


class DinoScorer(_EmbeddingScorer):
    name = "dino"

    def __init__(self):
        try:
            import torch
            from transformers import AutoImageProcessor, AutoModel
        except ImportError:
            die_dep("dino", "torch transformers")
        self._torch = torch
        self._proc = AutoImageProcessor.from_pretrained("facebook/dinov2-small")
        self._model = AutoModel.from_pretrained("facebook/dinov2-small")
        self._model.eval()

    def _embed(self, img_bytes):
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(img_bytes)).convert("RGB")
        inputs = self._proc(images=img, return_tensors="pt")
        with self._torch.no_grad():
            out = self._model(**inputs)
            v = out.last_hidden_state[:, 0]  # CLS トークン
            v = v / v.norm(dim=-1, keepdim=True)
        return v.squeeze(0).tolist()


SCORERS = {"phash": PhashScorer, "multihash": MultiHashScorer,
           "clip": ClipScorer, "dino": DinoScorer}


def die_dep(method, pip_pkgs):
    print(
        f"エラー: --method {method} には追加依存が必要です。\n"
        f"  pip install {pip_pkgs}\n"
        f"  （まとめて: pip install -r poc/requirements-ml.txt）",
        file=sys.stderr,
    )
    sys.exit(2)


# ---------------------------------------------------------------------------
# リランキング本体
# ---------------------------------------------------------------------------

def rerank(query_path, web_json_path, method="phash", verbose=True):
    """クエリ画像と保存済みレスポンスから、候補を自前スコアで再ランキングして dict を返す。"""
    query_bytes = Path(query_path).read_bytes()
    web = load_web_json(web_json_path)
    candidates = extract_candidates(web)

    scorer = SCORERS[method]()
    query_repr = scorer.query_repr(query_bytes)

    results = []
    fetch_ok = 0
    for i, c in enumerate(candidates, 1):
        img_bytes, status = fetch_image_bytes(c["url"])
        entry = {"bucket": c["bucket"], "url": c["url"], "fetch": status, "score": None}
        if img_bytes is not None:
            try:
                entry["score"] = round(float(scorer.score(query_repr, img_bytes)), 4)
                fetch_ok += 1
            except Exception as e:  # noqa: BLE001 — デコード不能画像等は score=None のまま
                entry["fetch"] = f"decode_err:{type(e).__name__}"
        if verbose:
            s = entry["score"]
            print(f"  [{i:>2}/{len(candidates)}] {entry['fetch']:<12} "
                  f"score={s if s is not None else '  -  '}  ({c['bucket']}) {c['url'][:80]}")
        results.append(entry)

    # スコアありは降順、スコアなし（未取得）は末尾。安定ソート。
    scored = sorted(
        [r for r in results if r["score"] is not None],
        key=lambda r: r["score"], reverse=True,
    )
    unscored = [r for r in results if r["score"] is None]
    ranked = scored + unscored

    return {
        "query": str(query_path),
        "source": str(web_json_path),
        "method": method,
        "phash_impl": getattr(scorer, "_impl", None),
        "candidate_count": len(candidates),
        "fetch_success": fetch_ok,
        "fetch_success_rate": round(fetch_ok / len(candidates), 3) if candidates else None,
        "ranked": ranked,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Vision候補をオフラインで自前スコア再ランキング (AIMORI PoC / フェーズ2検証)"
    )
    parser.add_argument("--query", required=True, help="クエリ画像（登録作品）のパス")
    parser.add_argument("--from", dest="web_json", required=True,
                        help="保存済み Vision レスポンス (poc/out/*.web.json)")
    parser.add_argument("--method", choices=list(SCORERS), default="phash",
                        help="類似度手法（デフォルト: phash）")
    parser.add_argument("--json", dest="json_out", default=None,
                        help="結果 JSON の保存先（省略時は標準出力のサマリのみ）")
    args = parser.parse_args()

    if not Path(args.query).is_file():
        print(f"エラー: クエリ画像が見つかりません: {args.query}", file=sys.stderr)
        sys.exit(1)
    if not Path(args.web_json).is_file():
        print(f"エラー: レスポンス JSON が見つかりません: {args.web_json}", file=sys.stderr)
        sys.exit(1)

    print(f"■ method={args.method}  query={args.query}")
    print(f"  source={args.web_json}")
    result = rerank(args.query, args.web_json, method=args.method)

    print(f"\n[サマリ] 候補 {result['candidate_count']} 件 / "
          f"取得成功 {result['fetch_success']} 件 "
          f"(成功率 {result['fetch_success_rate']})  method={result['method']}"
          + (f" impl={result['phash_impl']}" if result['phash_impl'] else ""))
    print("  ↓ 再ランキング上位（自前スコア降順）")
    for i, r in enumerate([x for x in result["ranked"] if x["score"] is not None][:10], 1):
        print(f"  {i:>2}. score={r['score']}  ({r['bucket']}) {r['url'][:80]}")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n結果 JSON 保存: {args.json_out}")


if __name__ == "__main__":
    main()
