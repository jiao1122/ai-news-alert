import hashlib
import json
import os
import re
from urllib.parse import quote, urlparse

import feedparser
import requests

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
BARK_URL = os.environ.get("BARK_URL", "").strip()
SEEN_FILE = "seen.json"


def google_news_rss(query: str) -> str:
    return (
        f"https://news.google.com/rss/search?q={quote(query)}"
        f"&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    )


RSS_FEEDS = [
    # —— 大厂官方与海外一手 ——
    ("OpenAI官方", "https://openai.com/news/rss.xml"),
    ("Google AI", "https://blog.google/technology/ai/rss/"),
    ("DeepMind", "https://deepmind.google/blog/rss.xml"),
    ("HuggingFace", "https://huggingface.co/blog/feed.xml"),
    ("Simon Willison", "https://simonwillison.net/atom/everything/"),
    (
        "HackerNews AI",
        "https://hnrss.org/newest?q=LLM+OR+Agent+OR+OpenAI+OR+Gemini+OR+Claude+OR+DeepSeek+OR+Qwen+OR+free+API&points=25",
    ),
    # —— AIHOT 精选（只提取模型/Agent/优惠/羊毛，严滤股票杂项） ——
    ("AIHOT精选", "https://aihot.news/feed.xml"),
    # —— 中文 AI 头部媒体、博主与技术社区 ——
    ("量子位", "https://www.qbitai.com/feed"),
    ("36氪", "https://36kr.com/feed-article"),
    ("少数派", "https://sspai.com/feed"),
    ("宝玉的分享", "https://baoyu.io/feed.xml"),
    ("V2EX技术", "https://www.v2ex.com/feed/tab/tech.xml"),
    # —— 全网中文 24h 评测口碑 & 优惠羊毛监控 ——
    (
        "中文Agent实测",
        google_news_rss("(AI Agent OR 智能体 OR 大模型) (实测 OR 评测 OR 体验 OR 对比 OR 吐槽) when:1d"),
    ),
    (
        "中文AI羊毛优惠",
        google_news_rss("(大模型 OR AI API OR DeepSeek OR Gemini OR ChatGPT OR Claude) (降价 OR 免费 OR 优惠 OR 限时免费) when:1d"),
    ),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
}


def clean_html(raw: str, limit: int = 260) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def parse_bark(raw_value: str):
    match = re.search(r"https?://[^\s'\"]+", raw_value)
    if match:
        parsed = urlparse(match.group(0))
        base = f"{parsed.scheme}://{parsed.netloc}"
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            raise ValueError("BARK_URL 中未找到设备 Key")
        return base, parts[0]
    key = raw_value.strip().strip("/").split("/")[0]
    if not key:
        raise ValueError("BARK_URL 为空")
    return "https://api.day.app", key


def send_bark(title: str, body: str, url: str = ""):
    base, device_key = parse_bark(BARK_URL)
    payload = {
        "device_key": device_key,
        "title": title,
        "body": body,
        "group": "AI情报",
        "isArchive": "1",
    }
    if url and url.startswith("http"):
        payload["url"] = url
    resp = requests.post(
        f"{base}/push",
        json=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"Bark 返回异常: {resp.status_code} {resp.text}")
    resp.raise_for_status()
    print(f"已推送到 Bark: {title}")


def item_id(url: str, title: str) -> str:
    raw = (url or title).strip()
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return [], True
    with open(SEEN_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else [], False


def save_seen(seen_list):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen_list[-2000:], f, ensure_ascii=False, indent=2)


def fetch_new_items(seen_set):
    per_source = []
    for source_name, feed_url in RSS_FEEDS:
        bucket = []
        try:
            resp = requests.get(feed_url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
            for entry in parsed.entries[:8]:
                title = (entry.get("title") or "").strip()
                link = (entry.get("link") or "").strip()
                summary = clean_html(entry.get("summary") or "", 260)
                uid = item_id(link, title)
                if not title or uid in seen_set:
                    continue
                seen_set.add(uid)
                bucket.append({
                    "id": uid,
                    "source": source_name,
                    "title": title,
                    "summary": summary,
                    "url": link,
                })
            print(f"[抓取] {source_name}: 新增 {len(bucket)} 条")
        except Exception as e:
            print(f"[跳过] 抓取 {source_name} 失败: {e}")
        if bucket:
            per_source.append(bucket)

    interleaved = []
    idx = 0
    while True:
        added = False
        for bucket in per_source:
            if idx < len(bucket):
                interleaved.append(bucket[idx])
                added = True
        if not added:
            break
        idx += 1
    return interleaved


def discover_flash_models():
    excluded = (
        "image", "tts", "audio", "embedding", "veo", "imagen",
        "robotics", "computer-use", "live", "vision", "aqa",
    )
    candidates = []
    try:
        r = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": GEMINI_API_KEY},
            timeout=15,
        )
        r.raise_for_status()
        models = r.json().get("models", [])
        scored = []
        for m in models:
            name = m.get("name", "").replace("models/", "")
            methods = m.get("supportedGenerationMethods", [])
            low = name.lower()
            if "generateContent" not in methods or "flash" not in low:
                continue
            if any(bad in low for bad in excluded):
                continue
            nums = [int(x) for x in re.findall(r"\d+", low)]
            ver = tuple(nums[:2]) if nums else (0, 0)
            is_stable = 0 if ("exp" in low or "preview" in low) else 1
            is_lite = 0 if "lite" in low else 1
            scored.append((is_stable, ver, is_lite, name))
        scored.sort(reverse=True)
        candidates = [x[3] for x in scored]
    except Exception as e:
        print(f"自动获取模型列表失败，使用兜底列表: {e}")

    fallbacks = [
        "gemini-flash-latest",
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
        "gemini-2.5-flash",
    ]
    for fb in fallbacks:
        if fb not in candidates:
            candidates.append(fb)
    return candidates


def analyze_with_gemini(items, max_results=4):
    prompt = f"""你是用户的私人 AI 情报筛选官。请阅读下面最新抓取的中英文资讯列表，严格只挑出真正属于以下 4 类之一的高价值内容（最多挑 {max_results} 条，宁缺毋滥）：

1. "新模型"：知名大厂或重要开源团队发布的新 AI 模型、AI Agent 重大版本升级（如 OpenAI、Google Gemini、Anthropic Claude、DeepSeek、Qwen、Meta Llama、xAI Grok、字节/智谱/Kimi 等）。
2. "优惠"：AI 会员、官方 API 降价、打折、限时促销活动。
3. "薅羊毛"：真实可用的免费 API 额度、大额免费算力/会员白嫖福利、限时免费体验。
4. "实测口碑"：针对热门 AI 模型或 AI Agent 的真实上手评测、横向优缺点对比、开发者深度吐槽或避坑经验（必须有实质干货，拒绝空洞吹水）。

严格过滤掉：股票涨跌、公司财报、纯粹的融资八卦、普通行业新闻、硬件芯片股市消息、软文推广、标题党、与模型/Agent/优惠无关的内容、重复话题（同一事件只保留最精华的一条）。如果列表中没有符合上述高标准的消息，必须返回空数组 []。

语言要求：用普通人也能秒懂的“大白话中文”总结，不要堆砌看不懂的专业黑话，直接讲清它是什么、好不好用、怎么白嫖或省钱。

请只输出合法的 JSON 数组，每个元素格式如下：
[
  {{
    "category": "新模型" 或 "优惠" 或 "薅羊毛" 或 "实测口碑",
    "title": "简短中文标题（22字以内）",
    "summary": "2-3句大白话中文精华（110字以内）",
    "url": "直接保留原链接"
  }}
]

待筛选资讯列表：
{json.dumps(items, ensure_ascii=False)}
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }

    last_err = None
    for model in discover_flash_models()[:8]:
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            resp = requests.post(
                endpoint,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": GEMINI_API_KEY,
                },
                json=payload,
                timeout=60,
            )
            if resp.status_code != 200:
                last_err = f"{model} -> HTTP {resp.status_code}: {resp.text[:200]}"
                print(f"[切换模型] {last_err}")
                continue
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
            result = json.loads(text)
            if isinstance(result, list):
                print(f"成功使用模型: {model}，筛选出 {len(result)} 条")
                return result[:max_results]
        except Exception as e:
            last_err = f"{model} -> {e}"
            print(f"[切换模型] {last_err}")

    raise RuntimeError(f"所有 Gemini 候选模型调用均失败，最后错误: {last_err}")


def main():
    if not GEMINI_API_KEY or not BARK_URL:
        raise ValueError("请检查 GitHub Secrets 中是否已配置 GEMINI_API_KEY 和 BARK_URL")

    seen_list, is_first_run = load_seen()
    seen_set = set(seen_list)

    new_items = fetch_new_items(seen_set)
    print(f"本次新抓取 {len(new_items)} 条资讯（首次运行: {is_first_run}）")

    batch = new_items[:50]
    max_push = 3 if is_first_run else 4
    alerts = analyze_with_gemini(batch, max_results=max_push) if batch else []

    emoji_map = {
        "新模型": "🚀",
        "优惠": "💰",
        "薅羊毛": "🎁",
        "实测口碑": "💬",
    }
    for a in alerts:
        cat = a.get("category", "AI动态")
        icon = emoji_map.get(cat, "📢")
        title = f"{icon} [{cat}] {a.get('title', 'AI新动态')}"
        body = a.get("summary", "").strip() or "点击查看详情"
        url = a.get("url", "").strip()
        send_bark(title, body, url)

    if not alerts and is_first_run:
        send_bark(
            "✅ AI情报监控已上线",
            f"本次巡逻了 {len(RSS_FEEDS)} 个信息源，后续将每30分钟自动监控。",
        )

    for it in new_items:
        seen_list.append(it["id"])
    save_seen(seen_list)


if __name__ == "__main__":
    main()
