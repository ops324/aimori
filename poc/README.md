# AIMORI PoC — Google Vision 逆画像検索

登録作品の画像を **Google Vision API の Web Detection** に渡し、ネット上の
どこに同一・類似画像が掲載されているかを取得する検証スクリプトです。

## このPoCが確かめること

> **メルカリ・minne・Creema 等のフリマ/ハンドメイド出品ページが、Google経由で実際にヒットするのか。**

ヒットすれば、AIMORIは「自前で各サイトをクロールする（法的リスク大）」のではなく
「Googleの索引を借りて転載を探す（法的リスク小）」設計で成立します。
これはビジネス設計の分岐点なので、机上でなく実測して確かめます。

---

## セットアップ

### 1. Google Cloud APIキーの取得

1. [Google Cloud Console](https://console.cloud.google.com/) にログイン
2. プロジェクトを作成（例：`aimori-poc`）
3. **Cloud Vision API を有効化**
   → https://console.cloud.google.com/apis/library/vision.googleapis.com で「有効にする」
4. **APIキーを発行**
   → 「APIとサービス」→「認証情報」→「認証情報を作成」→「APIキー」
5. （推奨）発行したキーの「キーを制限」で、対象APIを **Cloud Vision API のみ** に限定

> ⚠️ APIキーは秘密情報です。他人と共有したりGitにコミットしないでください。
> このスクリプトは環境変数から読むため、キーがコードやログに残りません。

### 2. キーを環境変数に設定

```bash
export GOOGLE_VISION_API_KEY=<発行したAPIキー>
```

（毎回入力するのが面倒なら `~/.zshrc` に追記）

---

## 使い方

```bash
# ローカル画像で実行
python3 reverse_search.py path/to/artwork.jpg

# 複数画像をまとめて
python3 reverse_search.py a.jpg b.png c.jpg

# 画像URLで実行（ローカルに落とさず試せる）
python3 reverse_search.py --url https://example.com/artwork.jpg
```

Python 3.9以降・`reverse_search.py` / `batch_verify.py` は標準ライブラリのみで動きます（`pip install` 不要）。
Webアプリ版（`webapp.py`）のみ Flask が必要です — 詳細は下記「Webアプリ版」を参照。

---

## バッチ検証（複数画像を一括検証）

作品画像を何枚もまとめて検証し、ヒット率を集計したい場合は `batch_verify.py` を使います。

### test_images/ の作り方

検証したい作品画像（イラスト・ハンドメイド写真等）を1つのディレクトリにまとめます。

```bash
mkdir -p poc/test_images
# ここに検証したい画像ファイルをコピーする
```

`poc/test_images/` は `.gitignore` されていないので、作品画像そのものをコミットしないよう注意してください（画像本体をGit管理したくない場合は独自のディレクトリを使い、`.gitignore` に追記してください）。

### 実行

```bash
export GOOGLE_VISION_API_KEY=<発行したAPIキー>

python3 poc/batch_verify.py poc/test_images/
python3 poc/batch_verify.py poc/test_images/ --force      # キャッシュを無視して再処理
python3 poc/batch_verify.py poc/test_images/ --max 20     # 最大20枚のみ処理（コスト制御）
python3 poc/batch_verify.py poc/test_images/ --delay 2    # API呼び出し間隔を2秒に
```

- 未キャッシュの画像が5枚を超える場合、実行前に確認プロンプトが出ます（`--yes` でスキップ可）
- 同じ画像に対する2回目以降の実行はキャッシュを利用し、APIを再度呼びません
- Vision API が 429（レート制限・無料枠超過）を返すと、その時点でバッチを中断し、それまでの結果でレポートを生成します
- `Ctrl+C` で中断しても、その時点までの結果でレポートが出力されます

### レポートの見方

`poc/out/VERIFICATION_SUMMARY.md`（`.gitignore` 済み）に以下が出力されます。

- 画像単位のAPIヒット率・ターゲットPFヒット率（強シグナル＝転載/転売のページヒット、弱シグナル＝類似画像のみのヒット）
- プラットフォーム別のヒット画像数・重複排除後のユニークURL数
- 画像ごとの詳細表と、要注意URLの一覧（手動確認用）

> ⚠️ レポートには第三者サイトのURL・ページタイトルが含まれます。チームや弁護士に共有する場合は、内容を確認したうえで `poc/out/` から個別に取り出してください（自動でコミットされることはありません）。

---

## Webアプリ版（ブラウザから1枚ずつ確認）

CLIの代わりに、ブラウザから画像をアップロードして結果を確認できる軽量ローカルツールです。
「アップロード→結果表示」だけのスライスで、作品DB・ユーザー登録・課金・定期スキャンなどは含みません
（それらはフェーズ2の本格Webアプリの範囲）。

### セットアップと起動

```bash
pip install -r poc/requirements.txt   # pipが必要なのはこの手順だけ
export GOOGLE_VISION_API_KEY=<発行したAPIキー>
python3 poc/webapp.py
```

ブラウザで `http://127.0.0.1:5000/` を開き、画像をアップロードすると結果がHTMLで表示されます。

### 注意事項

- **ローカル/内部利用専用です。** `127.0.0.1` 以外にバインドしないでください（APIキーはこのプロセス内にのみ存在し、外部公開する理由がありません）。
- 同じ `GOOGLE_VISION_API_KEY` を使用し、起動時に1回だけ存在チェックします（未設定なら起動しません）。
- 結果の生JSONは `poc/out/webapp/` に保存されます（CLI/バッチのキャッシュ `poc/out/*.web.json` とは別ディレクトリで、互いに衝突しません）。`poc/out/` は `.gitignore` 済みです。
- アップロードは画像ファイルの拡張子のみで簡易検査しています（内容の検証はしていません）。単一ユーザーのローカル確認用途を想定しており、本番運用のセキュリティ要件は満たしません。

---

## 出力の見方

- 🚨【要注意】 … メルカリ・minne 等ターゲットPFでヒットした掲載ページ（＝転載の疑い）
- 【参考】 … その他のサイトでの掲載ページ
- サマリ … 各カテゴリ件数と、ターゲットPFヒット数
- 生のAPIレスポンスは `out/<画像名>.web.json` に保存（後で件数集計・精査に使えます）

---

## 料金（2026-07時点）

| 項目 | 内容 |
|------|------|
| 無料枠 | 月 **1,000ユニット** まで無料（Web Detection 1画像 = 1ユニット） |
| 超過分 | **$3.50 / 1,000ユニット** |
| 原価試算 | 作品50点 × 月4回 = 200ユニット/ユーザー/月 → 無料枠内、有料換算でも約100円/ユーザー/月 |

出典: https://cloud.google.com/vision/pricing

---

## 検証手順（2段階）

### ステップ1: サニティチェック（APIが動くか）

ネット上に**確実に存在する画像**（有名な商品画像・広く出回っているイラスト等）で実行し、
`pagesWithMatchingImages` が返ってくることを確認する。
→ 返ってくれば、APIとキーは正常に機能している。

### ステップ2: 本番検証（フリマ系がヒットするか）

転載を探したい作品画像を複数枚 `poc/test_images/` に集め、[バッチ検証](#バッチ検証複数画像を一括検証)を実行する。

```bash
python3 poc/batch_verify.py poc/test_images/
```

`poc/out/VERIFICATION_SUMMARY.md` に、画像単位のヒット率・PF別ヒット数・要注意URL一覧が自動集計される。

**判断基準**：メルカリ/minne/Creema 等が現実的な頻度でヒットするなら Option C は有望。
ほとんどヒットしないなら、Googleの索引網羅性が不足 → 別アプローチ（提携・CLIP等）を再検討。

---

## 次のステップ（このPoCの外）

- 改変画像（色調変更・反転・トリミング）対策として **CLIP** による二次類似判定を追加
- 定期スキャン・作品DB・通知のWebアプリ化（軽量な単一画像版は本PoCの `webapp.py` として先行実装済み。ユーザー登録・課金・定期実行を含む本格版は依然フェーズ2の範囲）
- 削除要請テンプレ生成（※非弁リスクの論点整理が前提）
