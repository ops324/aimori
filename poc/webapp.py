#!/usr/bin/env python3
"""
AIMORI PoC — Webアプリ版

ブラウザから画像を1枚アップロードし、Google Vision API Web Detection の結果を
HTMLで確認できる軽量ツール。reverse_search.py のロジックをそのまま再利用する。

使い方（ローカル）:
    pip install -r poc/requirements.txt
    export GOOGLE_VISION_API_KEY=<your-key>
    python3 poc/webapp.py
    → http://127.0.0.1:5000/ をブラウザで開く

使い方（Vercel）:
    vercel env add GOOGLE_VISION_API_KEY
    vercel env add WEBAPP_USER
    vercel env add WEBAPP_PASS
    vercel deploy --prod

認証:
  - 環境変数 WEBAPP_USER / WEBAPP_PASS を設定すると HTTP Basic 認証が有効になる。
  - 未設定の場合は認証なし（ローカル開発用）。

注意:
  - Vercel 環境では出力 JSON を /tmp に保存（エフェメラル）。
  - 依存: Flask（poc/requirements.txt）。reverse_search.py / batch_verify.py 自体は
    標準ライブラリのみで動く。
"""

import base64
import datetime
import json
import os
import sys
from functools import wraps
from pathlib import Path

from flask import Flask, Response, render_template_string, request
from werkzeug.exceptions import RequestEntityTooLarge

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from reverse_search import (  # noqa: E402
    OUT_DIR,
    VisionAPIError,
    build_request_body,
    call_vision_api,
    get_api_key,
    safe_name,
    summarize_web_detection,
)

# Vercel 環境ではファイルシステムが読み取り専用（/tmp のみ書き込み可）。
_VERCEL = bool(os.environ.get("VERCEL"))
WEBAPP_OUT_DIR = Path("/tmp/aimori_webapp") if _VERCEL else OUT_DIR / "webapp"
# raw 14MB は base64 化後に約 18.7MB となり、Vision API の 20MB 上限に収まる余裕を見た値。
MAX_CONTENT_LENGTH = 14 * 1024 * 1024
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

# モジュール最上位で解決（__main__ の外）。
# python3 poc/webapp.py 以外（flask run / gunicorn 等）では __name__ が "webapp" になり
# __main__ ブロックはスキップされるため、ここで解決しないと scan() 内で参照した時に
# NameError になる。未設定なら get_api_key() が die() で即終了する（CLIと同じfail-fast）。
API_KEY = get_api_key()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH


FORM_TMPL = """
<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>AIMORI PoC — 画像検索</title>
<style>
  body { font-family: sans-serif; max-width: 640px; margin: 40px auto; padding: 0 16px; }
  h1 { font-size: 1.3rem; }
  .note { color: #555; font-size: 0.9rem; }
  form { margin-top: 24px; }
  input[type=submit] { padding: 8px 20px; }
</style>
</head>
<body>
<h1>AIMORI PoC — 画像アップロード検索</h1>
<p class="note">画像を1枚アップロードすると、Google Vision API で
ターゲットPF（メルカリ・minne等）での転載・模倣品候補を確認できます。
このツールはローカル/内部利用専用です。</p>
<form method="POST" action="/scan" enctype="multipart/form-data">
  <input type="file" name="image" accept="image/*" required>
  <input type="submit" value="検索する">
</form>
</body>
</html>
"""

RESULT_TMPL = """
<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>検索結果 — {{ filename }}</title>
<style>
  body { font-family: sans-serif; max-width: 720px; margin: 40px auto; padding: 0 16px; }
  h1 { font-size: 1.2rem; }
  h2 { font-size: 1.05rem; margin-top: 28px; }
  .note { color: #555; font-size: 0.85rem; }
  table { border-collapse: collapse; margin: 8px 0; }
  td, th { border: 1px solid #ccc; padding: 4px 10px; text-align: left; }
  ul { padding-left: 20px; }
  li { margin-bottom: 6px; word-break: break-all; }
  a.back { display: inline-block; margin-top: 24px; }
</style>
</head>
<body>
<h1>検索結果: {{ filename }}</h1>

<h2>サマリ</h2>
<table>
  <tr><td>掲載ページ</td><td>{{ summary.pages|length }} 件</td></tr>
  <tr><td>完全一致画像</td><td>{{ summary.full|length }} 件</td></tr>
  <tr><td>部分一致画像</td><td>{{ summary.partial|length }} 件</td></tr>
  <tr><td>類似画像</td><td>{{ summary.similar|length }} 件</td></tr>
  <tr><td>★ ターゲットPFヒット合計</td><td>{{ summary.flagged_all }} 件</td></tr>
</table>

<h2>🚨 ターゲットPFヒット（掲載ページ・転載/転売の疑い）</h2>
{% if summary.flagged_pages %}
<ul>
  {% for pf, url, kind, title in summary.flagged_pages %}
  <li>[{{ pf }}] ({{ kind }}) <a href="{{ url }}" target="_blank" rel="noopener">{{ url }}</a>
    {% if title %}<br><span class="note">{{ title }}</span>{% endif %}</li>
  {% endfor %}
</ul>
{% else %}
<p class="note">該当なし</p>
{% endif %}

<h2>⚠️ 模倣品候補（ターゲットPFの類似画像のみ）</h2>
{% if summary.flagged_similar %}
<ul>
  {% for pf, url in summary.flagged_similar %}
  <li>[{{ pf }}] <a href="{{ url }}" target="_blank" rel="noopener">{{ url }}</a></li>
  {% endfor %}
</ul>
{% else %}
<p class="note">該当なし</p>
{% endif %}

<h2>【参考】その他掲載ページ</h2>
{% if summary.other_pages %}
<ul>
  {% for p in summary.other_pages %}
  <li><a href="{{ p.url }}" target="_blank" rel="noopener">{{ p.url }}</a>
    {% if p.pageTitle %}<br><span class="note">{{ p.pageTitle }}</span>{% endif %}</li>
  {% endfor %}
</ul>
{% else %}
<p class="note">該当なし</p>
{% endif %}

<h2>【類似画像 全件】（ターゲットPF問わず、CLIの --all 相当）</h2>
{% if summary.similar %}
<ul>
  {% for img in summary.similar %}
  <li><a href="{{ img.url }}" target="_blank" rel="noopener">{{ img.url }}</a></li>
  {% endfor %}
</ul>
{% else %}
<p class="note">該当なし</p>
{% endif %}

<p class="note">生レスポンス保存先: {{ out_path }}</p>
<a class="back" href="/">← 別の画像を検索する</a>
</body>
</html>
"""

ERROR_TMPL = """
<!doctype html>
<html lang="ja">
<head><meta charset="utf-8"><title>エラー</title></head>
<body style="font-family: sans-serif; max-width: 640px; margin: 40px auto; padding: 0 16px;">
<h1>エラー</h1>
<p>{{ message }}</p>
<a href="/">← 戻る</a>
</body>
</html>
"""


@app.before_request
def auth_required():
    user = os.environ.get("WEBAPP_USER", "")
    passwd = os.environ.get("WEBAPP_PASS", "")
    if not (user and passwd):
        return  # 認証未設定（ローカル開発用）
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            u, p = decoded.split(":", 1)
            if u == user and p == passwd:
                return
        except Exception:
            pass
    return Response(
        "認証が必要です",
        401,
        {"WWW-Authenticate": 'Basic realm="AIMORI PoC"'},
    )


@app.errorhandler(RequestEntityTooLarge)
def handle_too_large(_e):
    return render_template_string(
        ERROR_TMPL,
        message=f"ファイルサイズが大きすぎます（上限 {MAX_CONTENT_LENGTH // (1024*1024)}MB）。",
    ), 413


@app.route("/")
def index():
    return render_template_string(FORM_TMPL)


@app.route("/scan", methods=["POST"])
def scan():
    f = request.files.get("image")
    if f is None or f.filename == "":
        return render_template_string(ERROR_TMPL, message="画像ファイルを選択してください。"), 400

    ext = Path(f.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        return render_template_string(
            ERROR_TMPL, message=f"対応していない拡張子です: {ext or '(拡張子なし)'}"
        ), 400

    data = f.read()
    if not data:
        return render_template_string(ERROR_TMPL, message="空のファイルです。"), 400

    b64 = base64.b64encode(data).decode("ascii")
    body = build_request_body({"content": b64})

    try:
        result = call_vision_api(API_KEY, body)
    except VisionAPIError as e:
        return render_template_string(ERROR_TMPL, message=str(e)), 502
    except Exception as e:  # 想定外のエラーもクラッシュさせず友好的に返す
        return render_template_string(ERROR_TMPL, message=f"予期しないエラー: {e}"), 502

    responses = result.get("responses", [{}])
    resp0 = responses[0] if responses else {}
    if "error" in resp0:
        err = resp0["error"]
        return render_template_string(
            ERROR_TMPL, message=f"APIエラー: {err.get('message', err)}"
        ), 502

    web = resp0.get("webDetection", {})
    summary = summarize_web_detection(web)

    WEBAPP_OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S_%f")
    sname = f"{stamp}_{safe_name(f.filename)}"
    out_path = WEBAPP_OUT_DIR / f"{sname}.web.json"
    out_path.write_text(json.dumps(web, ensure_ascii=False, indent=2), encoding="utf-8")

    return render_template_string(
        RESULT_TMPL,
        filename=f.filename,
        summary=summary,
        out_path=str(out_path.relative_to(_HERE)),
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
