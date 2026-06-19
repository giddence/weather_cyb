#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中央气象台气象信息自动抓取 + 企微Bot推送
两种模式：
  --mode page  (默认) 气象预警/公报页面（文字+图片）
  --mode city          城市天气预报（文字，无图片）
"""

import urllib.request
import urllib.error
import re
import json
import base64
import hashlib
import os
import sys
import io
import argparse
from datetime import datetime, timedelta

# 修复 Windows 终端 GBK 编码
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ============ 配置 ============
WEBHOOK_URL = os.environ.get("WECOM_WEBHOOK_URL", "")

# 页面配置：URL → 名称
PAGES = {
    "374": {"url": "https://weather.cma.cn/web/channel-374.html", "name": "暴雨预警"},
    "423": {"url": "https://weather.cma.cn/web/channel-423.html", "name": "交通气象预报"},
    "339": {"url": "https://weather.cma.cn/web/channel-339.html", "name": "24h降水量预报"},
    "340": {"url": "https://weather.cma.cn/web/channel-340.html", "name": "48h降水量预报"},
    "341": {"url": "https://weather.cma.cn/web/channel-341.html", "name": "72h降水量预报"},
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://weather.cma.cn/",
}


# ============ 页面抓取 & 解析 ============

def fetch_html(url: str) -> str:
    """获取页面HTML"""
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _extract_nested_div(html: str, class_name: str) -> str | None:
    """提取指定 class 的 div 内容，正确处理嵌套 div"""
    pattern = rf'<div[^>]*class="[^"]*{class_name}[^"]*"[^>]*>'
    m = re.search(pattern, html)
    if not m:
        return None

    start = m.start()
    depth = 1
    pos = m.end()

    while depth > 0 and pos < len(html):
        next_open = html.find("<div", pos)
        next_close = html.find("</div>", pos)

        if next_close < 0:
            break
        if next_open >= 0 and next_open < next_close:
            depth += 1
            pos = next_open + 4
        else:
            depth -= 1
            if depth == 0:
                return html[m.start():next_close + 6]
            pos = next_close + 6

    return None


def strip_tags(html: str) -> str:
    """去除HTML标签，保留文本"""
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def parse_text_content(html: str) -> dict:
    """
    解析气象页面的文字内容
    返回: {title, source, publish_time, warning_level, content, content_above, defense_guide[]}
    - content: 全部文字（向后兼容）
    - content_above: 仅图片上方的文字（用于交通气象等长页面）
    """
    result = {
        "title": "",
        "source": "",
        "publish_time": "",
        "warning_level": "",
        "content": "",
        "content_above": "",
        "defense_guide": [],
    }

    # 标题: <div class="pageheader"><div class="ptitle">暴雨预警</div></div>
    title_match = re.search(r'<div[^>]*class="ptitle"[^>]*>(.*?)</div>', html, re.DOTALL)
    if title_match:
        result["title"] = strip_tags(title_match.group(1))

    # 来源和发布时间
    source_match = re.search(r'来源[：:]\s*(.*?)(?:</span>|$)', html)
    if source_match:
        result["source"] = strip_tags(source_match.group(1))
    time_match = re.search(r'发布时间[：:]\s*(.*?)(?:</span>|$)', html)
    if time_match:
        result["publish_time"] = strip_tags(time_match.group(1))

    # 从 title 或内容中提取预警等级
    title_text = result["title"]
    if "红" in title_text:
        result["warning_level"] = "红色预警"
    elif "橙" in title_text:
        result["warning_level"] = "橙色预警"
    elif "黄" in title_text:
        result["warning_level"] = "黄色预警"
    elif "蓝" in title_text:
        result["warning_level"] = "蓝色预警"

    # 正文内容：在 <div class="xml"> 中提取所有内容
    # 使用嵌套感知的方式提取，因为 XML div 内可能嵌套其他 div
    xml_html = _extract_nested_div(html, "xml")
    if xml_html:
        # 去掉外层 <div class="xml"> 和 </div>
        xml_inner = re.sub(r'^<div[^>]*>', '', xml_html.strip())
        xml_inner = re.sub(r'</div>$', '', xml_inner.strip())

        content_parts = []
        content_parts_above = []  # 仅图片上方文字
        in_defense = False
        after_image = False  # 是否已过图片位置

        # 先提取 xml-title (如"中国气象局与交通运输部联合发布...")
        xml_title_match = re.search(
            r'<div[^>]*class="[^"]*xml-title[^"]*"[^>]*>(.*?)</div>',
            xml_inner, re.DOTALL
        )
        if xml_title_match:
            title_text = strip_tags(xml_title_match.group(1))
            if title_text:
                content_parts.append(title_text)
                content_parts_above.append(title_text)

        # 按顺序遍历 XML 内的所有元素（p, div, img）
        elements = re.findall(
            r'(<(?:p|div|img)[^>]*>.*?</(?:p|div)>|<img[^>]*/?>)',
            xml_inner, re.DOTALL
        )

        for elem in elements:
            # 检测 img 标签 — 从这里开始分离 "图片下方" 内容
            if re.search(r'<img[^>]+>', elem):
                after_image = True
                continue

            # xml-sub-title: 小节标题
            if 'xml-sub-title' in elem:
                section_title = strip_tags(elem)
                if section_title:
                    content_parts.append(f"\n**{section_title}**")
                continue

            # xml-title 已处理过，跳过
            if 'xml-title' in elem:
                continue

            # 普通 <p> 段落
            text = strip_tags(elem)
            if not text:
                continue

            # 防御指南单独处理
            if "防御指南" in text:
                in_defense = True
                guide_text = text.replace("防御指南：", "").replace("防御指南:", "").strip()
                if guide_text:
                    items = re.split(r'[；;]\s*(?=\d+[、.])', guide_text)
                    for item in items:
                        item = re.sub(r'^\d+[、.]\s*', '', item).strip()
                        if item:
                            result["defense_guide"].append(item)
                continue

            if in_defense:
                items = re.split(r'[；;]\s*(?=\d+[、.])', text)
                for item in items:
                    item = re.sub(r'^\d+[、.]\s*', '', item).strip()
                    if item:
                        result["defense_guide"].append(item)
            else:
                content_parts.append(text)
                if not after_image:
                    content_parts_above.append(text)

        result["content"] = "\n".join(content_parts)
        result["content_above"] = "\n".join(content_parts_above) if content_parts_above else ""

    # 如果 XML 中没有提取到防御指南，尝试在整个页面中查找
    if not result["defense_guide"]:
        guide_section = re.search(
            r'防御指南[：:](.*?)(?:</div>|</div>|$)',
            html, re.DOTALL
        )
        if guide_section:
            guide_html = guide_section.group(1)
            guide_text = strip_tags(guide_html)
            items = re.split(r'[；;]\s*(?=\d+[、.])', guide_text)
            for item in items:
                item = re.sub(r'^\d+[、.]\s*', '', item).strip()
                if item:
                    result["defense_guide"].append(item)

    return result


def extract_image_url(html: str) -> str | None:
    """
    从HTML中提取主要天气图片URL
    支持相对路径和绝对路径两种格式：
    - channel-374: src="/file/2026/06/18/xxx.jpg" (相对路径)
    - channel-339: src="https://weather.cma.cn/file/2026/06/18/xxx.JPG" (绝对路径)
    过滤掉 Logo、认证标识等无关图片（/assets/ 路径）
    """
    # 匹配 src 中的图片URL（支持相对路径和绝对路径）
    src_matches = re.findall(
        r'src=[\"\']((?:https://weather\.cma\.cn)?/file/[^\"\'\s]+\.(?:jpg|JPG|jpeg|JPEG)[^\"\'\s]*)[\"\']',
        html
    )

    for img_path in src_matches:
        if img_path.startswith("http"):
            return img_path
        else:
            return f"https://weather.cma.cn{img_path}"

    return None


def download_image(img_url: str, save_dir: str = ".") -> str | None:
    """下载图片，返回本地路径"""
    try:
        req = urllib.request.Request(img_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()

        filename = os.path.basename(img_url.split("?")[0])
        save_path = os.path.join(save_dir, filename)
        with open(save_path, "wb") as f:
            f.write(data)

        print(f"✅ 图片下载成功: {filename} ({len(data)/1024:.1f} KB)")
        return save_path
    except Exception as e:
        print(f"❌ 图片下载失败: {e}")
        return None


# ============ 企微Bot发送 ============

def send_markdown(webhook_url: str, content: str) -> bool:
    """发送 Markdown 消息到企微Bot"""
    # 企微限制 4096 UTF-8 字节，需要按字节截断
    max_bytes = 4000
    content_bytes = content.encode("utf-8")
    if len(content_bytes) > max_bytes:
        # 按字节截断，确保不破坏多字节字符
        truncated = content_bytes[:max_bytes - 30]  # 留30字节给截断提示
        content = truncated.decode("utf-8", errors="ignore") + "\n> ...已截断"
        print(f"⚠️  消息过长({len(content_bytes)}字节)，已截断至 {len(content.encode('utf-8'))} 字节")

    try:
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": content}
        }
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        if result.get("errcode") == 0:
            print("✅ Markdown 发送成功")
            return True
        else:
            print(f"❌ Markdown 发送失败: {result}")
            return False
    except Exception as e:
        print(f"❌ Markdown 发送异常: {e}")
        return False


def send_image(webhook_url: str, img_path: str) -> bool:
    """发送图片到企微Bot (base64方式)"""
    try:
        with open(img_path, "rb") as f:
            img_data = f.read()
            img_base64 = base64.b64encode(img_data).decode("utf-8")
            img_md5 = hashlib.md5(img_data).hexdigest()

        payload = {
            "msgtype": "image",
            "image": {
                "base64": img_base64,
                "md5": img_md5
            }
        }
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        if result.get("errcode") == 0:
            print("✅ 图片发送成功")
            return True
        else:
            print(f"❌ 图片发送失败: {result}")
            return False
    except Exception as e:
        print(f"❌ 图片发送异常: {e}")
        return False


# ============ 消息构建 ============

def build_markdown_message(text_info: dict, page_url: str = "") -> str:
    """
    构建 Markdown 消息
    企微 Markdown 消息有 4096 字符硬限制
    """
    MAX_LEN = 3900  # 留安全余量

    lines = []

    # 标题
    title = text_info.get("title", "气象信息")
    level = text_info.get("warning_level", "")
    if level:
        lines.append(f"## {title} <font color=\"warning\">{level}</font>")
    else:
        lines.append(f"## {title}")

    # 来源和发布时间
    source = text_info.get("source", "")
    pub_time = text_info.get("publish_time", "")
    if source or pub_time:
        lines.append(f"> 来源：{source}  发布时间：{pub_time}")

    # 正文（优先使用 content_above，仅图片上方摘要）
    content = text_info.get("content_above") or text_info.get("content", "")
    if content:
        lines.append("")
        full_text = "\n".join(lines) + "\n" + content

        if len(full_text) > MAX_LEN:
            # 计算可用空间（标题行 + 来源 + 空行 + 底部提示）
            header_len = len("\n".join(lines)) + 2
            footer = f"> 内容较长已截断，[查看完整信息]({page_url})\n\n📎 预报图见下方"
            available = MAX_LEN - header_len - len(footer) - 50  # 50字符余量

            if available > 200:
                # 有足够空间：截取前 available 个字符
                trunc_content = content[:available]
                last_break = max(
                    trunc_content.rfind("\n\n"),
                    trunc_content.rfind("\n"),
                )
                if last_break > available * 0.5:
                    trunc_content = trunc_content[:last_break].rstrip()
                lines.append(trunc_content)
            else:
                # 空间不够：只放第一段摘要
                first_para = content.split("\n")[0]
                if len(first_para) > available - 30:
                    first_para = first_para[:available - 33] + "..."
                lines.append(first_para)

            lines.append("")
            lines.append(f"> 内容较长已截断，[查看完整信息]({page_url})")
        else:
            lines.append(content)

    # 防御指南（仅在空间足够时添加）
    defense = text_info.get("defense_guide", [])
    if defense:
        guide_header = "\n**防御指南：**"
        temp = "\n".join(lines) + guide_header
        if len(temp) < MAX_LEN - 100:
            lines.append("")
            lines.append("**防御指南：**")
            for i, item in enumerate(defense, 1):
                guide_line = f"{i}. {item}"
                if len("\n".join(lines) + "\n" + guide_line) < MAX_LEN - 50:
                    lines.append(guide_line)
                else:
                    lines.append(f"> 完整防御指南见 [原文]({page_url})")
                    break

    # 底部提示
    lines.append("")
    lines.append("📎 预报图见下方")

    # 最终安全检查
    result = "\n".join(lines)
    if len(result) > 4096:
        result = result[:4080] + "\n> ...已截断"

    return result


def build_plain_text(text_info: dict) -> str:
    """构建纯文本消息（备用）"""
    lines = []

    title = text_info.get("title", "")
    level = text_info.get("warning_level", "")
    header = f"{title}"
    if level:
        header += f" [{level}]"
    lines.append(header)
    lines.append("=" * 30)

    source = text_info.get("source", "")
    pub_time = text_info.get("publish_time", "")
    if source or pub_time:
        lines.append(f"来源：{source}  发布时间：{pub_time}")

    content = text_info.get("content", "")
    if content:
        lines.append("")
        lines.append(content)

    defense = text_info.get("defense_guide", [])
    if defense:
        lines.append("")
        lines.append("防御指南：")
        for i, item in enumerate(defense, 1):
            lines.append(f"  {i}. {item}")

    return "\n".join(lines)


# ============ 城市天气 (API模式) ============

CITY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://weather.cma.cn/",
}

CITIES = {
    "昆明": "56778",
    "长春": "54161",
}

WEATHER_MAP = {
    "晴": "☀️", "多云": "⛅", "阴": "☁️",
    "小雨": "🌧️", "中雨": "🌧️", "大雨": "🌧️", "暴雨": "⛈️",
    "雷阵雨": "⛈️", "阵雨": "🌦️",
    "小雪": "🌨️", "中雪": "🌨️", "大雪": "❄️",
    "雾": "🌫️", "霾": "😷", "沙尘": "💨",
}


def city_get(station_id: str, endpoint: str) -> dict:
    url = f"https://weather.cma.cn/api/{endpoint}/{station_id}"
    req = urllib.request.Request(url, headers=CITY_HEADERS)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def city_today_rainfall(hourly_data: list) -> str:
    """提取今天逐小时降水（仅>0mm），返回摘要或 __NORAIN__/__NODATA__"""
    all_hours = []
    seen = set()
    for day in hourly_data:
        for h in day.get("list", []):
            ft = h.get("forecastTime", "")
            if ft and ft not in seen:
                seen.add(ft)
                all_hours.append(h)

    today = datetime.now().strftime("%Y/%m/%d")
    today_hours = [h for h in all_hours if h.get("forecastTime", "").startswith(today)]
    if not today_hours:
        return "__NODATA__"

    rain_items = []
    for h in today_hours:
        precip = h.get("precipitation", 0)
        if precip > 0:
            try:
                dt = datetime.strptime(h["forecastTime"], "%Y/%m/%d %H:%M")
                rain_items.append({
                    "start": dt, "end": dt + timedelta(hours=3),
                    "precip": precip, "text": h.get("text", ""),
                })
            except:
                continue

    if not rain_items:
        return "__NORAIN__"

    merged = []
    for r in rain_items:
        if merged and r["text"] == merged[-1]["text"]:
            merged[-1]["end"] = r["end"]
            merged[-1]["precip"] += r["precip"]
        else:
            merged.append(r.copy())

    parts = []
    total = 0
    for m in merged:
        s = m["start"].strftime("%H:%M")
        e = m["end"].strftime("%H:%M")
        parts.append(f"{s}-{e} {m['text']} **{m['precip']:.1f}mm**")
        total += m["precip"]
    return " | ".join(parts) + f"（累计 **{total:.0f}mm**）"


def build_city_markdown(city: str, station_id: str) -> str:
    try:
        now_data = city_get(station_id, "now")
        forecast_data = city_get(station_id, "weather")
        hourly_data = city_get(station_id, "hourly")
    except Exception as e:
        return f"❌ {city} 天气获取失败: {e}"

    lines = []
    n = now_data["data"]["now"]
    last_update = now_data["data"]["lastUpdate"]
    daily = forecast_data["data"]["daily"]

    weather_today = daily[0]["dayText"]
    night_today = daily[0]["nightText"]
    icon = WEATHER_MAP.get(weather_today, "")

    # 温度颜色：谁高谁橙红
    temp = n["temperature"]
    feel = n.get("feelst", temp)
    tc, fc = ("info", "warning") if feel >= temp else ("warning", "info")

    lines.append(f"## {city} {icon}{weather_today}  <font color=\"{tc}\">{temp}°C</font>")
    lines.append(
        f"> 体感 <font color=\"{fc}\">{feel}°C</font>　"
        f"{n['windDirection']} {n['windScale']}　"
        f"湿度 {n['humidity']}%　"
        f"夜间 {night_today}"
    )

    # 今日降水
    rain = city_today_rainfall(hourly_data.get("data", []))
    if rain == "__NORAIN__":
        lines.append("")
        lines.append("<font color=\"comment\">今日无明显降水</font>")
    elif rain and rain not in ("__NODATA__", "__NORAIN__"):
        color = "warning" if ("暴雨" in rain or "大雨" in rain) else ("comment" if "中雨" in rain else "info")
        lines.append("")
        lines.append(f"<font color=\"{color}\">━━━━ 今日降水 ━━━━</font>")
        lines.append(f"<font color=\"{color}\">{rain}</font>")
        lines.append(f"<font color=\"{color}\">━━━━━━━━━━━━━━━</font>")

    # 明日预报
    tomorrow = daily[1] if len(daily) > 1 else None
    if tomorrow:
        di = WEATHER_MAP.get(tomorrow["dayText"], "")
        ni = WEATHER_MAP.get(tomorrow["nightText"], "")
        lines.append("")
        lines.append("")
        lines.append("<font color=\"info\">━━━━ 明日预报 ━━━━</font>")
        lines.append(
            f">{di}{tomorrow['dayText']} / {ni}{tomorrow['nightText']}　"
            f"<font color=\"warning\">{tomorrow['low']}</font>~<font color=\"warning\">{tomorrow['high']}°C</font>　"
            f"{tomorrow['dayWindDirection']} {tomorrow['dayWindScale']}"
        )

    lines.append("")
    lines.append("")
    lines.append(f"> 更新时间 {last_update}")
    return "\n".join(lines)


def run_city_mode():
    if not WEBHOOK_URL:
        print("❌ 未设置 WECOM_WEBHOOK_URL")
        return

    for city, code in CITIES.items():
        print(f"\n📍 {city} ({code})")
        try:
            md = build_city_markdown(city, code)
            print(f"消息 {len(md.encode('utf-8'))} 字节:")
            print(md)
            if send_markdown(WEBHOOK_URL, md):
                print("✅ 发送成功")
            else:
                print("❌ 发送失败")
        except Exception as e:
            print(f"❌ 处理异常: {e}")


# ============ 主流程 ============

def process_page(channel_id: str, send_text: bool = True) -> bool:
    """处理一个频道：抓取→解析→发送
    send_text=False 时只发图片不发文字"""
    if channel_id not in PAGES:
        print(f"❌ 未知频道: {channel_id}")
        return False

    page = PAGES[channel_id]
    url = page["url"]
    name = page["name"]

    print(f"\n{'=' * 50}")
    print(f"📡 正在处理: {name} (channel-{channel_id})")
    print(f"   URL: {url}")

    # 1. 获取HTML
    try:
        html = fetch_html(url)
        print(f"✅ 页面获取成功 ({len(html)} 字节)")
    except Exception as e:
        print(f"❌ 页面获取失败: {e}")
        return False

    # 2. 解析文字信息
    text_info = parse_text_content(html)
    has_text = bool(text_info["title"] and (text_info.get("content_above") or text_info["content"]))
    if has_text:
        above = text_info.get("content_above", "")
        print(f"✅ 文字解析成功:")
        print(f"   标题: {text_info['title']}")
        print(f"   来源: {text_info['source']}")
        print(f"   时间: {text_info['publish_time']}")
        preview = above if above else text_info["content"]
        print(f"   正文: {preview[:80]}...")
        if text_info["defense_guide"]:
            print(f"   防御指南: {len(text_info['defense_guide'])} 条")
    else:
        print("ℹ️  无文字内容（纯图片页面）")

    # 3. 解析图片URL
    img_url = extract_image_url(html)
    if not img_url:
        print("❌ 未找到图片URL")
        return False
    print(f"✅ 图片URL: {img_url}")

    # 4. 下载图片
    img_path = download_image(img_url)
    if not img_path:
        return False

    # 5. 发送到企微
    if not WEBHOOK_URL:
        print("\n⏭️  跳过发送（未配置 Webhook）")
        return True

    print(f"\n📤 正在发送到企微Bot...")

    success = True

    if send_text and has_text:
        # 先发文字（Markdown格式）
        md_content = build_markdown_message(text_info, url)
        if not send_markdown(WEBHOOK_URL, md_content):
            plain = build_plain_text(text_info)
            send_markdown(WEBHOOK_URL, plain)

    # 发图片
    if not send_image(WEBHOOK_URL, img_path):
        success = False

    return success


def main():
    parser = argparse.ArgumentParser(description="中央气象台信息自动抓取 + 企微Bot推送")
    parser.add_argument(
        "--mode", type=str, default="page", choices=["page", "city"],
        help="运行模式: page=气象预警页面, city=城市天气"
    )
    parser.add_argument(
        "--channels", type=str, default="374,423,339",
        help="(page模式) 频道ID，逗号分隔"
    )
    parser.add_argument(
        "--image-only", action="store_true",
        help="(page模式) 仅发送图片"
    )
    args = parser.parse_args()

    print("=" * 50)
    print("🌤️  中央气象台信息自动抓取工具")
    print(f"⏰  运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if args.mode == "city":
        print("📋  模式: 城市天气")
        print("=" * 50)
        run_city_mode()
    else:
        channels = [c.strip() for c in args.channels.split(",")]
        send_text = not args.image_only
        print(f"📋  模式: 气象页面")
        print(f"📡  频道: {', '.join(channels)}")
        print(f"📝  文字模式: {'图片+文字' if send_text else '仅图片'}")
        print("=" * 50)

        if not WEBHOOK_URL:
            print("\n⚠️  未设置 WECOM_WEBHOOK_URL（仅本地测试模式）")

        for ch in channels:
            try:
                process_page(ch, send_text=send_text)
            except Exception as e:
                print(f"❌ 频道 {ch} 处理异常: {e}")

    print("\n✨ 全部完成!")
    print("=" * 50)


if __name__ == "__main__":
    main()
