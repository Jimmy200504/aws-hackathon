"""
清理 userSearchLog_20260601_20260607.csv

移除類別：
1. SEO spam：ks 含百度/霸屏/快排/谷歌/QQ/tgbot 等垃圾關鍵字
2. URL 搜尋：ks 以 http:// / https:// / www. 開頭
3. (可選) talentNo=0 且 c0=111102 且 empStr 為空 — 幾乎全是 spam

保留但標注：
- talentNo=0 / -1：無法歸戶，保留供趨勢分析
- empStr 為空：搜尋無結果，保留供需求缺口分析

輸出：
- userSearchLog_cleaned.csv：清理後資料
- cleaning_report.txt：清理摘要報告
"""

import csv
import re
import sys
from collections import Counter

INPUT_FILE = "userSearchLog_20260601_20260607.csv"
OUTPUT_FILE = "userSearchLog_cleaned.csv"
REPORT_FILE = "cleaning_report.txt"

# Spam patterns in ks field
SPAM_KS_PATTERN = re.compile(
    r"百度|霸屏|快排|谷歌技术|谷歌技術|"
    r"QQ：|QQ:|"
    r"www\.tgbot|"
    r"老船长|圣淘沙客服|"
    r"关键词.*优化|關鍵詞.*優化",
    re.IGNORECASE,
)

# URL pattern in ks field
URL_KS_PATTERN = re.compile(r"^https?://|^www\.", re.IGNORECASE)


def is_spam(row):
    """判斷是否為垃圾資料，回傳 (是否spam, 原因)"""
    talent_no = row["talentNo"]
    ks = row["ks"]
    c0 = row["c0"]
    emp_str = row["empStr"]

    # Rule 1: ks 含 spam 關鍵字
    if SPAM_KS_PATTERN.search(ks):
        return True, "spam_keyword"

    # Rule 2: ks 是 URL
    if URL_KS_PATTERN.match(ks):
        return True, "url_as_keyword"

    # Rule 3: talentNo=0 + c0 為中國地區碼(111xxx) + empStr 為空
    # 111102 是保定市等中國地區碼，正常台灣求職不會搜尋這些
    if talent_no == "0" and c0.startswith("111") and emp_str.strip() == "":
        return True, "china_region_no_result"

    return False, ""


def main():
    stats = {
        "total": 0,
        "kept": 0,
        "removed": 0,
        "reasons": Counter(),
        "talent_zero": 0,
        "talent_neg1": 0,
        "empty_empstr": 0,
    }

    print(f"讀取 {INPUT_FILE} ...")

    with (
        open(INPUT_FILE, "r", encoding="utf-8") as fin,
        open(OUTPUT_FILE, "w", encoding="utf-8", newline="") as fout,
    ):
        reader = csv.DictReader(fin)
        writer = csv.DictWriter(fout, fieldnames=reader.fieldnames)
        writer.writeheader()

        for i, row in enumerate(reader, 1):
            stats["total"] += 1

            # 統計資訊
            if row["talentNo"] == "0":
                stats["talent_zero"] += 1
            elif row["talentNo"] == "-1":
                stats["talent_neg1"] += 1
            if row["empStr"].strip() == "":
                stats["empty_empstr"] += 1

            # 判斷是否需要移除
            spam, reason = is_spam(row)
            if spam:
                stats["removed"] += 1
                stats["reasons"][reason] += 1
            else:
                stats["kept"] += 1
                writer.writerow(row)

            if i % 1_000_000 == 0:
                print(f"  已處理 {i:,} 筆 (移除 {stats['removed']:,}) ...")

    # 產生報告
    report_lines = [
        "=" * 60,
        "userSearchLog 資料清理報告",
        "=" * 60,
        f"",
        f"輸入檔案：{INPUT_FILE}",
        f"輸出檔案：{OUTPUT_FILE}",
        f"",
        f"總筆數：{stats['total']:,}",
        f"保留筆數：{stats['kept']:,} ({stats['kept']/stats['total']*100:.2f}%)",
        f"移除筆數：{stats['removed']:,} ({stats['removed']/stats['total']*100:.2f}%)",
        f"",
        f"--- 移除原因明細 ---",
    ]
    for reason, count in stats["reasons"].most_common():
        report_lines.append(f"  {reason}: {count:,}")

    report_lines += [
        f"",
        f"--- 保留資料中的注意事項 ---",
        f"  talentNo=0 (未登入/無法辨識)：{stats['talent_zero']:,} 筆",
        f"  talentNo=-1 (無法辨識)：{stats['talent_neg1']:,} 筆",
        f"  empStr 為空 (搜尋無結果)：{stats['empty_empstr']:,} 筆",
        f"",
        f"建議：",
        f"  - 做使用者行為歸戶分析時，排除 talentNo <= 0",
        f"  - 做搜尋→瀏覽→應徵漏斗時，排除 empStr 為空的紀錄",
        f"  - 做熱門關鍵字/需求缺口分析時，可保留所有清理後資料",
        "=" * 60,
    ]

    report = "\n".join(report_lines)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\n完成！")
    print(report)


if __name__ == "__main__":
    main()
