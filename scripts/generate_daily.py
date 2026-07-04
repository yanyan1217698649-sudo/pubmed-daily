#!/usr/bin/env python3
"""Generate a Chinese PubMed daily report without using any paid AI API."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.yaml"
SEEN_PATH = ROOT / "data" / "seen_pmids.json"
DOCS_DIR = ROOT / "docs"
REPORTS_DIR = DOCS_DIR / "reports"
INDEX_PATH = DOCS_DIR / "index.md"
NCBI_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
NCBI_EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


@dataclass
class Article:
    pmid: str
    title: str
    journal: str = "未知期刊"
    year: str = "未知年份"
    abstract: str = ""
    doi: str = ""
    keywords: list[str] = field(default_factory=list)
    publication_types: list[str] = field(default_factory=list)
    score: int = 0
    categories: list[str] = field(default_factory=list)
    is_seen: bool = False

    @property
    def pubmed_url(self) -> str:
        return f"https://pubmed.ncbi.nlm.nih.gov/{self.pmid}/"

    @property
    def combined_text(self) -> str:
        return " ".join([self.title, self.abstract, " ".join(self.keywords)]).lower()


def load_config(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore

        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except ModuleNotFoundError:
        print("提示：未安装 PyYAML，正在使用内置简易 YAML 读取器。", file=sys.stderr)
        return load_simple_yaml(path)


def load_simple_yaml(path: Path) -> dict[str, Any]:
    """Tiny fallback parser for this project's config shape.

    GitHub Actions installs PyYAML. This fallback only exists so beginners can
    often run the script locally before installing dependencies.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]
    index = 0

    def parse_scalar(raw: str) -> Any:
        value = raw.strip()
        if value == "":
            return ""
        if value in {"''", '""'}:
            return ""
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            return value[1:-1]
        if value.lower() in {"true", "false"}:
            return value.lower() == "true"
        try:
            if "." in value:
                return float(value)
            return int(value)
        except ValueError:
            return value

    while index < len(lines):
        raw_line = lines[index]
        index += 1
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        text = raw_line.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if text.startswith("- "):
            if not isinstance(parent, list):
                raise ValueError("简易 YAML 读取器只支持标准列表缩进。")
            parent.append(parse_scalar(text[2:]))
            continue

        key, sep, value = text.partition(":")
        if not sep:
            continue
        key = parse_scalar(key)
        value = value.strip()

        if value == ">":
            parts: list[str] = []
            while index < len(lines):
                next_line = lines[index]
                next_indent = len(next_line) - len(next_line.lstrip(" "))
                if next_line.strip() and next_indent <= indent:
                    break
                stripped = next_line.strip()
                if stripped and not stripped.startswith("#"):
                    parts.append(stripped)
                index += 1
            parent[key] = " ".join(parts)
            continue

        if value:
            parent[key] = parse_scalar(value)
            continue

        next_kind = "dict"
        probe = index
        while probe < len(lines):
            probe_text = lines[probe].strip()
            if probe_text and not probe_text.startswith("#"):
                next_kind = "list" if probe_text.startswith("- ") else "dict"
                break
            probe += 1
        child: Any = [] if next_kind == "list" else {}
        parent[key] = child
        stack.append((indent, child))
    return root


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_query(query: str) -> str:
    return clean_text(query).replace(" (", "(").replace(" )", ")")


def ncbi_get(url: str, params: dict[str, Any], delay: float) -> bytes:
    if delay > 0:
        time.sleep(delay)
    encoded = urllib.parse.urlencode({k: v for k, v in params.items() if v not in {"", None}})
    request = urllib.request.Request(
        f"{url}?{encoded}",
        headers={"User-Agent": "pubmed-daily/1.0 (https://github.com/)"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def search_pubmed(config: dict[str, Any], days: int) -> list[str]:
    ncbi = config["ncbi"]
    params: dict[str, Any] = {
        "db": "pubmed",
        "term": normalize_query(config["search"]["pubmed_query"]),
        "retmode": "json",
        "retmax": int(ncbi.get("retmax", 80)),
        "sort": "pub date",
        "datetype": "edat",
        "reldate": days,
        "tool": ncbi.get("tool", "pubmed_daily"),
        "email": ncbi.get("email", ""),
        "api_key": ncbi.get("api_key", ""),
    }
    raw = ncbi_get(NCBI_ESEARCH, params, float(ncbi.get("request_delay_seconds", 0.34)))
    data = json.loads(raw.decode("utf-8"))
    return data.get("esearchresult", {}).get("idlist", [])


def fetch_articles(config: dict[str, Any], pmids: list[str]) -> list[Article]:
    if not pmids:
        return []
    ncbi = config["ncbi"]
    params: dict[str, Any] = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
        "tool": ncbi.get("tool", "pubmed_daily"),
        "email": ncbi.get("email", ""),
        "api_key": ncbi.get("api_key", ""),
    }
    raw = ncbi_get(NCBI_EFETCH, params, float(ncbi.get("request_delay_seconds", 0.34)))
    xml_root = ET.fromstring(raw)
    articles: list[Article] = []
    for node in xml_root.findall(".//PubmedArticle"):
        pmid = clean_text(node.findtext(".//MedlineCitation/PMID", ""))
        if not pmid:
            continue
        article_node = node.find(".//Article")
        title = stringify_xml(article_node.find("ArticleTitle") if article_node is not None else None)
        journal = clean_text(node.findtext(".//Journal/Title", "")) or "未知期刊"
        year = extract_year(node)
        abstract = extract_abstract(article_node)
        doi = extract_doi(node)
        keywords = [clean_text(k.text or "") for k in node.findall(".//Keyword") if clean_text(k.text or "")]
        publication_types = [
            clean_text(p.text or "") for p in node.findall(".//PublicationType") if clean_text(p.text or "")
        ]
        articles.append(
            Article(
                pmid=pmid,
                title=clean_text(title) or "无题名",
                journal=journal,
                year=year,
                abstract=abstract,
                doi=doi,
                keywords=keywords,
                publication_types=publication_types,
            )
        )
    return articles


def stringify_xml(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return "".join(node.itertext())


def extract_abstract(article_node: ET.Element | None) -> str:
    if article_node is None:
        return ""
    parts: list[str] = []
    for abstract_text in article_node.findall(".//AbstractText"):
        label = abstract_text.attrib.get("Label")
        content = clean_text("".join(abstract_text.itertext()))
        if not content:
            continue
        parts.append(f"{label}: {content}" if label else content)
    return clean_text(" ".join(parts))


def extract_year(node: ET.Element) -> str:
    candidates = [
        ".//Article/Journal/JournalIssue/PubDate/Year",
        ".//PubMedPubDate[@PubStatus='pubmed']/Year",
        ".//PubMedPubDate[@PubStatus='entrez']/Year",
        ".//DateCompleted/Year",
    ]
    for path in candidates:
        year = clean_text(node.findtext(path, ""))
        if year:
            return year
    medline_date = clean_text(node.findtext(".//Article/Journal/JournalIssue/PubDate/MedlineDate", ""))
    match = re.search(r"(19|20)\d{2}", medline_date)
    return match.group(0) if match else "未知年份"


def extract_doi(node: ET.Element) -> str:
    for article_id in node.findall(".//ArticleId"):
        if article_id.attrib.get("IdType", "").lower() == "doi":
            return clean_text(article_id.text or "")
    return ""


def load_seen() -> dict[str, Any]:
    if not SEEN_PATH.exists():
        return {"recommended_pmids": {}, "history": []}
    with SEEN_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("recommended_pmids", {})
    data.setdefault("history", [])
    return data


def save_seen(seen: dict[str, Any]) -> None:
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SEEN_PATH.open("w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)
        f.write("\n")


def term_in_text(term: str, text: str) -> bool:
    term_lower = term.lower()
    escaped = re.escape(term_lower.rstrip("*"))
    escaped = escaped.replace(r"\ ", r"\s+")
    if term_lower.endswith("*"):
        return re.search(rf"(?<![a-z0-9]){escaped}", text) is not None
    return re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text) is not None


def score_and_classify(
    articles: list[Article], config: dict[str, Any], seen: dict[str, Any], report_date: str
) -> None:
    priority = config["search"].get("priority_keywords", {})
    categories = config["search"].get("categories", {})
    seen_pmids = seen.get("recommended_pmids", {})
    for article in articles:
        text = article.combined_text
        score = 0
        for term, weight in priority.items():
            if term_in_text(str(term), text):
                score += int(weight)
        if article.abstract:
            score += 1
        if article.doi:
            score += 1
        if any(pt.lower() in {"journal article", "clinical trial"} for pt in article.publication_types):
            score += 1
        if any("review" in pt.lower() for pt in article.publication_types):
            score -= 1
        seen_entry = seen_pmids.get(article.pmid)
        seen_date = seen_entry.get("date") if isinstance(seen_entry, dict) else seen_entry
        article.is_seen = bool(seen_entry and seen_date != report_date)
        if article.is_seen:
            score -= 6
        article.score = max(score, 0)
        article.categories = classify_article(text, categories)


def classify_article(text: str, categories: dict[str, list[str]]) -> list[str]:
    matched: list[str] = []
    for category, terms in categories.items():
        if any(term_in_text(term, text) for term in terms):
            matched.append(category)
    return matched or ["其他相关"]


def choose_top_articles(articles: list[Article], config: dict[str, Any]) -> list[Article]:
    top_n = int(config["search"].get("top_n", 5))
    threshold = int(config["search"].get("high_quality_score", 7))
    high_quality = [a for a in articles if a.score >= threshold and not a.is_seen]
    high_quality.sort(key=lambda a: (a.score, a.year), reverse=True)
    return high_quality[:top_n]


def infer_subject(article: Article) -> str:
    text = article.combined_text
    checks = [
        ("HK-2 近端肾小管上皮细胞", ["hk-2"]),
        ("人群/队列或环境暴露人群", ["cohort", "population", "human", "nhanes", "epidemiology"]),
        ("小鼠或大鼠肾损伤模型", ["mouse", "mice", "rat", "rats", "animal model"]),
        ("肾脏类器官或 iPSC 模型", ["organoid", "ipsc", "stem cell"]),
        ("肾小管/肾脏细胞与组织", ["proximal tubule", "tubular", "renal cell", "kidney cell"]),
        ("患者样本或临床数据", ["patient", "clinical", "biopsy"]),
    ]
    return first_matching_label(text, checks, "题名和摘要未明确，建议阅读全文确认")


def infer_methods(article: Article, config: dict[str, Any]) -> str:
    text = article.combined_text
    methods = config["search"].get("methods", {})
    matched = [label for label, terms in methods.items() if any(term_in_text(term, text) for term in terms)]
    if matched:
        return "；".join(matched[:3])
    return "基于题名/摘要的常规实验或文献分析，需阅读全文确认"


def first_matching_label(text: str, checks: list[tuple[str, list[str]]], fallback: str) -> str:
    for label, terms in checks:
        if any(term_in_text(term, text) for term in terms):
            return label
    return fallback


def summarize_finding(article: Article) -> str:
    text = article.combined_text
    focus_terms = []
    for label, terms in [
        ("环境污染物暴露", ["environmental exposure", "pollutant", "toxicant", "pfas", "cadmium", "arsenic", "lead"]),
        ("肾毒性/肾损伤", ["nephrotoxicity", "kidney injury", "renal toxicity", "aki", "ckd"]),
        ("肾纤维化", ["kidney fibrosis", "renal fibrosis", "fibrosis", "tgf"]),
        ("m6A-METTL3/PRODH 调控", ["m6a", "mettl3", "prodh"]),
        ("单细胞或空间组学", ["single-cell", "single cell", "scrna", "snrna", "spatial transcriptomics"]),
        ("类器官模型", ["organoid", "ipsc"]),
    ]:
        if any(term_in_text(term, text) for term in terms):
            focus_terms.append(label)
    if article.abstract:
        clue = conclusion_sentence(article.abstract)
        if clue:
            return f"摘要提示研究重点涉及{'、'.join(focus_terms[:3]) or '本方向相关问题'}；结论线索为：{clue}"
    return f"题名和关键词提示该文关注{'、'.join(focus_terms[:3]) or '本方向相关问题'}，需要阅读全文确认具体结果。"


def conclusion_sentence(abstract: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", clean_text(abstract))
    preferred = [
        s
        for s in sentences
        if re.search(r"\b(conclusion|conclusions|suggest|indicate|demonstrate|show|revealed|found)\b", s, re.I)
    ]
    source = preferred[-1:] or sentences[-1:]
    if not source:
        return ""
    sentence = source[0]
    if len(sentence) > 260:
        sentence = sentence[:257].rstrip() + "..."
    return sentence


def why_read(article: Article) -> str:
    reasons = []
    if "环境流行病学" in article.categories and "机制实验" in article.categories:
        reasons.append("同时连接环境暴露与机制线索")
    if "肾毒性" in article.categories:
        reasons.append("与肾毒性/肾损伤主线直接相关")
    if "单细胞组学" in article.categories:
        reasons.append("可帮助寻找细胞类型特异性机制")
    if "类器官" in article.categories:
        reasons.append("对建立更接近人体的模型有参考价值")
    if "m6A-METTL3-PRODH" in article.categories:
        reasons.append("贴近 m6A-METTL3-PRODH 调控假说")
    if article.score >= 12:
        reasons.append("关键词匹配度较高")
    return "；".join(reasons[:3]) or "与检索主题有交集，可作为背景或线索文献扫读"


def markdown_link(text: str, url: str) -> str:
    safe_text = text.replace("[", "\\[").replace("]", "\\]")
    return f"[{safe_text}]({url})"


def render_article(article: Article, index: int, config: dict[str, Any]) -> str:
    doi_text = markdown_link(article.doi, f"https://doi.org/{article.doi}") if article.doi else "未提供"
    categories = "、".join(article.categories)
    return "\n".join(
        [
            f"### {index}. {article.title}",
            "",
            f"- 题目：{article.title}",
            f"- 期刊：{article.journal}",
            f"- 年份：{article.year}",
            f"- PMID：{markdown_link(article.pmid, article.pubmed_url)}",
            f"- DOI：{doi_text}",
            f"- 分类：{categories}",
            f"- 规则评分：{article.score}",
            f"- 研究对象：{infer_subject(article)}",
            f"- 核心方法：{infer_methods(article, config)}",
            f"- 主要发现：{summarize_finding(article)}",
            f"- 为什么值得读：{why_read(article)}",
        ]
    )


def render_report(
    report_date: str,
    articles: list[Article],
    top_articles: list[Article],
    config: dict[str, Any],
    window_days: int,
    first_count: int,
    fallback_reason: str,
    error_message: str = "",
) -> str:
    generated_at = f"{report_date} UTC"
    threshold = int(config["search"].get("high_quality_score", 7))
    window_text = "近 24 小时" if window_days == 1 else f"近 {window_days} 天"

    lines: list[str] = [
        "---",
        f"title: PubMed 文献晨报 {report_date}",
        "---",
        "",
        f"# PubMed 文献晨报｜{report_date}",
        "",
        f"- 生成日期：{generated_at}",
        f"- 检索窗口：{window_text}",
        f"- 高质量阈值：规则评分 ≥ {threshold}",
        f"- 近 24 小时原始命中数：{first_count}",
        "",
        "## 今日总体判断",
        "",
    ]

    if error_message:
        lines.extend(
            [
                "今日检索 PubMed 时遇到网络或 NCBI 返回异常，已生成空日报，方便归档不断档。",
                "",
                f"- 异常信息：`{error_message}`",
            ]
        )
    elif top_articles:
        category_counter = count_categories(top_articles)
        lead_categories = "、".join([name for name, _ in category_counter[:3]]) or "主题相关"
        lines.append(
            f"今日筛选出 {len(top_articles)} 篇优先阅读文献，主要集中在：{lead_categories}。"
        )
        if fallback_reason:
            lines.append(f"由于{fallback_reason}，本次已自动扩大检索范围。")
    else:
        lines.append("今日无高质量新文献。")
        if fallback_reason:
            lines.append(f"原因：{fallback_reason}。")
        elif articles:
            lines.append("虽然检索到一些候选文献，但规则评分未达到高质量阈值，建议暂不精读。")
        else:
            lines.append("PubMed 在当前检索窗口内没有返回与主题匹配的新文献。")

    lines.extend(["", "## 今日最值得读的 5 篇文章", ""])
    if top_articles:
        for i, article in enumerate(top_articles, 1):
            lines.append(render_article(article, i, config))
            lines.append("")
    else:
        lines.append("今日无高质量新文献，因此不强行推荐 5 篇。")
        lines.append("")

    lines.extend(["## 分类归档", ""])
    categories = config["search"].get("categories", {})
    for category in categories:
        lines.append(f"### {category}")
        matched = [a for a in top_articles if category in a.categories]
        if matched:
            for article in matched:
                lines.append(f"- {markdown_link(article.title, article.pubmed_url)}（PMID: {article.pmid}）")
        else:
            lines.append("- 今日暂无高质量新文献。")
        lines.append("")

    lines.extend(["## 今日阅读优先级", ""])
    if top_articles:
        for i, article in enumerate(top_articles, 1):
            lines.append(f"{i}. {article.title}（优先理由：{why_read(article)}）")
    else:
        lines.append("1. 今日不建议安排精读；可以把时间用于复习既往核心文献或优化检索词。")
    lines.append("")

    lines.extend(render_mermaid(report_date, top_articles))
    return "\n".join(lines).rstrip() + "\n"


def count_categories(articles: list[Article]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for article in articles:
        for category in article.categories:
            counts[category] = counts.get(category, 0) + 1
    return sorted(counts.items(), key=lambda item: item[1], reverse=True)


def mermaid_safe(text: str, max_len: int = 42) -> str:
    cleaned = re.sub(r"[:()\[\]{}\"']", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_len] + ("..." if len(cleaned) > max_len else "")


def render_mermaid(report_date: str, top_articles: list[Article]) -> list[str]:
    lines = ["## Mermaid 思维导图", "", "```mermaid", "mindmap", f"  root((PubMed晨报 {report_date}))"]
    if not top_articles:
        lines.extend(
            [
                "    今日总体判断",
                "      今日无高质量新文献",
                "    后续动作",
                "      继续自动监测",
                "      必要时调整关键词",
            ]
        )
    else:
        lines.append("    今日优先阅读")
        for article in top_articles:
            lines.append(f"      {mermaid_safe(article.title)}")
            for category in article.categories[:3]:
                lines.append(f"        {mermaid_safe(category)}")
        lines.append("    研究主线")
        lines.append("      环境低剂量污染物")
        lines.append("      肾毒性与肾纤维化")
        lines.append("      单细胞组学与类器官")
        lines.append("      m6A METTL3 PRODH")
    lines.append("```")
    return lines


def update_index() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    reports = sorted(REPORTS_DIR.glob("*.md"), reverse=True)
    lines = [
        "---",
        "title: PubMed 文献晨报",
        "---",
        "",
        "# PubMed 文献晨报",
        "",
        "这里会按日期倒序展示每天自动生成的 PubMed 文献晨报。",
        "",
        "## 日报归档",
        "",
    ]
    if reports:
        for report in reports:
            date_text = report.stem
            lines.append(f"- [{date_text}](reports/{date_text}.html)")
    else:
        lines.append("暂无日报。GitHub Actions 首次运行后会自动生成。")
    INDEX_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def update_seen(seen: dict[str, Any], report_date: str, top_articles: list[Article]) -> None:
    if not top_articles:
        return
    recommended = seen.setdefault("recommended_pmids", {})
    for article in top_articles:
        recommended[article.pmid] = {
            "date": report_date,
            "title": article.title,
            "score": article.score,
            "pubmed_url": article.pubmed_url,
            "doi": article.doi,
        }
    history = [item for item in seen.setdefault("history", []) if item.get("date") != report_date]
    history.append(
        {
            "date": report_date,
            "pmids": [article.pmid for article in top_articles],
        }
    )
    seen["history"] = history[-60:]


def generate(report_date: str) -> None:
    config = load_config(CONFIG_PATH)
    seen = load_seen()
    first_days = int(config["search"].get("first_window_days", 1))
    fallback_days = int(config["search"].get("fallback_window_days", 7))

    first_count = 0
    window_days = first_days
    fallback_reason = ""
    error_message = ""
    articles: list[Article] = []
    top_articles: list[Article] = []

    try:
        pmids = search_pubmed(config, first_days)
        first_count = len(pmids)
        articles = fetch_articles(config, pmids)
        score_and_classify(articles, config, seen, report_date)
        top_articles = choose_top_articles(articles, config)

        if not top_articles:
            fallback_reason = (
                "近 24 小时内没有达到高质量阈值的未推荐文献"
                if first_count
                else "近 24 小时内没有检索到候选文献"
            )
            window_days = fallback_days
            fallback_pmids = search_pubmed(config, fallback_days)
            articles = fetch_articles(config, fallback_pmids)
            score_and_classify(articles, config, seen, report_date)
            top_articles = choose_top_articles(articles, config)
    except (urllib.error.URLError, TimeoutError, ET.ParseError, json.JSONDecodeError) as exc:
        error_message = str(exc)
        articles = []
        top_articles = []

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report = render_report(
        report_date=report_date,
        articles=articles,
        top_articles=top_articles,
        config=config,
        window_days=window_days,
        first_count=first_count,
        fallback_reason=fallback_reason,
        error_message=error_message,
    )
    (REPORTS_DIR / f"{report_date}.md").write_text(report, encoding="utf-8")
    update_seen(seen, report_date, top_articles)
    save_seen(seen)
    update_index()

    print(f"已生成日报：docs/reports/{report_date}.md")
    print(f"推荐文献数：{len(top_articles)}")
    if fallback_reason:
        print(f"已扩大检索范围：{fallback_reason}")
    if error_message:
        print(f"检索异常：{error_message}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a PubMed daily report.")
    parser.add_argument(
        "--date",
        default=datetime.now(timezone.utc).date().isoformat(),
        help="Report date, default is today's UTC date, for example 2026-07-04.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    generate(args.date)
