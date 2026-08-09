#!/usr/bin/env python3
"""
同步上游 sing-box 规则集，移除私货字符串，生成详细变更日志。
支持本地测试：若本地没有 upstream_repo 目录，可先手动执行
git clone https://github.com/SukkaLab/ruleset.skk.moe.git upstream_repo
"""
import json
import os
import hashlib
import sys
from typing import Dict, Tuple

UPSTREAM_DIR = "upstream_repo/sing-box"   # 上游仓库签出后的 sing-box 目录
OUTPUT_DIR = "."                          # 本仓库根目录
CONFIG_FILE = "sync_config.json"
SUMMARY_FILE = "sync_summary.txt"

def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        print(f"错误：配置文件 {CONFIG_FILE} 不存在")
        sys.exit(1)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def clean_rules(ruleset: dict, config: dict) -> Tuple[dict, dict]:
    """
    返回 (清理后的规则集, 统计信息)。
    统计包含移除的条目数、清空的字段、丢弃的规则等。
    """
    if "rules" not in ruleset:
        return ruleset, {}

    blacklist = config.get("blacklist_strings", [])
    if not blacklist:
        return ruleset, {"total_rules": len(ruleset["rules"])}

    target_fields = ["domain", "domain_suffix", "domain_keyword"]
    stats = {
        "total_rules": len(ruleset["rules"]),
        "removed_rules": 0,
        "domain_removed": 0,
        "domain_suffix_removed": 0,
        "domain_keyword_removed": 0,
        "fields_cleared": []
    }

    new_rules = []
    for rule in ruleset["rules"]:
        cleaned_rule = {}
        for field, value in rule.items():
            if field in target_fields and isinstance(value, list):
                before = len(value)
                filtered = [item for item in value
                            if not any(bl in item for bl in blacklist)]
                after = len(filtered)
                removed = before - after
                if field == "domain":
                    stats["domain_removed"] += removed
                elif field == "domain_suffix":
                    stats["domain_suffix_removed"] += removed
                elif field == "domain_keyword":
                    stats["domain_keyword_removed"] += removed

                if after > 0:
                    cleaned_rule[field] = filtered
                else:
                    stats["fields_cleared"].append(f"{field}({before} items cleared)")
            else:
                cleaned_rule[field] = value

        if cleaned_rule:
            new_rules.append(cleaned_rule)
        else:
            stats["removed_rules"] += 1

    if not new_rules:
        return None, stats
    ruleset["rules"] = new_rules
    return ruleset, stats

def md5(data: dict) -> str:
    return hashlib.md5(
        json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()

def main():
    # 检查上游目录是否存在
    if not os.path.isdir(UPSTREAM_DIR):
        print(f"错误：上游目录 '{UPSTREAM_DIR}' 不存在。")
        print("请先执行以下命令克隆上游仓库：")
        print("  git clone https://github.com/SukkaLab/ruleset.skk.moe.git upstream_repo")
        sys.exit(1)

    config = load_config()
    print("=" * 60)
    print("开始处理上游规则集...")
    print(f"黑名单字符串: {config.get('blacklist_strings', [])}")
    print("=" * 60)

    upstream_files = {}
    clean_stats = {}
    for fname in sorted(os.listdir(UPSTREAM_DIR)):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(UPSTREAM_DIR, fname)
        with open(fpath, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError as e:
                print(f"  ⚠ 无法解析 {fname}: {e}")
                continue
        cleaned, stats = clean_rules(data, config)
        if cleaned is not None:
            upstream_files[fname] = cleaned
            print(f"✓ {fname}: 规则 {stats['total_rules']} 条, "
                  f"域名移除 {stats['domain_removed']}, "
                  f"后缀移除 {stats['domain_suffix_removed']}, "
                  f"关键字移除 {stats['domain_keyword_removed']}, "
                  f"清空字段: {', '.join(stats['fields_cleared']) if stats['fields_cleared'] else '无'}, "
                  f"丢弃空规则 {stats['removed_rules']}")
        else:
            print(f"✗ {fname}: 清理后无有效规则，整个文件被舍弃。")
        clean_stats[fname] = stats

    # 本地文件列表（排除配置文件）
    local_files = set()
    for f in os.listdir(OUTPUT_DIR):
        if f.endswith(".json") and f != CONFIG_FILE:
            local_files.add(f)

    new_files = set(upstream_files.keys())
    removed = local_files - new_files
    added = new_files - local_files
    common = local_files & new_files

    change_actions = []

    # 新增文件
    for fname in sorted(added):
        print(f"[新增] {fname}")
        with open(os.path.join(OUTPUT_DIR, fname), "w", encoding="utf-8") as f:
            json.dump(upstream_files[fname], f, indent=2, ensure_ascii=False)
        change_actions.append(f"Added {fname}")

    # 更新已有文件
    for fname in sorted(common):
        local_path = os.path.join(OUTPUT_DIR, fname)
        with open(local_path, "r", encoding="utf-8") as f:
            try:
                local_data = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                local_data = {}
        if md5(local_data) != md5(upstream_files[fname]):
            print(f"[更新] {fname}")
            with open(local_path, "w", encoding="utf-8") as f:
                json.dump(upstream_files[fname], f, indent=2, ensure_ascii=False)
            change_actions.append(f"Updated {fname}")
        else:
            print(f"[未变化] {fname}")

    # 删除多余文件
    for fname in sorted(removed):
        print(f"[删除] {fname}")
        os.remove(os.path.join(OUTPUT_DIR, fname))
        change_actions.append(f"Removed {fname}")

    # 生成摘要文件
    summary_lines = []
    if change_actions:
        summary_lines.append("Sync: " + ", ".join(change_actions))
        summary_lines.append("\nCleaning Details:")
        for fname, stats in clean_stats.items():
            if fname in upstream_files:
                summary_lines.append(
                    f"  {fname}: rules {stats['total_rules']}, "
                    f"domain_removed {stats['domain_removed']}, "
                    f"suffix_removed {stats['domain_suffix_removed']}, "
                    f"keyword_removed {stats['domain_keyword_removed']}, "
                    f"emptied_fields: {stats['fields_cleared']}, "
                    f"dropped_empty_rules: {stats['removed_rules']}"
                )
            else:
                summary_lines.append(f"  {fname}: Discarded entirely")
    else:
        summary_lines.append("No changes detected.")

    summary_text = "\n".join(summary_lines)
    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        f.write(summary_text)

    print("\n" + "=" * 60)
    print("变更摘要:")
    print(summary_text)
    print("=" * 60)

    if change_actions:
        print("检测到变化，将在 GitHub Actions 中触发提交和 Release（本地测试不会执行这些操作）。")

if __name__ == "__main__":
    main()