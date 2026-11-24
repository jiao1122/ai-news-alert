import hashlib
import json
import os
import re
from urllib.parse import urlparse

import feedparser
import requests

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
BARK_URL = os.environ.get("BARK_URL", "").strip()
IS_MANUAL_RUN = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
SEEN_FILE = "seen.json"

RSS_FEEDS = [
    ("OpenAI官方", "https://openai.com/news/rss.xml"),
    ("Google AI", "https://blog.google/technology/ai/rss/"),
    ("DeepMind", "https://deepmind.google/blog/rss.xml"),
    ("HuggingFace", "https://huggingface.co/blog/feed.xml"),
    ("Simon Willison", "https://simonwillison.net/atom/everything/"),
    ("HackerNews AI", "https://hnrss.org/newest?q=LLM+OR+OpenAI+OR+Gemini+OR+Claude+OR+DeepSeek+OR+Qwen+OR+free+API&points=20"),
    ("Reddit LocalLLaMA", "https://www.reddit.com/r/LocalLLaMA/hot/.rss"),
    ("V2EX技术", "https://www.v2ex.com/feed/tab/tech.xml"),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AIAlertBot/1.0; +https://github.com)"
}


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
        "isArchive": 1,
    }
    if url:
        payload["url"] = url
    resp = requests.post(f"{base}/push", json=payload, timeout=15)
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
    # 只保留最近 1500 条记录，避免文件无限增大
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen_list[-1500:], f, ensure_ascii=False, indent=2)


def fetch_new_items(seen_set):
    items = []
    for source_name, feed_url in RSS_FEEDS:
        try:
            resp = requests.get(feed_url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
            for entry in parsed.entries[:12]:
                title = (entry.get("title") or "").strip()
                link = (entry.get("link") or "").strip()
                summary = re.sub(r"<[^>]+>", " ", entry.get("summary") or "")
                summary = re.sub(r"\s+", " ", summary).strip()[:280]
                uid = item_id(link, title)
                if not title or uid in seen_set:
                    continue
                seen_set.add(uid)
                items.append({
                    "id": uid,
                    "source": source_name,
                    "title": title,
                    "summary": summary,
                    "url": link,
                })
        except Exception as e:
            print(f"[跳过] 抓取 {source_name} 失败: {e}")
    return items


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


def analyze_with_gemini(items, max_results=5):
    prompt = f"""你是用户的私人 AI 情报筛选官。请阅读下面最新抓取的资讯列表，严格只挑出真正属于以下 3 类之一的高价值内容（最多挑 {max_results} 条）：
1. "新模型"：知名大厂或重要开源团队发布的新 AI 模型、重大版本升级（如 OpenAI、Google Gemini、Anthropic Claude、DeepSeek、Qwen、Meta Llama、xAI Grok、字节/智谱/Kimi 等）。
2. "优惠"：AI 会员、官方 API 降价、打折、限时促销活动。
3. "薅羊毛"：真实可用的免费 API 额度、免费白嫖大额算力/会员福利、限时免费体验。

严格过滤掉：普通技术讨论、观点文章、小众工具广告、融资八卦、非 AI 内容、已过期消息。如果列表中没有符合上述 3 类的高价值消息，必须返回空数组 []。

请只输出合法的 JSON 数组，每个元素格式如下：
[
  {{
    "category": "新模型" 或 "优惠" 或 "薅羊毛",
    "title": "简短中文标题（22字以内）",
    "summary": "2-3句中文精华，说清发布内容/降价幅度/怎么白嫖（100字以内）",
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
    for model in discover_flash_models()[:6]:
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

    # 首次运行最多精选 3 条，防止旧闻刷屏；平时每次最多处理最新 30 条
    batch = new_items[:25] if is_first_run else new_items[:30]
    max_push = 3 if is_first_run else 5

    alerts = []
    if batch:
        alerts = analyze_with_gemini(batch, max_results=max_push)

    emoji_map = {"新模型": "🚀", "优惠": "💰", "薅羊毛": "🎁"}
    for a in alerts:
        cat = a.get("category", "AI动态")
        icon = emoji_map.get(cat, "📢")
        title = f"{icon} [{cat}] {a.get('title', 'AI新动态')}"
        body = a.get("summary", "").strip() or "点击查看详情"
        url = a.get("url", "").strip()
        send_bark(title, body, url)

    if not alerts and (is_first_run or IS_MANUAL_RUN):
        send_bark(
            "✅ AI情报监控已上线",
            f"本次自动巡逻了 {len(RSS_FEEDS)} 个信息源（抓取 {len(new_items)} 条），暂未发现新的重磅模型发布或薅羊毛信息。后续将每30分钟自动监控！",
        )

    # 保存所有已见过的 ID，防止下次重复推送
    for it in new_items:
        seen_list.append(it["id"])
    save_seen(seen_list)


if __name__ == "__main__":
    main()
