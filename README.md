# PubMed 文献晨报

这是一个自动更新的 PubMed 文献晨报项目。它会每天检索与下面方向相关的新文献，并生成中文 Markdown 日报，放在 `docs/reports/` 里，再自动更新 `docs/index.md` 作为归档首页。

研究方向包括：环境低剂量污染物暴露、肾毒性、肾纤维化、HK-2 细胞、m6A、METTL3、PRODH、单细胞组学、类器官、环境流行病学与机制研究结合。

## 每天会生成什么

每天的日报会包含：

- 今日总体判断
- 今日最值得读的 5 篇文章
- 每篇文章的题目、期刊、年份、PMID、DOI、PubMed 链接
- 研究对象、核心方法、主要发现、为什么值得读
- 按“环境流行病学 / 机制实验 / 单细胞组学 / 类器官 / 肾毒性 / m6A-METTL3-PRODH”分类
- 今日阅读优先级
- Mermaid 思维导图

如果近 24 小时内没有高质量结果，脚本会自动扩大到近 7 天，并在日报中说明原因。如果仍然没有合适文献，也会生成当天日报，说明“今日无高质量新文献”。

## 如何修改关键词

打开 [`config.yaml`](config.yaml)，主要修改这几处：

- `search.pubmed_query`：PubMed 检索式，影响实际检索范围。
- `search.priority_keywords`：用于规则评分的重点词，越匹配越可能被推荐。
- `search.categories`：用于把文章分到不同栏目。
- `search.high_quality_score`：高质量候选的最低分数，数字越高越严格。

修改后可以手动运行一次，确认效果：

```bash
python scripts/generate_daily.py
```

## 如何手动运行

在 GitHub 仓库页面：

1. 点击顶部的 `Actions`。
2. 选择左侧的 `Daily PubMed Report`。
3. 点击 `Run workflow`。
4. 等待运行完成后，新的日报会自动提交到仓库。

在本地运行：

```bash
pip install -r requirements.txt
python scripts/generate_daily.py
```

## 如何开启 GitHub Pages

在 GitHub 仓库页面：

1. 打开 `Settings`。
2. 点击左侧 `Pages`。
3. `Source` 选择 `Deploy from a branch`。
4. `Branch` 选择 `main`，目录选择 `/docs`。
5. 保存后等待 GitHub Pages 构建完成。

## 每天生成的网页在哪里看

开启 GitHub Pages 后，归档首页通常是：

```text
https://你的GitHub用户名.github.io/pubmed-daily/
```

每天的日报会显示在首页的“日报归档”里，最新日期排在最前。

## 自动运行时间

GitHub Actions 使用 UTC 时间。本项目当前设置为每天 `08:00 UTC` 运行，约等于英国夏令时早上 9 点。

英国冬令时早上 9 点约等于 `09:00 UTC`。如果你希望冬令时也严格在英国时间早上 9 点运行，可以把 `.github/workflows/daily.yml` 里的：

```yaml
- cron: "0 8 * * *"
```

改成：

```yaml
- cron: "0 9 * * *"
```

## 说明

这个版本不需要 OpenAI API key。文章筛选和中文总结都基于标题、摘要、关键词、期刊信息和规则化模板完成，适合先免费跑起来。后续如果你想加入大模型精读总结，可以在这个基础上继续扩展。
