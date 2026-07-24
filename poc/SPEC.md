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
├── test_severity.py     # severity_rank/sort_flagged の単体テスト（pytest不要、標準assertのみ）
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

プロジェクトルート（`poc/` の外）に `vercel.json` があり、公開LP（`index.html`）を`/`に、
`webapp.py`を`/poc`配下にルーティングするVercelデプロイ設定を担う（詳細は後述「Vercelデプロイ」参照）。

---

## 検知ロジック

### Google Vision API Web Detection の使用フィールド

| フィールド | 意味 | AIMORIでの用途 |
|-----------|------|--------------|
| `pagesWithMatchingImages` | 同一・類似画像が掲載されているページURL | **転載・転売の検知**（掲載ページがターゲットPFか判定） |
| `fullMatchingImages` | 完全一致の画像URL | 転載の確度が高い候補。**ターゲットPFのCDN上に直接ホストされている場合はPF自動判定にも使用**（掲載ページ未検出のケースを拾う） |
| `partialMatchingImages` | 部分一致（トリミング等）の画像URL | 加工転載の候補。`fullMatchingImages`と同様、PF自動判定に使用 |
| `visuallySimilarImages` | 視覚的に類似した別画像のURL | **模倣品の検知**（別デザインだが見た目が似ているもの） |
| `webEntities` | 画像の推定ラベル（上位5件表示） | 参考情報 |
| `bestGuessLabels` | 画像内容の最有力推定ラベル（1件） | 参考情報（`best_guess`として表示。`webEntities`とは別の推定粒度なので統合しない） |

### 検知の3種類・2モード

```
【デフォルトモード】ターゲットPFに絞った検知

① 転載・転売の検知
   pagesWithMatchingImages のURLがターゲットPFのドメインと一致
   → 🚨【要注意】ターゲットPFの掲載ページ として表示

② 直接一致の検知
   fullMatchingImages / partialMatchingImages のURL自体がターゲットPFの
   CDNドメインと一致（掲載ページが未インデックスでも検知できる）
   → 🎯【直接一致】ターゲットPFで画像自体が完全/部分一致 として表示
   （①と同時に成立する場合もあり、排他的ではない）

③ 模倣品の検知
   visuallySimilarImages のURLがターゲットPFのドメインと一致
   → ⚠️【模倣品候補】ターゲットPFで見つかった類似画像 として表示

【--all モード】デザインパクリ検知（全サイト対象）

④ デザインパクリの検知
   visuallySimilarImages と pagesWithMatchingImages を全件表示
   fullMatchingImages / partialMatchingImages も直接一致画像として全件表示
   ターゲットPFにはタグを付与して識別
   → 📄【掲載ページ一覧】全件（PFタグ付き）
   → 🎯【直接一致画像 全件】全件（PFタグ付き）
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
    "BOOTH":        ["booth.pm"],
    "pixivFANBOX":  ["fanbox.cc"],
    "X(Twitter)":   ["x.com", "twitter.com", "twimg.com"],
    "楽天市場":      ["rakuten.co.jp"],
    "Yahoo!フリマ":  ["paypayfleamarket.yahoo.co.jp"],
}
```

`twimg.com`（Twitterの画像CDN）は実データ（`book.jpg`の検証結果）で `visuallySimilarImages` に含まれるにもかかわらず未検知だった実例を踏まえて追加。BOOTH・pixivFANBOXはイラスト・ハンドメイド作家にとって主要な販売PFとして追加。他PF（メルカリ等）の画像CDNドメインは実データでの裏付けが無いため未追加——実画像での検証が別途必要。

追加・変更は `TARGET_PLATFORMS` のみを編集すればよい。

### 重要度（一致の種類による序列）

Vision APIのマッチング結果（`fullMatchingImages` / `visuallySimilarImages` 等）には**数値の類似スコアが含まれない**ため、AIMORIでは一致の**種類（kind）**で重要度を定義する。`reverse_search.py` の純粋関数で管理する:

```python
_SEVERITY_ORDER = {"完全一致": 0, "部分一致": 1, "掲載": 2, "類似": 3}  # 小さいほど重要
severity_rank(kind)   # kind→ランク（未知の種別は最下位=99）
sort_flagged(summary) # flagged_pages/flagged_direct を重要度順に安定ソートした新dictを返す（summaryは非破壊）
```

`sort_flagged` は `flagged_pages`（掲載ページ）と `flagged_direct`（直接一致画像）を重要度順に並べ替える。`sorted()` の安定性により、同ランク内ではAPIが返した元の（関連度）順を維持する。`flagged_similar` は全件 `kind='類似'` のためソート対象外。webapp.py の結果画面がこの序列でバッジ色分け・並び替えを行う（後述「結果画面のUI」）。**偽の類似度%は表示しない**方針。

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
| 改変画像の検知限界 | 大幅な色変更・反転・コラージュは `visuallySimilarImages` でも見逃す場合がある | フェーズ2で二次リランキング（pHash→必要ならCLIP）を追加。効果検証ハーネスは実装済み（`poc/rerank.py`/`poc/eval_rerank.py`）|
| 画像サイズ上限 | base64エンコード後20MB超はAPIエラー | 4MB超で警告を表示、リサイズを案内 |
| ~~batch_verify.pyの429検知の穴~~（修正済み） | 従来は `batch_verify.py` の429検知が `"APIエラー (HTTP 429)"` のliteral一致（HTTPエラー経路のみ）で、HTTP 200 + レスポンス内 `"error"` でクォータ超過が返るケースを検知できなかった | `reverse_search.py` に共有ヘルパー `explain_response_error(err)` を追加し、`status == "RESOURCE_EXHAUSTED"` またはメッセージに `"quota"` を含む場合は `"APIエラー (クォータ超過)"` という専用文言を出すよう統一。`batch_verify.py` の429検知もこの文言を追加でチェックするよう修正済み |
| Webappの拡張子検査 | アップロード画像の検証は拡張子（`.jpg`等）のみで、内容は検証していない | 内部ローカルツールとして許容 |

---

## バッチ検証（`batch_verify.py`）

`reverse_search.py` を単独では実施しにくい「複数画像をまとめて検証し、ヒット率を定量把握する」ために追加したスクリプト。`reverse_search.py` 自体には変更を加えず、その純粋関数（`summarize_web_detection` / `platform_hit_breakdown` / `TARGET_PLATFORMS` / `safe_name` / `OUT_DIR` / `get_api_key`）を再利用する。分類ロジック自体は `build_result_from_web()` で再実装せず、`summarize_web_detection()` / `platform_hit_breakdown()` に委譲する薄いラッパーとして実装している。

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
| 直接一致ヒット（`has_direct_hit`） | 画像自体がターゲットPFのCDN上で完全/部分一致（`fullMatchingImages`/`partialMatchingImages`）。ページヒットと同等以上に強い証拠だが、ページヒットとの排他性は取らない（両方成立するケースもある） |
| 強シグナル合算（`has_page_hit or has_direct_hit`） | ページヒットまたは直接一致ヒットのいずれか（転載/転売相当の総数、重複除去済み） |
| 類似のみヒット（`has_similar_only_hit`） | 模倣品候補の弱シグナル。ページヒット・直接一致ヒットのどちらも無く `visuallySimilarImages` のみターゲットPFに存在（3値の優先順位: `has_page_hit` > `has_direct_hit` > `has_similar_only_hit`、`has_similar_only_hit`はこの2つに対して排他的） |

URLの重複排除は2階層で行う: 画像内（同一URLが掲載ページと類似画像の両方に出現する場合）と画像間（PF別サマリのUnique URL数）。

### 出力

`poc/out/VERIFICATION_SUMMARY.md`（デフォルト、gitignore済み）に集計結果・PF別ヒット数・画像別詳細・要注意URL一覧を Markdown で出力する。レポートには第三者サイトのURLが含まれるため、共有する場合は内容を確認した上で個別に取り出す。

---

## Webアプリ (`webapp.py`)

ブラウザから画像を1枚アップロードし、Vision API結果をHTMLで確認できる軽量ローカルツール。「アップロード→結果表示」のスライスのみを実装しており、フェーズ2の本格Webアプリ（作品DB・ユーザー登録・課金・定期スキャン）とは別物。

### 再利用する `reverse_search.py` の関数

`get_api_key`, `build_request_body`, `call_vision_api`, `summarize_web_detection`, `sort_flagged`, `explain_response_error`, `safe_name`, `VisionAPIError`, `OUT_DIR`

### ルート

| ルート | メソッド | 内容 |
|-------|---------|------|
| `/`, `/poc/` | GET | 画像アップロードフォーム（インラインHTML） |
| `/scan`, `/poc/scan` | POST | 画像を受け取りVision APIを呼び、結果をHTMLで返す |

`/poc/*` はVercel本番でルート直下の公開LP（`index.html`）と共存させるためのエイリアス（`vercel.json`が`/poc(/.*)?`をこのアプリにルーティングする）。テンプレート内のリンク・フォームaction属性は相対パスで記述されており、`/`・`/poc/`のどちらでアクセスしても正しい送信先に解決される。ローカル開発（`python3 poc/webapp.py`）では従来通り`/`でアクセスする。

### 設計判断

- **ローカルは無認証、外部公開時はBasic Auth必須**: ローカル実行（`python3 poc/webapp.py`）では `app.run(host="127.0.0.1", ...)` で固定し `0.0.0.0` バインドは行わない。Vercel等に公開する場合は環境変数 `WEBAPP_USER` / `WEBAPP_PASS` を設定すると `@app.before_request` がHTTP Basic認証を強制する（`poc/webapp.py`）。両方とも未設定のままローカル以外へ公開する運用は想定していない。
- **APIキー解決はモジュール最上位**: `if __name__=="__main__"` の外で `API_KEY = get_api_key()` を呼ぶ。`__main__` ブロック内で解決すると `flask run` 等の起動経路で `__name__` が `"webapp"` になりブロックがスキップされ、リクエスト時に `NameError` になるため。
- **アップロード制約**: `MAX_CONTENT_LENGTH=14MB`（base64化後にVisionの20MB上限へ収まる余裕を見た値）、拡張子allow-list（`.jpg/.jpeg/.png/.webp/.gif/.bmp`）。結果画面の登録画像インラインプレビューは別途 `INLINE_PREVIEW_MAX=1.5MB` で制限（Vercelレスポンス上限対策、上記「結果画面のUI」参照）。
- **出力先**: ローカルは `poc/out/webapp/<timestamp>_<safe_name>.web.json`（`timestamp` はマイクロ秒粒度）。`batch_verify.py` のキャッシュ（`poc/out/` 直下 `<sname>.web.json`）とディレクトリごと分離されており、命名・キャッシュロジックへの相互影響はない。Vercel環境（環境変数 `VERCEL` が設定される）では書き込み可能ディレクトリが `/tmp` のみのため `/tmp/aimori_webapp/` に切り替わる（エフェメラルであり永続しない）。結果ページの「生レスポンス保存先」表示は、`_HERE`（`poc/`）のサブパスでない場合は絶対パスにフォールバックする。
- **例外処理**: `VisionAPIError` およびその他の想定外例外はリクエスト単位で捕捉し、HTMLエラーページ（502）を返す。サーバプロセス自体はクラッシュしない。
- **XSS/SSTI対策**: `render_template_string` には固定テンプレ定数とコンテキスト変数のみを渡し、ユーザー値（filename・第三者ページタイトル等）をテンプレ文字列に連結しない。Jinjaのオートエスケープを維持し `|safe` は使用しない。サムネイルの `onerror` ハンドラも**URLを差し込まない静的な文字列**（クラス付与のみ）とし、JSコンテキストでのエスケープ不整合を避ける。

### 結果画面のUI

「URLの羅列」から「見て分かる証拠」へ引き上げるため、結果ページ（`RESULT_TMPL`）は以下を備える。LP（`index.html`）と共通のOKLCHデザイントークン（`BASE_CSS`）を使用する。

- **サムネイル可視化**: 画像URLを持つセクション（`flagged_direct` / `flagged_similar` / 全件`similar`）は一致画像をサムネイルグリッドで表示する。第三者CDNの画像はホットリンク保護で403になることが多いため、**破損時は `onerror` で画像を隠し、caption内のラベル付きリンクに劣化させる**（空の死にカードを作らない）。各グリッド下に従来の全URLリストを `<details>` で保持し、情報を失わない。ページURLを持つ `flagged_pages` はサムネイル化せずリンク表示（→ follow-up）。
- **登録画像プレビュー**: アップロード画像を結果上部に data URI でインライン表示する。ただし **Vercelのserverlessレスポンス上限（~4.5MB）**を踏まえ、`INLINE_PREVIEW_MAX`（raw 1.5MB）以下の画像のみ埋め込み、超過時は「省略」注記を出す。base64は既存の値を再利用し再エンコードしない。MIMEは拡張子→正式タイプの明示マップで解決（`image/jpg` は使わない）。
- **重要度ソート・色分け**: `sort_flagged` の序列でヒットを並べ、`sev-badge`（完全一致=柿/clay、部分一致=藍/accent、掲載・類似=neutral）で色分け。見出しに「重要度（一致の種類による）順」と明記し、数値%は出さない。
- **空状態**: ターゲットPFヒットが0件（`flagged_all == 0`）のとき、3つの「該当なし」の羅列を1枚の安心パネル（「見つかりませんでした／ただし存在しないことの保証ではない」）に集約する。参考・全件類似セクションは `flagged_all` に関わらず常に描画し、情報を隠さない。
- **ローディング演出**: フォーム送信は同期POSTで待機が発生するため、`FORM_TMPL` 側に全画面オーバーレイ（「照合中…」）を表示する。**bfcache（戻るボタン）復元時にオーバーレイ／無効化ボタンが固まる**のを防ぐため `pageshow` でリセットする。アニメーションは `prefers-reduced-motion` を尊重する。
- **プライバシー注記**: 第三者CDN画像を `<img>` で読み込むと閲覧者のIPが（侵害者側の）CDNに渡る。`referrerpolicy="no-referrer"` でリファラは消えるがIPは隠れない。内部ツールの脅威モデルとして許容する。

### 依存

`poc/requirements.txt`（`Flask>=3.0,<4`）。`reverse_search.py` / `batch_verify.py` 自体は引き続き標準ライブラリのみ。

### Vercelデプロイ（任意）

内部確認・デモ目的でVercelにも公開可能。プロジェクトルートの `vercel.json` を使用する。
`index.html`（`@vercel/static`）を`/`に、`webapp.py`（`@vercel/python`）を`/poc(/.*)?`に
ルーティングしており、本番URLでは `https://<ドメイン>/poc` からアクセスする。

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
- **掲載ページ（転載の疑い）のサムネイル化**：最重要シグナルである `flagged_pages` に視覚証拠を付ける。`pages[i].fullMatchingImages[0].url`（fallback `partialMatchingImages`）から画像URLを取得する。`flagged_pages` のtuple arityは変えず、`reverse_search.py` に純粋ヘルパーを additive 追加して `batch_verify.py` を非破壊に保つ方針。ホットリンク破損も多いため実データ検証とセットで行う
- **二次リランキング（pHash → 必要ならCLIP）**：Google Vision APIが返した類似画像URL（full/partial/similar）に対し、クエリ画像との**本物の0-1類似度**を自前計算して並べ替え、改変画像（色変え・反転・切り抜き）の精度を向上。まず軽量な pHash をベースラインとし、取りこぼす場合のみ CLIP/DINOv2 を投入する。
  - **効果検証はオフラインで実装済み**：`poc/rerank.py`（リランカ本体）＋ `poc/eval_rerank.py`（Recall@k/mAP でVision順 vs 自前スコア順を比較）。新API不要・`webapp.py`/Vercel非依存。ML依存は `poc/requirements-ml.txt` に分離（`requirements.txt` は `Flask` のみ維持）。使い方は `poc/README.md`「オフライン評価ハーネス」節、投資順序は `docs/DL_ROADMAP.md` を参照。
  - **本番搭載時の注意**：CLIP推論はVercel関数（`/tmp`のみ・実行時間制限・GPUなし）では動かせないため、常駐バックエンドコンテナに置く。
- **Webアプリ化（本格版）**：作品登録・スキャン結果・案件管理をUI化。軽量な単一画像版はPoCの `webapp.py` として先行実装済み（ユーザー登録・課金・定期実行は依然未実装）
- **定期スキャンの自動化**：スケジューラで登録作品を定期的にスキャン

### フェーズ3以降
- CLIP/DINOv2 + Qdrant によるベクトル検索（大規模ユーザー対応・ANN設計）／知財特化のドメインファインチューニング
- 削除要請テンプレート自動生成（弁護士確認後）
