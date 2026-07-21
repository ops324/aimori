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
    explain_response_error,
    get_api_key,
    safe_name,
    sort_flagged,
    summarize_web_detection,
)

# Vercel 環境ではファイルシステムが読み取り専用（/tmp のみ書き込み可）。
_VERCEL = bool(os.environ.get("VERCEL"))
WEBAPP_OUT_DIR = Path("/tmp/aimori_webapp") if _VERCEL else OUT_DIR / "webapp"
# raw 14MB は base64 化後に約 18.7MB となり、Vision API の 20MB 上限に収まる余裕を見た値。
MAX_CONTENT_LENGTH = 14 * 1024 * 1024
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

# 結果画面に「登録画像」をインライン(data URI)表示する際の上限（raw bytes）。
# Vercel の serverless レスポンスには ~4.5MB の上限があるため、大きい画像はプレビューを
# 省略する（base64化で約1.37倍になるため 1.5MB → 約2.05MBのdata URIに収まる）。
INLINE_PREVIEW_MAX = int(1.5 * 1024 * 1024)
# data URI 用の MIME マップ。"image/jpg" は正式なMIMEタイプではないため専用に定義する。
EXT_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}

# モジュール最上位で解決（__main__ の外）。
# python3 poc/webapp.py 以外（flask run / gunicorn 等）では __name__ が "webapp" になり
# __main__ ブロックはスキップされるため、ここで解決しないと scan() 内で参照した時に
# NameError になる。未設定なら get_api_key() が die() で即終了する（CLIと同じfail-fast）。
API_KEY = get_api_key()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH


# LP（リポジトリ直下 index.html）のOKLCHデザイントークンを移植した共通CSS。
# 単一.pyファイル構成を維持するため poc/static/ には分離しない（Vercelの
# @vercel/python バンドル・/poc ルーティングとの相性を考慮、詳細はSPEC.md参照）。
BASE_CSS = """
:root{
  --color-paper:       oklch(97.5% 0.012 85);
  --color-paper-2:     oklch(95%   0.016 82);
  --color-ink:         oklch(27%   0.018 60);
  --color-ink-soft:    oklch(44%   0.016 60);
  --color-ink-faint:   oklch(60%   0.014 60);
  --color-line:        oklch(87%   0.018 78);
  --color-accent:      oklch(46%   0.086 250);
  --color-accent-ink:  oklch(38%   0.090 250);
  --color-accent-soft: oklch(93%   0.030 250);
  --color-clay:        oklch(64%   0.115 45);
  --color-clay-soft:   oklch(94%   0.032 55);
  --color-focus:       oklch(46%   0.086 250);
  --color-paper-on-accent: oklch(98% 0.010 85);

  --font-display: "Shippori Mincho", "Hiragino Mincho ProN", "Yu Mincho", serif;
  --font-body: "Zen Kaku Gothic New", "Hiragino Kaku Gothic ProN", "Yu Gothic", system-ui, sans-serif;

  --text-xs: .78rem;
  --text-sm: .88rem;
  --text-base: 1.02rem;
  --text-lg: 1.2rem;
  --text-xl: 1.45rem;
  --text-2xl: 1.85rem;

  --space-2xs: .25rem;
  --space-xs: .5rem;
  --space-sm: .75rem;
  --space-md: 1.25rem;
  --space-lg: 2rem;
  --space-xl: 3.25rem;

  --rule: 1px;
  --radius-sm: 6px;
  --radius-md: 12px;
  --radius-pill: 999px;

  --ease-out: cubic-bezier(.22,.61,.36,1);
}

*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}
body{
  font-family:var(--font-body);
  background:var(--color-paper);
  color:var(--color-ink);
  font-size:var(--text-base);
  line-height:1.85;
  letter-spacing:.01em;
  -webkit-font-smoothing:antialiased;
}
h1,h2,h3{font-family:var(--font-display);font-weight:600;line-height:1.35;letter-spacing:.02em;overflow-wrap:anywhere;min-width:0}
p{overflow-wrap:anywhere}
a{color:var(--color-accent-ink);text-underline-offset:.22em;text-decoration-thickness:1px}

.wrap{width:100%;max-width:46rem;margin-inline:auto;padding:0 var(--space-md)}

:focus-visible{outline:2px solid var(--color-focus);outline-offset:3px;border-radius:3px}

.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:.5em;
  font-family:var(--font-body);font-weight:700;font-size:var(--text-base);
  line-height:1.2;text-decoration:none;white-space:nowrap;
  padding:.85em 1.6em;border-radius:var(--radius-pill);
  border:1.5px solid transparent;cursor:pointer;
  transition:transform .18s var(--ease-out), background-color .18s var(--ease-out), box-shadow .18s var(--ease-out), border-color .18s var(--ease-out);
}
.btn-primary{background:var(--color-accent);color:var(--color-paper-on-accent);box-shadow:0 1px 0 oklch(30% 0.09 250 / .35)}
.btn-primary:hover{background:var(--color-accent-ink);transform:translateY(-2px);box-shadow:0 8px 22px oklch(38% 0.09 250 / .22)}
.btn-primary:active{transform:translateY(0)}
.btn-primary:disabled{opacity:.6;cursor:wait;transform:none;box-shadow:none}
.btn-ghost{background:transparent;color:var(--color-accent-ink);border-color:var(--color-line)}
.btn-ghost:hover{background:var(--color-accent-soft);border-color:var(--color-accent);transform:translateY(-2px)}
.btn-ghost:active{transform:translateY(0)}

.nav{position:sticky;top:0;z-index:50;background:oklch(97.5% 0.012 85 / .82);backdrop-filter:blur(10px);border-bottom:var(--rule) solid var(--color-line)}
.nav-in{display:flex;align-items:center;justify-content:space-between;gap:var(--space-md);padding-block:var(--space-sm)}
.brand{display:flex;align-items:baseline;gap:.55rem;text-decoration:none;color:var(--color-ink)}
.brand .mark{font-family:var(--font-display);font-weight:700;font-size:1.35rem;letter-spacing:.08em}
.brand .mark b{color:var(--color-accent-ink)}
.brand .tag{font-size:var(--text-xs);color:var(--color-ink-faint);letter-spacing:.04em}
.badge{font-size:var(--text-xs);color:var(--color-clay);border:var(--rule) solid var(--color-clay-soft);background:var(--color-clay-soft);padding:.3em .9em;border-radius:var(--radius-pill)}
@media(max-width:560px){.brand .tag{display:none}}

main{padding-block:var(--space-xl)}
h1{font-size:var(--text-xl);margin-bottom:var(--space-sm)}
.note{color:var(--color-ink-soft);font-size:var(--text-sm)}

/* 重要度バッジ（一致の種類による色分け。数値スコアは存在しないためカテゴリ表示） */
.sev-badge{display:inline-block;font-size:var(--text-xs);font-weight:700;padding:.25em .8em;border-radius:var(--radius-pill);border:var(--rule) solid transparent;white-space:nowrap}
.sev-full{background:var(--color-clay-soft);color:var(--color-clay);border-color:var(--color-clay)}
.sev-partial{background:var(--color-accent-soft);color:var(--color-accent-ink);border-color:var(--color-accent)}
.sev-listed,.sev-similar{background:var(--color-paper-2);color:var(--color-ink-soft);border-color:var(--color-line)}

/* サムネイルグリッド（一致画像の視覚的な確認用） */
.thumb-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:var(--space-sm);margin:var(--space-sm) 0}
.thumb-card{background:var(--color-paper-2);border:var(--rule) solid var(--color-line);border-radius:var(--radius-md);padding:var(--space-xs);overflow:hidden}
.thumb{display:block;width:100%;aspect-ratio:1/1;object-fit:cover;border-radius:var(--radius-sm);background:var(--color-paper-2)}
.thumb-card figcaption{font-size:var(--text-xs);margin-top:var(--space-2xs);display:flex;flex-wrap:wrap;align-items:center;gap:.4em;color:var(--color-ink-soft)}
.thumb-card .thumb-link{color:var(--color-accent-ink);font-weight:500}
.thumb-card.img-broken .thumb{display:none}
"""


FORM_TMPL = """
<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AIMORI PoC — 画像検索</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Shippori+Mincho:wght@400;600;700&family=Zen+Kaku+Gothic+New:wght@400;500;700&display=swap" rel="stylesheet">
<style>
""" + BASE_CSS + """
  .dropzone {
    border: 1.5px dashed var(--color-line);
    border-radius: var(--radius-md);
    padding: var(--space-lg);
    text-align: center;
    margin-top: var(--space-md);
    transition: border-color .18s var(--ease-out), background-color .18s var(--ease-out);
  }
  .dropzone.dragover { border-color: var(--color-accent); background: var(--color-accent-soft); }
  .dropzone label { display: block; cursor: pointer; color: var(--color-accent-ink); font-weight: 700; }
  .dropzone input[type=file] {
    position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
    overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0;
  }
  .preview { max-width: 200px; max-height: 200px; margin: var(--space-md) auto 0; border-radius: var(--radius-sm); display: none; }
  .field-error { color: var(--color-clay); font-size: var(--text-sm); margin-top: var(--space-xs); min-height: 1.2em; }
  form { margin-top: var(--space-lg); }
  .submit-row { margin-top: var(--space-md); }
  [aria-busy="true"]::after {
    content: ""; display: inline-block; width: 1em; height: 1em; margin-left: .6em;
    border: 2px solid currentColor; border-top-color: transparent; border-radius: 50%;
    vertical-align: -0.2em;
  }
  @media(prefers-reduced-motion:no-preference) {
    [aria-busy="true"]::after { animation: spin 0.8s linear infinite; }
  }
  @media(prefers-reduced-motion:reduce) {
    [aria-busy="true"]::after { display: none; }
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .scan-overlay {
    position: fixed; inset: 0; z-index: 100;
    background: oklch(97.5% 0.012 85 / .92); backdrop-filter: blur(2px);
    display: flex; align-items: center; justify-content: center; padding: var(--space-md);
  }
  .scan-overlay[hidden] { display: none; }
  .scan-overlay-card {
    background: var(--color-paper); border: var(--rule) solid var(--color-line);
    border-radius: var(--radius-md); padding: var(--space-xl); max-width: 26rem;
    text-align: center; box-shadow: 0 12px 30px oklch(30% 0.02 60 / .18);
  }
  .scan-overlay-spinner {
    width: 2.4rem; height: 2.4rem; margin: 0 auto var(--space-md);
    border: 3px solid var(--color-line); border-top-color: var(--color-accent); border-radius: 50%;
  }
  @media(prefers-reduced-motion:no-preference) {
    .scan-overlay-spinner { animation: spin 0.9s linear infinite; }
  }
  .scan-overlay-card h2 { font-size: var(--text-lg); margin-bottom: var(--space-xs); }
  .scan-overlay-card p { color: var(--color-ink-soft); font-size: var(--text-sm); }
</style>
</head>
<body>
<header class="nav">
  <div class="wrap nav-in">
    <a class="brand" href="." aria-label="AIMORI ホーム">
      <span class="mark">AI<b>MORI</b></span>
      <span class="tag">アイデアを守るAI</span>
    </a>
    <span class="badge">PoC・内部ツール</span>
  </div>
</header>
<main class="wrap">
<h1>画像アップロード検索</h1>
<p class="note">画像を1枚アップロードすると、Google Vision API で
ターゲットPF（メルカリ・minne等）での転載・模倣品候補を確認できます。
このツールはローカル/内部利用専用です。</p>
<form method="POST" action="scan" enctype="multipart/form-data" id="scan-form">
  <div class="dropzone" id="dropzone">
    <label for="image-file">クリックまたはドラッグ&amp;ドロップで画像を選択</label>
    <input type="file" id="image-file" name="image" accept="image/*" required>
    <img class="preview" id="preview" alt="選択した画像のプレビュー">
    <p class="field-error" id="field-error" aria-live="polite"></p>
  </div>
  <p class="submit-row">
    <button type="submit" class="btn btn-primary" id="scan-submit">検索する</button>
  </p>
</form>
<div class="scan-overlay" id="scan-overlay" role="status" aria-live="polite" hidden>
  <div class="scan-overlay-card">
    <div class="scan-overlay-spinner" aria-hidden="true"></div>
    <h2>ネットを巡回して照合中…</h2>
    <p>数十秒かかることがあります。ページを閉じずにお待ちください。</p>
  </div>
</div>
</main>
<script>
(function () {
  var dropzone = document.getElementById('dropzone');
  var input = document.getElementById('image-file');
  var preview = document.getElementById('preview');
  var fieldError = document.getElementById('field-error');
  var previewUrl = null;
  var ALLOWED_EXT = ['.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp'];
  var MAX_SIZE = 14 * 1024 * 1024;

  function handleFile(file) {
    fieldError.textContent = '';
    if (!file) { return; }
    var parts = file.name.split('.');
    var ext = '.' + (parts.length > 1 ? parts.pop().toLowerCase() : '');
    if (ALLOWED_EXT.indexOf(ext) === -1) {
      fieldError.textContent = '対応していない拡張子です: ' + ext;
    } else if (file.size > MAX_SIZE) {
      fieldError.textContent = 'ファイルサイズが大きすぎます（上限14MB）。';
    }
    if (previewUrl) { URL.revokeObjectURL(previewUrl); }
    previewUrl = URL.createObjectURL(file);
    preview.src = previewUrl;
    preview.style.display = 'block';
  }

  input.addEventListener('change', function () {
    handleFile(input.files[0]);
  });
  dropzone.addEventListener('dragover', function (e) {
    e.preventDefault();
    dropzone.classList.add('dragover');
  });
  dropzone.addEventListener('dragleave', function () {
    dropzone.classList.remove('dragover');
  });
  dropzone.addEventListener('drop', function (e) {
    e.preventDefault();
    dropzone.classList.remove('dragover');
    if (e.dataTransfer.files.length) {
      input.files = e.dataTransfer.files;
      handleFile(input.files[0]);
    }
  });

  var overlay = document.getElementById('scan-overlay');

  document.getElementById('scan-form').addEventListener('submit', function () {
    var btn = document.getElementById('scan-submit');
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    btn.textContent = '検索中…（数秒〜数十秒かかります）';
    overlay.hidden = false;
    document.body.style.overflow = 'hidden';
  });

  // 戻るボタン等で bfcache からこのページが復元された場合、送信中の見た目
  // （オーバーレイ表示・ボタン無効化）が固まったまま残ってしまうのを防ぐ。
  window.addEventListener('pageshow', function () {
    var btn = document.getElementById('scan-submit');
    overlay.hidden = true;
    document.body.style.overflow = '';
    btn.disabled = false;
    btn.removeAttribute('aria-busy');
    btn.textContent = '検索する';
  });
})();
</script>
</body>
</html>
"""

RESULT_TMPL = """
<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>検索結果 — {{ filename }}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Shippori+Mincho:wght@400;600;700&family=Zen+Kaku+Gothic+New:wght@400;500;700&display=swap" rel="stylesheet">
<style>
""" + BASE_CSS + """
  h2 { font-size: var(--text-lg); margin-top: var(--space-lg); margin-bottom: var(--space-xs); }
  .stats {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: var(--space-sm); margin: var(--space-md) 0;
  }
  .stat {
    background: var(--color-paper-2); border: var(--rule) solid var(--color-line);
    border-radius: var(--radius-md); padding: var(--space-sm) var(--space-md);
  }
  .stat-num { display: block; font-family: var(--font-display); font-size: var(--text-2xl); color: var(--color-ink); }
  .stat-label { font-size: var(--text-xs); color: var(--color-ink-faint); }
  .stat-highlight { background: var(--color-accent-soft); border-color: var(--color-accent); }
  .stat-highlight .stat-num { color: var(--color-accent-ink); }
  .est-label { margin-top: var(--space-md); }
  .entity-tags { list-style: none; display: flex; flex-wrap: wrap; gap: .4em; margin: var(--space-xs) 0 var(--space-md); padding: 0; }
  .entity-tags .tag { background: var(--color-accent-soft); color: var(--color-accent-ink); border-radius: var(--radius-pill); padding: .3em .9em; font-size: var(--text-xs); }
  ul.result-list { padding-left: 20px; }
  ul.result-list li { margin-bottom: 6px; word-break: break-all; }
  a.back { display: inline-block; margin-top: var(--space-lg); }
  .sec-note { margin-bottom: var(--space-xs); }
  .compare-strip { margin: var(--space-md) 0; }
  .compare-strip .thumb { width: 140px; aspect-ratio: 1/1; }
  .goodnews {
    background: var(--color-accent-soft); border: var(--rule) solid var(--color-accent);
    border-radius: var(--radius-md); padding: var(--space-lg); margin: var(--space-md) 0;
  }
  .goodnews h2 { margin-top: 0; }
  .goodnews p { margin-top: var(--space-xs); }
  details.result-details { margin: var(--space-xs) 0 var(--space-md); }
  details.result-details summary { cursor: pointer; color: var(--color-accent-ink); font-weight: 500; font-size: var(--text-sm); padding: .3em 0; }
  details.result-details ul.result-list { margin-top: var(--space-xs); }
</style>
</head>
<body>
<header class="nav">
  <div class="wrap nav-in">
    <a class="brand" href="." aria-label="AIMORI ホーム">
      <span class="mark">AI<b>MORI</b></span>
      <span class="tag">アイデアを守るAI</span>
    </a>
    <span class="badge">PoC・内部ツール</span>
  </div>
</header>
<main class="wrap">
{% set SEV_CLASS = {"完全一致":"sev-full","部分一致":"sev-partial","掲載":"sev-listed","類似":"sev-similar"} %}
{% macro sev_badge(kind) %}<span class="sev-badge {{ SEV_CLASS.get(kind,'sev-similar') }}">{{ kind }}</span>{% endmacro %}

<h1>検索結果: {{ filename }}</h1>

<div class="stats">
  <div class="stat"><span class="stat-num">{{ summary.pages|length }}</span><span class="stat-label">掲載ページ</span></div>
  <div class="stat"><span class="stat-num">{{ summary.full|length }}</span><span class="stat-label">完全一致画像</span></div>
  <div class="stat"><span class="stat-num">{{ summary.partial|length }}</span><span class="stat-label">部分一致画像</span></div>
  <div class="stat"><span class="stat-num">{{ summary.similar|length }}</span><span class="stat-label">類似画像</span></div>
  <div class="stat stat-highlight"><span class="stat-num">{{ summary.flagged_all }}</span><span class="stat-label">★ ターゲットPFヒット合計</span></div>
</div>

{% if summary.best_guess %}<p class="est-label">推定: <strong>{{ summary.best_guess }}</strong></p>{% endif %}
{% if summary.entities %}
<ul class="entity-tags">
  {% for e in summary.entities[:5] %}{% if e.description %}<li class="tag">{{ e.description }}</li>{% endif %}{% endfor %}
</ul>
{% endif %}

{% if uploaded_uri %}
<figure class="compare-strip">
  <img class="thumb" src="{{ uploaded_uri }}" alt="アップロードした登録画像">
  <figcaption class="note">アップロード画像（登録した作品）</figcaption>
</figure>
{% else %}
<p class="note">（画像プレビューは、大きなファイルのため省略しています）</p>
{% endif %}

{% if summary.flagged_all == 0 %}
<section class="goodnews">
  <h2><span aria-hidden="true">✅</span> ターゲットPFでの転載・模倣は見つかりませんでした</h2>
  <p>現時点で、メルカリ・minne等のターゲットPFでの転載・模倣品候補は検出されませんでした。</p>
  <p class="note">ただし、これは「存在しない」ことの保証ではありません。検索エンジンに未インデックスの出品や、加工された転載は見つからない場合があります。定期的な再チェックをおすすめします。</p>
  {% if summary.other_pages %}<p class="note">なお、ターゲットPF以外での掲載ページが {{ summary.other_pages|length }} 件見つかっています（下記【参考】をご確認ください）。</p>{% endif %}
</section>
{% else %}

<h2><span aria-hidden="true">🚨</span> ターゲットPFヒット（掲載ページ・転載/転売の疑い）</h2>
<p class="note sec-note">重要度（一致の種類による）順に表示</p>
{% if flagged_pages %}
<ul class="result-list">
  {% for pf, url, kind, title in flagged_pages %}
  <li>[{{ pf }}] {{ sev_badge(kind) }} <a href="{{ url }}" target="_blank" rel="noopener">{{ title or url }}</a></li>
  {% endfor %}
</ul>
{% else %}
<p class="note">該当なし</p>
{% endif %}

<h2><span aria-hidden="true">🎯</span> ターゲットPF直接一致（画像そのものが完全/部分一致としてCDN上で検出）</h2>
<p class="note sec-note">重要度（一致の種類による）順に表示</p>
{% if flagged_direct %}
<div class="thumb-grid">
  {% for pf, url, kind in flagged_direct %}
  <figure class="thumb-card">
    <img class="thumb" src="{{ url }}" alt="[{{ pf }}] {{ kind }} の一致画像"
         loading="lazy" referrerpolicy="no-referrer"
         onerror="this.closest('.thumb-card').classList.add('img-broken')">
    <figcaption>[{{ pf }}] {{ sev_badge(kind) }}
      <a class="thumb-link" href="{{ url }}" target="_blank" rel="noopener">画像を開く</a>
    </figcaption>
  </figure>
  {% endfor %}
</div>
<details class="result-details"><summary>URL一覧をすべて表示</summary>
<ul class="result-list">
  {% for pf, url, kind in flagged_direct %}
  <li>[{{ pf }}] (画像直接:{{ kind }}) <a href="{{ url }}" target="_blank" rel="noopener">{{ url }}</a></li>
  {% endfor %}
</ul>
</details>
{% else %}
<p class="note">該当なし</p>
{% endif %}

<h2><span aria-hidden="true">⚠️</span> 模倣品候補（ターゲットPFの類似画像のみ）</h2>
{% if summary.flagged_similar %}
<div class="thumb-grid">
  {% for pf, url in summary.flagged_similar %}
  <figure class="thumb-card">
    <img class="thumb" src="{{ url }}" alt="[{{ pf }}] 類似画像"
         loading="lazy" referrerpolicy="no-referrer"
         onerror="this.closest('.thumb-card').classList.add('img-broken')">
    <figcaption>[{{ pf }}] {{ sev_badge('類似') }}
      <a class="thumb-link" href="{{ url }}" target="_blank" rel="noopener">画像を開く</a>
    </figcaption>
  </figure>
  {% endfor %}
</div>
<details class="result-details"><summary>URL一覧をすべて表示</summary>
<ul class="result-list">
  {% for pf, url in summary.flagged_similar %}
  <li>[{{ pf }}] <a href="{{ url }}" target="_blank" rel="noopener">{{ url }}</a></li>
  {% endfor %}
</ul>
</details>
{% else %}
<p class="note">該当なし</p>
{% endif %}

{% endif %}

<h2>【参考】その他掲載ページ</h2>
{% if summary.other_pages %}
<ul class="result-list">
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
<div class="thumb-grid">
  {% for img in summary.similar %}
  <figure class="thumb-card">
    <img class="thumb" src="{{ img.url }}" alt="類似画像"
         loading="lazy" referrerpolicy="no-referrer"
         onerror="this.closest('.thumb-card').classList.add('img-broken')">
    <figcaption><a class="thumb-link" href="{{ img.url }}" target="_blank" rel="noopener">画像を開く</a></figcaption>
  </figure>
  {% endfor %}
</div>
<details class="result-details"><summary>URL一覧をすべて表示</summary>
<ul class="result-list">
  {% for img in summary.similar %}
  <li><a href="{{ img.url }}" target="_blank" rel="noopener">{{ img.url }}</a></li>
  {% endfor %}
</ul>
</details>
{% else %}
<p class="note">該当なし</p>
{% endif %}

<p class="note">生レスポンス保存先: {{ out_path }}</p>
<a class="back btn btn-ghost" href=".">← 別の画像を検索する</a>
</main>
</body>
</html>
"""

ERROR_TMPL = """
<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>エラー — AIMORI PoC</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Shippori+Mincho:wght@400;600;700&family=Zen+Kaku+Gothic+New:wght@400;500;700&display=swap" rel="stylesheet">
<style>
""" + BASE_CSS + """
</style>
</head>
<body>
<header class="nav">
  <div class="wrap nav-in">
    <a class="brand" href="." aria-label="AIMORI ホーム">
      <span class="mark">AI<b>MORI</b></span>
      <span class="tag">アイデアを守るAI</span>
    </a>
    <span class="badge">PoC・内部ツール</span>
  </div>
</header>
<main class="wrap">
<h1>エラー</h1>
<p>{{ message }}</p>
<a class="btn btn-ghost" href=".">← 戻る</a>
</main>
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
    resp = Response(
        render_template_string(
            ERROR_TMPL, message="認証が必要です。ユーザー名とパスワードを入力してください。"
        ),
        401,
    )
    resp.headers["WWW-Authenticate"] = 'Basic realm="AIMORI PoC"'
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


@app.errorhandler(RequestEntityTooLarge)
def handle_too_large(_e):
    return render_template_string(
        ERROR_TMPL,
        message=f"ファイルサイズが大きすぎます（上限 {MAX_CONTENT_LENGTH // (1024*1024)}MB）。",
    ), 413


# "/poc/" 系はVercel本番で /poc 配下にマウントするためのエイリアス（vercel.json参照）。
# テンプレート内リンクは相対パスなので、"/" と "/poc/" のどちらでアクセスしても機能する。
@app.route("/")
@app.route("/poc/")
def index():
    return render_template_string(FORM_TMPL)


@app.route("/scan", methods=["POST"])
@app.route("/poc/scan", methods=["POST"])
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
        return render_template_string(
            ERROR_TMPL, message=explain_response_error(resp0["error"])
        ), 502

    web = resp0.get("webDetection", {})
    summary = summarize_web_detection(web)

    WEBAPP_OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S_%f")
    sname = f"{stamp}_{safe_name(f.filename)}"
    out_path = WEBAPP_OUT_DIR / f"{sname}.web.json"
    out_path.write_text(json.dumps(web, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        out_path_display = str(out_path.relative_to(_HERE))
    except ValueError:
        # Vercel環境では WEBAPP_OUT_DIR が /tmp 配下になり、_HERE のサブパスにならない。
        out_path_display = str(out_path)

    sorted_flagged = sort_flagged(summary)

    # 登録画像のインラインプレビュー(data URI)。既に計算済みの b64 を再利用し、
    # 大きすぎる画像は Vercel のレスポンスサイズ上限を避けるためプレビューを省略する。
    uploaded_uri = None
    if len(data) <= INLINE_PREVIEW_MAX:
        mime = EXT_MIME.get(ext)
        if mime:
            uploaded_uri = f"data:{mime};base64,{b64}"

    return render_template_string(
        RESULT_TMPL,
        filename=f.filename,
        summary=summary,
        flagged_pages=sorted_flagged["flagged_pages"],
        flagged_direct=sorted_flagged["flagged_direct"],
        uploaded_uri=uploaded_uri,
        out_path=out_path_display,
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
