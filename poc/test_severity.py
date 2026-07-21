#!/usr/bin/env python3
"""
severity_rank() / sort_flagged() の単体テスト（標準ライブラリの assert のみ、pytest不要）。

使い方:
    python3 poc/test_severity.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reverse_search import severity_rank, sort_flagged  # noqa: E402


def test_severity_rank_order():
    assert severity_rank("完全一致") < severity_rank("部分一致")
    assert severity_rank("部分一致") < severity_rank("掲載")
    assert severity_rank("掲載") < severity_rank("類似")


def test_severity_rank_unknown_is_lowest():
    assert severity_rank("不明なkind") > severity_rank("類似")


def test_sort_flagged_orders_by_severity_and_is_stable():
    summary = {
        "flagged_pages": [
            ("メルカリ", "https://a", "掲載", "A"),
            ("minne", "https://b", "完全一致", "B"),
            ("Creema", "https://c", "部分一致", "C"),
            ("BASE", "https://d", "完全一致", "D"),  # 同ランク内で元の順序を維持するか確認
        ],
        "flagged_direct": [
            ("メルカリ", "https://x", "部分一致"),
            ("minne", "https://y", "完全一致"),
        ],
    }
    result = sort_flagged(summary)

    pages_kinds = [t[2] for t in result["flagged_pages"]]
    assert pages_kinds == ["完全一致", "完全一致", "部分一致", "掲載"]
    # 同ランク（完全一致）内では元の順序（B → D）が保たれる（安定ソート）。
    pages_titles = [t[3] for t in result["flagged_pages"]]
    assert pages_titles.index("B") < pages_titles.index("D")

    direct_kinds = [t[2] for t in result["flagged_direct"]]
    assert direct_kinds == ["完全一致", "部分一致"]


def test_sort_flagged_does_not_mutate_input():
    summary = {
        "flagged_pages": [("メルカリ", "https://a", "掲載", "A"), ("minne", "https://b", "完全一致", "B")],
        "flagged_direct": [],
    }
    original_order = list(summary["flagged_pages"])
    sort_flagged(summary)
    assert summary["flagged_pages"] == original_order


def main():
    tests = [
        test_severity_rank_order,
        test_severity_rank_unknown_is_lowest,
        test_sort_flagged_orders_by_severity_and_is_stable,
        test_sort_flagged_does_not_mutate_input,
    ]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"\n全 {len(tests)} 件のテストに成功しました。")


if __name__ == "__main__":
    main()
