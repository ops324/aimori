# AIMORI PoC 技術仕様書

## 概要

Google Vision API の Web Detection を使い、登録作品の**転載（同一画像）**と**模倣品（類似画像）**をネット上から発見する逆画像検索スクリプト。

自前クロール（法的リスク大）を使わず、Googleが合法的にクロール済みの索引を借りることで、法的リスクをほぼゼロにした「逆引き代行モデル（Option C）」を実装している。

---

## ファイル構成

```
poc/
├── reverse_search.py   # メインスクリプト
├── README.md           # セットアップ・実行手順
├── SPEC.md             # 本ファイル（技術仕様）
├── .gitignore
└── out/                # 生レスポンス保存先（gitignore済み）
    └── <画像名>.web.json
```

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

### 検知の2種類

```
① 転載・転売の検知
   pagesWithMatchingImages のURLがターゲットPFのドメインと一致
   → 🚨【要注意】ターゲットPFの掲載ページ として表示

② 模倣品の検知（本番用途）
   visuallySimilarImages のURLがターゲットPFのドメインと一致
   → ⚠️【模倣品候補】ターゲットPFで見つかった類似画像 として表示
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
```

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

---

## 制約・既知の限界

| 制約 | 詳細 | 対策 |
|------|------|------|
| Googleのインデックス遅延 | 転載されてからGoogleにインデックスされるまで数日〜数週間かかる場合がある | 許容する（完全リアルタイムは不可）|
| インデックスされていないページは検知不可 | 非公開出品・新着出品は未インデックスのことがある | 定期スキャンで経時的に拾う |
| 改変画像の検知限界 | 大幅な色変更・反転・コラージュは `visuallySimilarImages` でも見逃す場合がある | フェーズ2でCLIP二次判定を追加（予定）|
| 画像サイズ上限 | base64エンコード後20MB超はAPIエラー | 4MB超で警告を表示、リサイズを案内 |

---

## 今後の拡張計画

### フェーズ2で追加予定
- **CLIP二次判定**：Google Vision APIが返した類似画像URLに対してCLIPで特徴量比較を行い、改変画像の精度を向上
- **Webアプリ化**：作品登録・スキャン結果・案件管理をUI化
- **定期スキャンの自動化**：スケジューラで登録作品を定期的にスキャン

### フェーズ3以降
- CLIP + Qdrant によるベクトル検索（大規模ユーザー対応）
- 削除要請テンプレート自動生成（弁護士確認後）
