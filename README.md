# sing-box 规则同步、清洗与 SRS 编译

从一个或多个 upstream GitHub 仓库镜像 sing-box JSON 规则集（加上你自己维护的本地 JSON），剔除黑名单域名，全部编译为 sing-box 的二进制 `.srs` 格式，发布 jsDelivr CDN 链接，并在有任何实际更新时自动打上带时间戳的 Release。

## 目录结构

```
sync.json               # 数据源、输出路径、CDN 设置
blacklist.json          # 需剔除的匹配字符串 — 随时编辑，无需修改代码
sync_and_clean.py        # 完整处理管道
custom/                  # 可选：你自己手动维护的 JSON 规则集
rules/                   # 自动生成：清洗后的 JSON，每个数据源对应一个目录树
srs/                     # 自动生成：编译后的 .srs，每个数据源对应一个目录树
logs/                    # 自动生成：单次运行的详细日志 + Release 摘要
CHANGELOG.md             # 自动生成：历史运行日志汇总
ACCESS_LINKS.md          # 自动生成：本次运行涉及的所有文件的 jsDelivr 链接
.github/workflows/sync.yml
```

## 工作原理

1. **`.github/workflows/sync.yml`** 使用浅克隆、无 blob、稀疏克隆（shallow, blobless, sparse `git clone`）拉取配置的所有数据源 — 不需要 GitHub API 调用，公开仓库不需要 token，也不会触发速率限制。每个数据源存放在 `upstream/@<owner>/<repo>/<branch>/<directory_name>/`。
2. **`sync_and_clean.py`** 针对每个数据源以及本地 `custom/` 目录：
    - 遍历 `domain`、`domain_suffix` 或 `domain_keyword` 列表，剔除其值**包含**黑名单字符串（不区分大小写）的条目。
    - 如果某个字段清洗后变为空，则直接删除该字段。
    - 如果某条规则的所有字段均被删除，则剔除该条规则。
    - 如果某个文件的所有规则均被剔除，则直接剔除该文件。
    - 将清洗后的 JSON 写入到 `rules/<namespace>/<...>.json`。
    - 编译为 sing-box 的 `.srs` 二进制格式，并保存为两种路径：
        - `srs/<namespace>/<...>/<date>/<file>.srs` — 带日期的快照，永久保存
        - `srs/<namespace>/<...>/<file>.srs` — "latest" 最新版本，始终保持最新，不带日期
3. 本次运行处理的每一个文件，都会在 **`ACCESS_LINKS.md`** 中生成对应的 jsDelivr 链接（包括路径及包含 URL 的代码块）。
4. 系统会生成详细日志（`logs/sync_<ts>.log`）和 Release 摘要（`logs/summary_<ts>.md`）；摘要会自动追加到 `CHANGELOG.md` 中。
5. 工作流每 12 小时（或手动触发）运行一次。只有在产生实际内容变更时，才会提交代码、打上 `vYYYYMMDD_HHMMSS` 标签，并发布 GitHub Release（以摘要作为 Release 说明）。

如果没有变更，则不会产生任何提交、标签或 Release。

## 命名空间

`rules/` 和 `srs/` 均使用完全相同的命名空间前缀，因此即使不同数据源包含同名文件也不会产生冲突：

```
rules/@<owner>/<repo>/<branch>/<directory_name>/<...>.json
srs/@<owner>/<repo>/<branch>/<directory_name>/<...>/<date>/<file>.srs
srs/@<owner>/<repo>/<branch>/<directory_name>/<...>/<file>.srs
```

`upstream_path` 和 `directory_name` 均支持多层级路径（例如 `"sing-box/Clash"`） — 所有路径处理均基于 `pathlib` 构建，且 Git 的 sparse-checkout（cone 模式，Git 2.25 起默认启用）直接支持嵌套目录。

`custom/` 遵循相同的模式，但使用固定的无 owner 命名空间 — `rules/custom/<...>.json` 和 `srs/custom/<...>.srs` — 并且同样支持其下方的任意多级子目录（例如 `custom/mygroup/ads/blocklist.json`）。该目录下的文件不会从外部拉取，由你在仓库中直接维护，每次运行都会经历与其他数据源完全相同的清洗 + 编译流程。如果 `custom/` 不存在或为空，则会被静默跳过。

## 配置说明

### `sync.json`
```jsonc
{
  "sources": [
    {
      "owner": "SukkaLab",
      "repo": "ruleset.skk.moe",
      "branch": "master",
      "upstream_path": "sing-box",
      "directory_name": "sing-box"   // 可选 — 默认为 upstream_path 的最后一个路径段
    }
    // 可在此添加更多条目以支持其他 upstream 仓库/目录
  ],

  "local_output_root": "rules",
  "upstream_checkout_root": "upstream",

  "enable_custom": true,
  "custom_dir_name": "custom",

  "sing_box_bin": "sing-box",

  "cdn_base_url": "https://testingcf.jsdelivr.net/gh",
  "cdn_ref_mode": "tag",      // "tag" (固定日期的文件) 或 "branch" (保持最新的文件)
  "cdn_branch": "master"
}
```

### `blacklist.json`
```jsonc
{
  "blacklist": [
    "example-bad-domain.com",
    "ads.example.net"
  ]
}
```
匹配规则为不区分大小写的子字符串检测，对数组中的每个元素生效。

## 手动运行

```bash
# 手动填充 upstream/ 目录（工作流中会按数据源自动处理）
git clone --depth 1 --filter=blob:none --sparse \
  --branch master https://github.com/SukkaLab/ruleset.skk.moe.git tmp_clone
(cd tmp_clone && git sparse-checkout set sing-box)
mkdir -p "upstream/@SukkaLab/ruleset.skk.moe/master/sing-box"
cp -r tmp_clone/sing-box/. "upstream/@SukkaLab/ruleset.skk.moe/master/sing-box/"
rm -rf tmp_clone

python3 sync_and_clean.py
```

必须将 `sing-box` 添加到系统 `PATH` 中才能成功编译 `.srs` — 如果找不到 `sing-box`，脚本会记录该文件的编译失败日志并继续运行（依然会生成 JSON 输出和链接，只是不会生成该文件的 `.srs` 及对应链接）。

## 注意事项

- GitHub Workflow 需要 `permissions: contents: write` 权限，以便使用默认 token 推送提交/标签并创建 Release。
- 无论 `cdn_ref_mode` 如何设置，"latest" 最新版的 `.srs` 链接总是基于 `cdn_branch` 生成，因为它的内容每次运行都会变化 — 如果使用指向特定标签的链接，标签之后文件内容发生变化会产生误导。
- "清洗后变为空" 仅将 `invert` 视为非匹配 Key 进行清除 — 其他匹配选择器 Key（如 `ip_cidr`、`process_name` 等）均会被保留，即使其域名相关字段被删光，规则依然有效。
