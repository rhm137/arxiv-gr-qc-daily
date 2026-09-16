# arXiv 多分区每日摘要（云端运行）

自动汇总 arXiv 三个分区的每日新论文，DeepSeek 中文翻译 + 四段式评价，
生成 HTML 报告发布到 GitHub Pages，并推送到微信（PushPlus）。

> 注意：本仓库是**云端推送**项目，代码只在 GitHub Actions 上运行，
> 本地仅用于维护代码（改配置/分区/排查），不需要本地跑通推送。

## 订阅分区

| 分区 | 说明 | 数据来源 |
|------|------|----------|
| `gr-qc` | 引力与量子宇宙学 | `/list/gr-qc/new` |
| `hep-th` | 高能理论物理 | `/list/hep-th/new` |
| `astro-ph` | 天体物理（仅两个子类合并） | `/list/astro-ph.CO/new` + `/list/astro-ph.HE/new` |

astro-ph = CO（宇宙学）+ HE（高能现象）合并去重，New 优先于 Cross。

## 架构

```
cron-job.org（云端，每天 12:00 北京时间，POST workflow_dispatch）
        │
        ▼
GitHub Actions（.github/workflows/daily.yml，job: digest）
        ├─ 1. scripts/run.py：解析 arXiv 官方公告页 /list/{cat}/new（唯一权威来源）
        ├─ 2. arXiv API 补全元数据
        ├─ 3. DeepSeek 逐篇中文翻译 + 四段式评价
        ├─ 4. 生成 HTML → ./outputs-public/
        ├─ 5. 与线上 summary.json 比对，同批次跳过（防重复推送）
        ├─ 6. PushPlus 群组推送（topic: arxiv-gr-qc，仅群组）
        └─ 7. 部署 GitHub Pages（永久链接 latest.html）
```

## 代码结构

```
.
├── .github/workflows/daily.yml   # 流水线（唯一被 Actions 调用的入口在此定义）
├── scripts/
│   ├── run.py                    # ★ 核心：抓取→翻译→生成HTML→写 summary，单一入口
│   ├── fetch_papers.py           # （历史遗留，逻辑已并入 run.py，不再被调用）
│   ├── translate_cloud.py        # （历史遗留，同上）
│   ├── build_html.py             # （历史遗留，同上）
│   └── build_hub.py              # （历史遗留，同上）
├── .gitignore
└── README.md
```

**只有两个文件是活的**：`scripts/run.py`（逻辑）和 `.github/workflows/daily.yml`（流水线）。
`scripts/` 下其余脚本是早期拆分版本，run.py 已整合其逻辑，改代码只改 run.py。

## run.py 用法

```bash
python scripts/run.py [--date YYYY-MM-DD] [--cats gr-qc hep-th astro-ph] [--out ./outputs-public] [--wait-minutes 45]
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `--date` | 空 | 期望批次日期，仅校验用；实际抓取以官网 `/new` 当前批次为准 |
| `--cats` | 三个分区全抓 | 只抓指定分区 |
| `--out` | `./outputs-public` | HTML 输出目录（运行时生成） |
| `--wait-minutes` | `45` | 若批次未发布，每 10 分钟探测、最多等这么久 |

环境变量：

| 变量 | 默认 | 说明 |
|------|------|------|
| `DEEPSEEK_API_KEY` | 必填 | DeepSeek 翻译密钥 |
| `LLM_MODEL` | `deepseek-chat` | 翻译模型，可覆盖 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | API 地址，可覆盖 |

## GitHub Secrets

| Secret 名 | 用途 |
|-----------|------|
| `DEEPSEEK_API_KEY` | DeepSeek 翻译 |
| `PUSHPLUS_TOKEN` | PushPlus 微信推送 |

## 手动触发一次

仓库 → Actions → 左侧 "arXiv Multi-Category Daily Digest" → Run workflow → 分支 main → Run workflow。
`date` 留空即可（抓取官网当前批次）。若该批次当天已推过会空跑（正常，非故障）。

## 批次规则（改代码前必读）

arXiv 公告在美东 **Sun/Mon/Tue/Wed/Thu 20:00** 发布（Fri/Sat 无公告），
公告产生的是**次日标签**的批次（官方："mailed Thursday night / Friday morning"），
listing 页约在美东**午夜**（= 北京 12:00 夏令时 / 13:00 冬令时）翻页到新批次。
批次标签只有 Mon–Fri。北京时间 12:00 触发正好在翻页边界，
`_expected_label()` 已按此模型校准（等待循环兜底）。
批次成员以官网 `/list/{cat}/new` 页面为唯一权威来源
（部分论文因审核挂起会延迟数日，任何时间窗推算都不可靠）。

详见迁移包文档 `03-arXiv批次规则与抓取机制.md`（不在本仓库内）。
