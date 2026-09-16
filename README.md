# PYGL Research Radar v1

PYGL Research Radar 是一个证据感知的每日生物医学文献筛选器。它从 PubMed 与 Crossref 的 topic lane 和高层期刊 journal-first lane 联合检索过去 48 小时的候选论文，批量 LLM 初筛，再对保留论文尝试合法开放获取全文，按“实验结构可迁移性”优先于关键词重合的规则排序，生成中文 Markdown/HTML 简报，发布为 GitHub Issue 备份，并通过微信公众号测试账号模板消息推送公开 Pages 日报。

它服务于巨噬细胞 efferocytosis / phagosome-lysosome processing 研究，特别关注可迁移的实验模式：conditioned medium → fractionation → ligand → blockade/add-back、抑制剂 → 激活/激动剂 → rescue、上游阻断 → 下游旁路/epistasis、急性磷酸化 → 代谢 → 功能表型、吞噬结合完整但吞噬后处理受损，以及 injury-first → delayed intervention → resolution。

## Evidence guardrails

每篇论文恰好标记为 `FULLTEXT_READ` 或 `ABSTRACT_ONLY`。

- `ABSTRACT_ONLY` 只能生成摘要层面的总结；流水线会清除图号、剂量、样本量等全文级断言，并把未能获取全文记录为正常的部分完成。
- `FULLTEXT_READ` 只有在 PMC/Europe PMC XML、解析出 substantial text 的 OA PDF，或带 article-body/section 结构的 OA HTML 返回后才会设置。Unpaywall landing page、paywall、反爬或 CAPTCHA 不会被当成全文。
- 全文来源、DOI/PMID、发布时间、检索来源、retrieval mode、模型和机器可读六维评分都会保存在 JSON 报告中。

## Setup

需要 Python 3.11+：

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[test]"
cp config.example.yaml config.yaml
cp .env.example .env  # 只在本地使用；不要提交
```

编辑 `config.yaml` 中的研究画像、冻结期刊、双 lane 配额、评分权重与输出目录。`journal_candidate_slots` 和 `journal_per_venue_quota` 保证高层期刊优先但不挤掉 topic lane；建议将 `config.yaml` 和 `.env` 保留在本地或通过部署环境注入。

## Local commands

离线 fixture dry-run（不调用网络、不发送微信、不更新 seen cache）：

```bash
python -m pygl_radar --config config.example.yaml --fixture fixtures/sample_papers.json --dry-run --no-fulltext
```

正常扫描与推送：

```bash
python -m pygl_radar --config config.yaml
```

## Codex-native primary mode

Codex Automation 是每日主要科研分析链路：它在 **08:45 Asia/Shanghai** 完成候选检索、实验逻辑优先的 triage、合规 OA hydration、Top 3–5 journal-club 深读和本地验证，然后只提交脱敏的 `codex-output/YYYY-MM-DD.json` 与 `.md`。GitHub Actions 随后只做确定性的 Pages 归档和一条简短 WeChat 入口通知；Codex 不接触任何 WeChat credential。原有 DeepSeek/OpenAI-compatible 流水线保留为 `workflow_dispatch` 手动 benchmark/fallback，不再每天自动运行。

准备阶段不创建或调用任何 LLM API client，继续复用现有 PubMed/Crossref 双 lane、48 小时窗口、去重、期刊配额、candidate cap、seen-state 和可用的 GitHub feedback ingestion：

```bash
python -m pygl_radar codex-prepare --config config.yaml
# 没有 config.yaml 时：
python -m pygl_radar codex-prepare --config config.example.yaml
```

它生成 gitignored 的 `work/YYYY-MM-DD/`，其中 `candidates.json` 是语义 triage 前的完整候选池。Codex 先读完整候选池，再写只引用已知 `paper_id` 的 `shortlist.json`（约 10–15 篇），然后执行：

```bash
python -m pygl_radar codex-hydrate \
  --workspace work/YYYY-MM-DD \
  --shortlist work/YYYY-MM-DD/shortlist.json
# Codex 写入 work/YYYY-MM-DD/codex_reviewed.json 后：
python -m pygl_radar codex-validate --workspace work/YYYY-MM-DD
```

只有验证成功才会生成可发布的 `codex-output/YYYY-MM-DD.json` 和 `.md`。JSON 是唯一的 Pages 数据源，`.md` 是 GitHub 上的人类可读 fallback。输出带有 `review_provider=codex-automation`、`review_mode=codex-native`，严格移除 raw full text；`work/` 不会提交，也不会复制到 GitHub Pages。完整的 Automation prompt、权限边界、证据模式和失败处理见 [`docs/CODEX_AUTOMATION_PROMPT.md`](docs/CODEX_AUTOMATION_PROMPT.md)。

## Web Dashboard / GitHub Pages

每次生产 radar 成功后，Pages job 会从当日 JSON digest 构建静态手机优先网站，并把脱敏后的日报数据保存在 `site/data/reports/`，因此旧日报的永久 URL 不会因后续部署改变：

- `/` 与 `/latest/`：最新一期日报
- `/reports/YYYY-MM-DD/`：某一天的永久日报
- `/archive/`：按日期倒序的历史列表

页面只展示 AI digest、评分、证据状态和 DOI/PubMed/Publisher/合法全文来源链接；`fulltext_text`、原始全文缓存和 secret 不会复制到 Pages。GitHub Issue 仍是日报备份和反馈入口，格式为 `feedback <DOI或PMID> <relevant|idea|method|skip>`。

默认公开地址由 `GITHUB_REPOSITORY` 推导为 project Pages 地址。生产环境可在 Actions Variables 设置 `PUBLIC_SITE_URL`，例如 `https://radar.example.com`；自定义域名由 GitHub Pages 设置和 DNS 管理，不由 renderer 生成 `CNAME`。不需要修改业务代码，也不需要在仓库提交 secret。Project Pages 的 `/pygl-research-radar/` base path 由相对内部链接处理，绑定自定义根域名后同样有效。

### 首次启用 Pages

1. 在仓库 **Settings → Pages** 将 Source 设为 **GitHub Actions**，并允许 Actions 使用 Pages environment。
2. 合并本 PR 后，首次成功的 Codex Automation 只提交一个通过校验的 `codex-output/YYYY-MM-DD.json` + `.md` 对；`Publish validated Codex report` 会自动部署 Pages。PR CI 只 build/test，不会发布。
3. 如果使用自定义域名，在 **Settings → Pages → Custom domain** 填入 `radar.example.com`；在 DNS 服务商添加 `radar` 的 CNAME，目标为 `ywanyi520-coder.github.io`（不包含仓库名）。
4. 在 **Settings → Secrets and variables → Actions → Variables** 设置 `PUBLIC_SITE_URL=https://radar.example.com`。
5. GitHub 验证 DNS 后，在 Pages 设置中启用 HTTPS。Actions workflow 不生成或依赖 `CNAME` 文件。

Pages job 会把经过脱敏的 `site/` 历史回写到默认分支，这是为了让 `/reports/YYYY-MM-DD/` 永久稳定；这属于有意的 v1 运维行为，后续可以迁移到独立 history branch。

反馈标签只做有界的软偏好修正，不会永久排除陌生机制。报告 Issue 评论格式为 `feedback <DOI或PMID> <relevant|idea|method|skip>`；下一次 Actions 运行会学习期刊、机制与实验模式偏好：

```bash
python -m pygl_radar --config config.yaml feedback --paper-id 10.1234/example --label relevant
```

运行测试：

```bash
pytest
```

## GitHub Actions secrets

`.github/workflows/publish-codex-report.yml` 只在 `main` 收到经过提交边界校验的 `codex-output/*.json` 时运行：它不调用 LLM、PubMed、Crossref 或 DeepSeek，且 site/ 历史回写不会递归触发。`.github/workflows/daily-radar.yml` 现在只支持 `workflow_dispatch`，作为 DeepSeek 手动 fallback。需要在仓库 Settings → Secrets and variables → Actions 中配置：

| Secret | 用途 |
| --- | --- |
| `WECHAT_APP_ID` | 微信公众号测试账号 AppID |
| `WECHAT_APP_SECRET` | 微信公众号测试账号 AppSecret |
| `WECHAT_OPEN_ID` | 接收消息的用户 OpenID |
| `WECHAT_TEMPLATE_ID` | 模板消息 ID |
| `OPENAI_API_KEY` | OpenAI-compatible triage/review API；缺失时使用确定性 fallback |
| `OPENAI_BASE_URL` | 可选，兼容 OpenAI Chat Completions 的服务地址 |
| `RADAR_TRIAGE_MODEL` | 可选，批量初筛模型名 |
| `RADAR_REVIEW_MODEL` | 可选，深度复核模型名 |
| `UNPAYWALL_EMAIL` | 可选，启用 Unpaywall 合规 OA 查询所需的联系邮箱 |

不要把 AppID、AppSecret、OpenID、模板 ID、模型 key 或邮箱写入代码、YAML、fixture、日志或 PR。Codex 绝不访问或存储四个 WeChat 值；只有 Actions 的 notification job 通过 `${{ secrets.* }}` 读取它们。Codex JSON 先由确定性 publish-check 再次验证，Pages 失败时通知回退到该提交不可变的 GitHub Markdown 报告；校验失败时不部署也不发送日报通知。

微信 notifier 是独立抽象。`MockNotifier` 用于 dry-run 和测试；`WeChatNotifier` 使用官方测试账号/模板消息接口，token 与发送错误不会把 secret 或 token 写入日志。

## Pipeline boundary

1. PubMed/Crossref 独立检索，任一来源失败仍保留另一来源结果。
2. 规范化 DOI/PMID/title 去重；生产 deferred 模式在 Issue 发布成功后写入 seen cache，普通模式在通知成功后写入。
3. 最多约 80 篇候选进入批量 triage，最多 15 篇深审。
4. 评分为 `direct_relevance`、`mechanism_relevance`、`experimental_similarity`、`transferability`、`idea_value`、`evidence_quality` 六个 0–100 分量；实验相似性、可迁移性与 idea value 权重高于直接关键词相关性。
5. 只输出达到质量门槛的最多 3–5 篇；质量不足时明确输出“没有达到门槛”，不凑数。

参考项目检查：实现前检查了 [zcz718/paperradar](https://github.com/zcz718/paperradar) 的多源/候选池/OA provenance 思路和 [OpenRaiser/PaperFlow](https://github.com/OpenRaiser/PaperFlow) 的用户画像/反馈持久化思路。本仓库重新实现了所需的小型模块，没有复制其代码，因此不引入其运行时或第三方代码声明。

## Layout

```text
pygl_radar/
├── sources/       # PubMed/Crossref adapters and injectable HTTP client
├── dedup.py       # DOI/PMID/title identity and pushed-paper cache
├── triage.py      # batch LLM triage + deterministic fallback
├── fulltext.py    # PMC/Europe PMC/Unpaywall/OA lawful acquisition chain
├── review.py      # evidence-aware deep review and claim sanitizer
├── scoring.py     # machine-readable weighted six-axis scoring
├── digest.py      # Chinese Markdown/HTML/WeChat rendering
├── pages.py       # static Pages renderer and permanent report/archive URLs
├── codex_mode.py  # deterministic Codex workspace, OA hydration, and validation
├── notifiers/     # MockNotifier and WeChat template-message notifier
├── feedback.py    # issue-comment ingestion and bounded tag-level soft preference modifier
├── publishing.py  # mobile-readable GitHub Issue report publisher
└── pipeline.py    # end-to-end orchestrator
```
