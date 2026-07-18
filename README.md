# AIMORI（アイモリ）

> アイデアを守るAI — 日本の作家・クリエイターのための知財監視SaaS

[![Status](https://img.shields.io/badge/status-Phase%200%20%E2%80%94%20Validation-yellow)](SPEC.md)

---

## このリポジトリについて

AIMORI は、個人イラストレーター・ハンドメイド作家・D2Cブランドが、自分の作品の**無断転載・模倣を自動検知**するためのAI監視サービスです。

日本語プラットフォーム（メルカリ・minne・Creema・BASE・pixiv・X 等）に特化しており、これらを対象とした監視SaaSは国内外で前例がありません。

**現在地**: フェーズ0 — LP公開・需要検証中

---

## ファイル構成

| ファイル | 内容 |
|---------|------|
| [`index.html`](index.html) | ランディングページ（LP） |
| [`SPEC.md`](SPEC.md) | 事業・技術仕様書（詳細） |

---

## クイックスタート（LP確認）

```bash
# ブラウザで直接開く
open index.html
```

サーバー不要。単一HTMLファイルで完結しています。

---

## ロードマップ（概要）

```
Phase 0 ← 現在地
  LP公開・事前登録 → 作家インタビュー → 弁護士相談 → Go/No-Go

Phase 1  MVP開発（1〜3ヶ月）
  pixiv/X対応 → CLIP類似検知 → クローズドβ

Phase 2  拡大・収益化（3〜6ヶ月）
  メルカリ/minne/Creema/BASE → 有料サブスク開始

Phase 3  深化（6ヶ月〜）
  海外EC対応 → 弁護士連携オプション
```

詳細は [SPEC.md](SPEC.md) を参照。

---

## 技術スタック（予定）

- 画像類似検知: CLIP + Qdrant（ベクトルDB）
- バックエンド: Python / FastAPI（TBD）
- フロントエンド: React / Next.js（TBD）
- LP: 単一HTMLファイル（現在）

---

## 連絡先

事業に関するお問い合わせ・相談: nujihiwtt@gmail.com

---

*© 2026 AIMORI. 事業仕様・コードのご利用はご連絡ください。*
