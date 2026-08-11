# sing-box / mihomo 规则同步、清洗与编译

从一个或多个 upstream GitHub 仓库镜像 sing-box JSON / Clash conf 规则集（加上你自己维护的本地 JSON），剔除黑名单域名，编译为 sing-box 的二进制 `.srs` 格式与 mihomo 的二进制 `.mrs` 格式，发布 jsDelivr CDN 链接，并在有任何实际更新时自动打上带时间戳的 Release。

> **说明**：所有编译生成的文件仅存放于 `release` 分支，主分支（`main`）仅保留源代码与配置文件。

## Release 分支目录结构

```
json/                   # 自动生成：清洗后的 JSON 规则集
conf/                   # 自动生成：转换并清洗后的原 .conf 规则集 JSON
srs/                    # 自动生成：编译后的 sing-box .srs 二进制规则集
mrs/                    # 自动生成：编译后的 mihomo .mrs 二进制规则集
logs/                   # 自动生成：单次运行的详细日志 + Release 摘要
CHANGELOG.md            # 自动生成：历史运行日志汇总
ACCESS_LINKS.md         # 自动生成：本次运行涉及的所有文件的 jsDelivr 链接（基于 release 分支）
```

## 工作原理

1. **`.github/workflows/build.yml`** 使用浅克隆、无 blob、稀疏克隆（shallow, blobless, sparse `git clone`）拉取配置的所有数据源 — 不需要 GitHub API 调用，公开仓库不需要 token，也不会触发速率限制。每个数据源存放在 `upstream/@<owner>/<repo>/<branch>/<directory_name>/`。
2. **`sync_and_clean.py`** 针对每个数据源以及本地 `custom/` 目录：
    - 遍历 `domain`、`domain_suffix` 或 `domain_keyword` 列表，剔除其值**包含**黑名单字符串（不区分大小写）的条目。
    - 如果某个字段清洗后变为空，则直接删除该字段。
    - 如果某条规则的所有字段均被删除，则剔除该条规则。
    - 如果某个文件的所有规则均被剔除，则直接剔除该文件。
    - 将清洗后的 JSON 写入到 `json/<namespace>/<...>.json`，将转换/清洗后的 `.conf` 写入 `conf/<namespace>/<...>.json`。
    - 编译为 sing-box 的 `.srs` 二进制格式与 mihomo 的 `.mrs` 二进制格式，并保存为两种路径：
        - `<fmt>/<namespace>/<...>/<date>/<file>.<fmt>` — 带日期的快照，永久保存
        - `<fmt>/<namespace>/<...>/<file>.<fmt>` — "latest" 最新版本，始终保持最新，不带日期
3. 本次运行处理的每一个文件，都会在 **`ACCESS_LINKS.md`** 中生成对应的 jsDelivr 链接（基于 `release` 分支）。
4. 工作流在检测到变更后，将编译产物及 `ACCESS_LINKS.md` 仅提交并推送到 **`release`** 分支，并打上 `vYYYYMMDD_HHMMSS` 标签发布 GitHub Release。

## 命名空间与目录结构

`json/`、`conf/`、`srs/` 和 `mrs/` 均使用完全相同的命名空间前缀，在 `release` 分支上的目录结构如下：

```
json/@<owner>/<repo>/<branch>/<directory_name>/<...>.json
conf/@<owner>/<repo>/<branch>/<directory_name>/<...>.json
srs/@<owner>/<repo>/<branch>/<directory_name>/<...>/<date>/<file>.srs
srs/@<owner>/<repo>/<branch>/<directory_name>/<...>/<file>.srs
mrs/@<owner>/<repo>/<branch>/<directory_name>/<...>/<date>/<file>.mrs
mrs/@<owner>/<repo>/<branch>/<directory_name>/<...>/<file>.mrs
```

`upstream_path` 和 `directory_name` 均支持多层级路径（例如 `"sing-box/Clash"`）。

`custom/` 遵循相同的模式，使用固定的无 owner 命名空间 — `json/custom/<...>.json`、`srs/custom/<...>.srs`、`mrs/custom/<...>.mrs`。

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
  ],

  "local_output_root": "json",
  "upstream_checkout_root": "upstream",

  "enable_custom": true,
  "custom_dir_name": "custom",

  "sing_box_bin": "sing-box",
  "mihomo_bin": "mihomo",

  "cdn_base_url": "https://testingcf.jsdelivr.net/gh",
  "cdn_ref_mode": "tag",      // "tag" (固定日期的文件) 或 "branch" (保持最新的文件)
  "cdn_branch": "release"
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

## 手动运行

```bash
python3 sync_and_clean.py
```
