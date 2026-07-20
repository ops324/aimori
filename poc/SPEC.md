# AIMORI PoC 技術仕様書

## 概要

Google Vision API の Web Detection を使い、登録作品の**転載（同一画像）**と**模倣品（類似画像）**をネット上から発見する逆画像検索スクリプト。

自前クロール（法的リスク大）を使わず、Googleが合法的にクロール済みの索引を借りることで、法的リスクをほぼゼロにした「逆引き代行モデル（Option C）」を実装している。

---

## ファイル構成

```
poc/
├── reverse_search.py   # メインスクリプト（単一/複数画像の検索）
├── batch_verify.py     # バッチ検証スクリプト（ディレクトリ一括検証・集計レポート）
├── webapp.py            # Webアプリ版（ブラウザから1枚アップロード→結果表示、Flask）
├── requirements.txt     # webapp.py用の依存（Flask）
├── README.md           # セットアップ・実行手順
├── SPEC.md             # 本ファイル（技術仕様）
├── .gitignore
└── out/                # 生レスポンス保存先（gitignore済み）
    ├── <画像名>.web.json         # Vision API 生レスポンス（CLI/バッチ用キャッシュ）
    ├── <画像名>.web.json.meta    # キャッシュ検証用 SHA1 サイドカー（batch_verify.py が生成）
    ├── VERIFICATION_SUMMARY.md   # batch_verify.py が生成する検証レポート
    ├── .batch_checkpoint.<PID>.jsonl  # 実行中の一時チェックポイント（正常終了時は削除）
    └── webapp/              # webapp.py の保存先（CLI/バッチのキャッシュとは分離）
        └── <timestamp>_<画像名>.web.json
```

プロジェクトルート（`poc/` の外）に `vercel.json` があり、`webapp.py` のVercelデプロイ設定を担う（詳細は後述「Vercelデプロイ」参照）。

---

## 検知ロジック

### Google Vision API Web Detection の使用フィールド

| フィールド | 意味 | AIMORIでの用途 |
|-----------|------|--------------|
| `pagesWithMatchingImages` | 同一・類似画像が掲載されているページURL | **転載・転売の検知**（掲載ページがターゲットPFか判定） |
| `fullMatchingImages` | 完全一致の画像URL | 転載の確度が高い候補 |
| `partialMatchingImages` | 部分一致（トリミング等）の画像URL | 加工転載の候補 |
| `visuallySimilarImages` | 視覚的に類似した別画像のURL | **模倣品の検知**（別デザインだが見た目が似ているもの） |
| `webEntities` | 画像の推定ラベル | 参考情報 |

### 検知の3種類・2モード

```
【デフォルトモード】ターゲットPFに絞った検知

① 転載・転売の検知
   pagesWithMatchingImages のURLがターゲットPFのドメインと一致
   → 🚨【要注意】ターゲットPFの掲載ページ として表示

② 模倣品の検知
   visuallySimilarImages のURLがターゲットPFのドメインと一致
   → ⚠️【模倣品候補】ターゲットPFで見つかった類似画像 として表示

【--all モード】デザインパクリ検知（全サイト対象）

③ デザインパクリの検知
   visuallySimilarImages と pagesWithMatchingImages を全件表示
   ターゲットPFにはタグを付与して識別
   → 📄【掲載ページ一覧】全件（PFタグ付き）
   → 🔍【類似画像一覧】全件 ← デザインパクリ候補
```

---

## ターゲットプラットフォーム定義

`reverse_search.py` の `TARGET_PLATFORMS` dict で管理。ホスト名の末尾一致で判定（サブドメイン対応）。

```python
TARGET_PLATFORMS = {
    "メルカリ":      ["mercari.com", "jp.mercari.com", "mercari-shops.com"],
    "minne":        ["minne.com"],
    "Creema":       ["creema.jp"],
    "BASE":         ["base.shop", "base.ec", "thebase.in",
                     "official.ec", "buyshop.jp", "shopselect.net", "theshop.jp"],
    "pixiv":        ["pixiv.net"],
    "X(Twitter)":   ["x.com", "twitter.com"],
    "楽天市場":      ["rakuten.co.jp"],
    "Yahoo!フリマ":  ["paypayfleamarket.yahoo.co.jp"],
}
```

追加・変更は `TARGET_PLATFORMS` のみを編集すればよい。

---

## API仕様

### エンドポイント

```
POST https://vision.googleapis.com/v1/images:annotate
```

### 認証

`X-goog-api-key` **HTTPヘッダー**でAPIキーを送信（URLクエリ不使用 — シェル履歴・ログへの漏洩を防ぐ）。

### リクエストボディ

```json
{
  "requests": [{
    "image": {
      "content": "<base64エンコードした画像>"
    },
    "features": [{"type": "WEB_DETECTION", "maxResults": 50}]
  }]
}
```

画像URLで指定する場合：
```json
"image": {"source": {"imageUri": "https://..."}}
```

### レスポンスの主要構造

```json
{
  "responses": [{
    "webDetection": {
      "pagesWithMatchingImages": [{"url": "...", "pageTitle": "..."}],
      "fullMatchingImages":      [{"url": "..."}],
      "partialMatchingImages":   [{"url": "..."}],
      "visuallySimilarImages":   [{"url": "..."}],
      "webEntities":             [{"description": "...", "score": 0.0}]
    }
  }]
}
```

---

## 料金（2026-07時点）

| 月間利用量 | 料金 |
|-----------|------|
| 〜1,000ユニット/月 | **無料** |
| 1,001〜5,000,000ユニット/月 | **$3.50 / 1,000ユニット** |

出典: https://cloud.google.com/vision/pricing

### 原価試算

```
登録作品50点 × 月4回スキャン = 200ユニット/ユーザー/月
→ 無料枠（1,000ユニット/月）の範囲内
→ 有料換算でも 200 × $0.0035 ≈ $0.70 ≈ 約100円/ユーザー/月
→ 月額¥1,980プランで十分なマージン
```

---

## CLI仕様

### 使い方

```bash
# ローカル画像（複数可）
python3 reverse_search.py image.jpg [image2.png ...]

# 画像URL
python3 reverse_search.py --url https://example.com/image.jpg

# 混在
python3 reverse_search.py image.jpg --url https://example.com/image.jpg

# デザインパクリ検知（全サイトの類似画像を全件表示）
python3 reverse_search.py --all image.jpg
```

### オプション

| オプション | 説明 |
|-----------|------|
| `--url URL` | 画像URL指定（複数可）。ローカルファイルの代わりに使用 |
| `--all` | ターゲットPFに限らず類似画像・掲載ページを全件表示（デザインパクリ検知用） |

### 環境変数

| 変数名 | 必須 | 説明 |
|-------|------|------|
| `GOOGLE_VISION_API_KEY` | ✅ | Google Cloud Vision API のAPIキー |

未設定の場合は取得手順を案内して終了する。

### 出力ファイル

生レスポンスを `poc/out/<画像名>.web.json` に保存（後から精査・集計に使用）。`out/` は `.gitignore` 済み。

### エラーハンドリング

| HTTPコード | 原因 | 出力メッセージ |
|-----------|------|--------------|
| 400 | キー形式不正 / リクエスト不正 | キーを確認してください |
| 401 | 認証失敗 | APIキーが正しいか確認 |
| 403 | API未有効化 / 権限なし / 請求未設定 | Cloud Consoleで有効化・請求設定を促すURL付きメッセージ |
| 429 | レート制限 / 無料枠超過 | 無料枠(月1,000ユニット)を超過 |

`reverse_search.py` は内部でエラーを `VisionAPIError`（HTTP/ネットワーク/レスポンス内エラー）として raise する。CLIの `main()` はこれをトップレベルで捕捉し `die()`（stderr出力 + `exit 1`）する。`webapp.py` はリクエスト単位で同じ例外を捕捉し、プロセスを落とさずHTMLエラーページ（502）を返す。分類ロジック（`pagesWithMatchingImages`等からPFヒットを抽出する処理）は `summarize_web_detection(web)` という純粋関数に切り出されており、CLI出力と `webapp.py` の両方から再利用される。

---

## 制約・既知の限界

| 制約 | 詳細 | 対策 |
|------|------|------|
| Googleのインデックス遅延 | 転載されてからGoogleにインデックスされるまで数日〜数週間かかる場合がある | 許容する（完全リアルタイムは不可）|
| インデックスされていないページは検知不可 | 非公開出品・新着出品は未インデックスのことがある | 定期スキャンで経時的に拾う |
| 改変画像の検知限界 | 大幅な色変更・反転・コラージュは `visuallySimilarImages` でも見逃す場合がある | フェーズ2でCLIP二次判定を追加（予定）|
| 画像サイズ上限 | base64エンコード後20MB超はAPIエラー | 4MB超で警告を表示、リサイズを案内 |
| Webapp版の429検知の穴 | `batch_verify.py` の429検知は `"APIエラー (HTTP 429)"` のliteral一致（HTTPエラー経路のみ）。HTTP 200 + レスポンス内 `"error"` でクォータ超過が返るケースは検知できない | 既知の限界として許容。修正は本スコープ外 |
| Webappの拡張子検査 | アップロード画像の検証は拡張子（`.jpg`等）のみで、内容は検証していない | 内部ローカルツールとして許容 |

---

## バッチ検証（`batch_verify.py`）

`reverse_search.py` を単独では実施しにくい「複数画像をまとめて検証し、ヒット率を定量把握する」ために追加したスクリプト。`reverse_search.py` 自体には変更を加えず、その純粋関数（`match_platform` / `TARGET_PLATFORMS` / `safe_name` / `OUT_DIR` / `get_api_key`）を再利用する。

### 使い方

```bash
export GOOGLE_VISION_API_KEY=<your-key>
python3 poc/batch_verify.py ./test_images/
python3 poc/batch_verify.py ./test_images/ --force            # キャッシュ無視で再処理
python3 poc/batch_verify.py ./test_images/ --delay 2          # API呼び出し間隔（秒、デフォルト1）
python3 poc/batch_verify.py ./test_images/ --max 20           # 最大処理枚数
python3 poc/batch_verify.py ./test_images/ --output PATH      # レポート出力先
python3 poc/batch_verify.py ./test_images/ --yes              # コスト確認プロンプトをスキップ
```

### キャッシュ機構

`safe_name()` はファイル名の非可逆変換のため、異なる画像が同じキャッシュキーになる可能性がある。これに対応するため:

- 各 `.web.json` に対して `.web.json.meta`（元画像の SHA1 と サイズ）をサイドカーとして保存
- 実行時に SHA1 を再計算し、一致すればキャッシュ利用・不一致なら警告を出して新規 API 呼び出し
- サイドカーの無い旧形式の `.web.json`（`reverse_search.py` を単体実行して作られたもの）は valid cache として扱い、その場でサイドカーを生成する
- 同一バッチ内で `safe_name` が衝突するファイル名の組み合わせがあれば、`--force` の有無に関わらず即エラー終了する

### レート制限対策

Vision API の 429（レート制限・無料枠超過）を検知した時点でバッチ全体を中断し、それまでの結果でレポートを生成する。API呼び出し間には `--delay`（デフォルト1秒）のウェイトを挟む。

### 集計指標

| 指標 | 意味 |
|------|------|
| ページヒット（`has_page_hit`） | 転載・転売の強シグナル。`pagesWithMatchingImages` がターゲットPFに存在 |
| 類似のみヒット（`has_similar_only_hit`） | 模倣品候補の弱シグナル。ページヒットが無く `visuallySimilarImages` のみターゲットPFに存在（排他的） |

URLの重複排除は2階層で行う: 画像内（同一URLが掲載ページと類似画像の両方に出現する場合）と画像間（PF別サマリのUnique URL数）。

### 出力

`poc/out/VERIFICATION_SUMMARY.md`（デフォルト、gitignore済み）に集計結果・PF別ヒット数・画像別詳細・要注意URL一覧を Markdown で出力する。レポートには第三者サイトのURLが含まれるため、共有する場合は内容を確認した上で個別に取り出す。

---

## Webアプリ (`webapp.py`)

ブラウザから画像を1枚アップロードし、Vision API結果をHTMLで確認できる軽量ローカルツール。「アップロード→結果表示」のスライスのみを実装しており、フェーズ2の本格Webアプリ（作品DB・ユーザー登録・課金・定期スキャン）とは別物。

### 再利用する `reverse_search.py` の関数

`get_api_key`, `build_request_body`, `call_vision_api`, `summarize_web_detection`, `match_platform`, `safe_name`, `VisionAPIError`, `TARGET_PLATFORMS`, `OUT_DIR`

### ルート

| ルート | メソッド | 内容 |
|-------|---------|------|
| `/` | GET | 画像アップロードフォーム（インラインHTML） |
| `/scan` | POST | 画像を受け取りVision APIを呼び、結果をHTMLで返す |

### 設計判断

- **ローカルは無認証、外部公開時はBasic Auth必須**: ローカル実行（`python3 poc/webapp.py`）では `app.run(host="127.0.0.1", ...)` で固定し `0.0.0.0` バインドは行わない。Vercel等に公開する場合は環境変数 `WEBAPP_USER` / `WEBAPP_PASS` を設定すると `@app.before_request` がHTTP Basic認証を強制する（`poc/webapp.py`）。両方とも未設定のままローカル以外へ公開する運用は想定していない。
- **APIキー解決はモジュール最上位**: `if __name__=="__main__"` の外で `API_KEY = get_api_key()` を呼ぶ。`__main__` ブロック内で解決すると `flask run` 等の起動経路で `__name__` が `"webapp"` になりブロックがスキップされ、リクエスト時に `NameError` になるため。
- **アップロード制約**: `MAX_CONTENT_LENGTH=14MB`（base64化後にVisionの20MB上限へ収まる余裕を見た値）、拡張子allow-list（`.jpg/.jpeg/.png/.webp/.gif/.bmp`）。
- **出力先**: ローカルは `poc/out/webapp/<timestamp>_<safe_name>.web.json`（`timestamp` はマイクロ秒粒度）。`batch_verify.py` のキャッシュ（`poc/out/` 直下 `<sname>.web.json`）とディレクトリごと分離されており、命名・キャッシュロジックへの相互影響はない。Vercel環境（環境変数 `VERCEL` が設定される）では書き込み可能ディレクトリが `/tmp` のみのため `/tmp/aimori_webapp/` に切り替わる（エフェメラルであり永続しない）。結果ページの「生レスポンス保存先」表示は、`_HERE`（`poc/`）のサブパスでない場合は絶対パスにフォールバックする。
- **例外処理**: `VisionAPIError` およびその他の想定外例外はリクエスト単位で捕捉し、HTMLエラーページ（502）を返す。サーバプロセス自体はクラッシュしない。
- **XSS/SSTI対策**: `render_template_string` には固定テンプレ定数とコンテキスト変数のみを渡し、ユーザー値（filename・第三者ページタイトル等）をテンプレ文字列に連結しない。Jinjaのオートエスケープを維持し `|safe` は使用しない。

### 依存

`poc/requirements.txt`（`Flask>=3.0,<4`）。`reverse_search.py` / `batch_verify.py` 自体は引き続き標準ライブラリのみ。

### Vercelデプロイ（任意）

内部確認・デモ目的でVercelにも公開可能。プロジェクトルートの `vercel.json`（`@vercel/python` ビルド）を使用する。

```bash
vercel env add GOOGLE_VISION_API_KEY production
vercel env add WEBAPP_USER production
vercel env add WEBAPP_PASS production
vercel deploy --prod
```

| 環境変数 | 必須 | 説明 |
|---------|------|------|
| `GOOGLE_VISION_API_KEY` | ✅ | Vision APIキー（ローカルと共通） |
| `WEBAPP_USER` / `WEBAPP_PASS` | 実質必須 | Basic Auth用の認証情報。両方設定して初めて認証が有効になる |

未設定のまま公開すると誰でもアクセスでき、APIキーのクォータを消費されるリスクがあるため、Vercelへのデプロイ時は必ず設定すること。

---

## 今後の拡張計画

### フェーズ2で追加予定
- **CLIP二次判定**：Google Vision APIが返した類似画像URLに対してCLIPで特徴量比較を行い、改変画像の精度を向上
- **Webアプリ化（本格版）**：作品登録・スキャン結果・案件管理をUI化。軽量な単一画像版はPoCの `webapp.py` として先行実装済み（ユーザー登録・課金・定期実行は依然未実装）
- **定期スキャンの自動化**：スケジューラで登録作品を定期的にスキャン

### フェーズ3以降
- CLIP + Qdrant によるベクトル検索（大規模ユーザー対応）
- 削除要請テンプレート自動生成（弁護士確認後）
