# -*- coding: utf-8 -*-
"""
定时刷新 IPTV-HD.m3u 中的「央视·卫视(直播源)」分组。

数据来源: 第三方托管的 ysp-live 服务订阅地址
    https://8080-agqxmx2vxrekc4op3mzvjdqvha.instabox.tech/all.m3u

特点:
    - 该服务是持续直播（不是央视频的快照），可以直接长期订阅
    - 每次运行会重新拉取订阅、用 ffprobe 实测分辨率、剔除失效频道
    - 原播放列表里其它分组（超高清、央视频、央视、卫视、各省地方台、港澳台）原样保留

用法:
    python refresh-live.py              # 刷新并写回 IPTV-HD.m3u
    python refresh-live.py --check      # 只检查不写入

依赖:
    ffprobe（apt: ffmpeg / brew: ffmpeg / Windows 装 ffmpeg 加入 PATH）
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
PLAYLIST = os.path.join(HERE, "IPTV-HD.m3u")
SOURCE = "https://8080-agqxmx2vxrekc4op3mzvjdqvha.instabox.tech/all.m3u"
GROUP = "央视·卫视(直播源)"
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"

# 分组在最终列表中的固定顺序（其余分组保持原顺序）
ORDER_FIRST = ["超高清", "央视频", GROUP]


def log(msg):
    print(msg, flush=True)


def fetch_playlist():
    req = urllib.request.Request(SOURCE, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", "replace")


def parse(text):
    items, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            g = re.search(r'group-title="([^"]*)"', line)
            lg = re.search(r'tvg-logo="([^"]*)"', line)
            cur = {
                "name": line.rsplit(",", 1)[-1].strip(),
                "group": g.group(1) if g else "",
                "logo": lg.group(1) if lg else "",
            }
        elif line and not line.startswith("#") and cur:
            cur["url"] = line
            items.append(cur)
            cur = None
    return items


def probe(item):
    """用 ffprobe 实测分辨率，读不到视为不可用。"""
    try:
        p = subprocess.run(
            [FFPROBE, "-v", "error", "-rw_timeout", "20000000",
             "-analyzeduration", "10000000", "-probesize", "3000000",
             "-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-of", "csv=p=0", item["url"]],
            capture_output=True, text=True, timeout=60)
        lines = p.stdout.strip().splitlines()
        if lines:
            w, h = lines[0].split(",")[:2]
            item["w"], item["h"] = int(w), int(h)
            return item
    except Exception:
        pass
    item["w"], item["h"] = 0, 0
    return item


def sort_key(c):
    n = c["name"]
    m = re.match(r"^CCTV[-_ ]?(\d+)(\+)?", n)
    if m:
        return (0, int(m.group(1)), 1 if m.group(2) else 0)
    if n.startswith("CGTN"):
        return (1, 0, 0)
    if "卫视" in n:
        return (2, 0, 0)
    if n.startswith("CCTV"):
        return (3, 0, 0)
    return (4, 0, 0)


def logo(name):
    c = re.sub(r'[\\/:*?"<>|]', "", name).replace(" ", "")
    return "https://live.fanmingming.cn/tv/" + urllib.parse.quote(c) + ".png"


def render(channels):
    lines = [f"# ===== {GROUP} ({len(channels)}) =====",
             "# 该分组为第三方托管的 ysp-live 服务：持续直播（非快照），1080p 为主，含 4K"]
    for c in channels:
        n = c["name"]
        lines.append(
            f'#EXTINF:-1 tvg-name="{n}" tvg-logo="{logo(n)}" group-title="{GROUP}",{n}')
        lines.append(c["url"])
    return lines


def split_groups(lines):
    """解析成 (head, {分组: [行]}, 顺序)。"""
    head, groups, order, cur = [], {}, [], None
    for line in lines:
        if line.startswith("# ====="):
            # 分组名可能含括号，只去掉末尾的 " (数字)" 计数
            name = re.sub(r"\s*\(\d+\)\s*$", "", line.strip("# =").strip()).strip()
            cur = name
            if name not in groups:
                groups[name] = []
                order.append(name)
            groups[name].append(line)
            continue
        if line.startswith("#EXTM3U"):
            head.append(line)
            continue
        if line.startswith("#"):
            (groups[cur] if cur else head).append(line)
            continue
        if line.strip():
            (groups[cur] if cur else head).append(line)
    return head, groups, order


def update_uhd(groups, live):
    """把实测为 4K 的直播源补进「超高清」分组。"""
    uhd = groups.get("超高清")
    if not uhd:
        return
    fourk = [c for c in live if c.get("h", 0) >= 2160]
    have = set()
    for line in uhd:
        if line.startswith("#EXTINF"):
            have.add(line.rsplit(",", 1)[-1])
    body = uhd[0:1]
    for line in uhd[1:]:
        body.append(line)
    # 用直播源替换同名项（直播源更稳定），并补充缺的 4K
    replace = {c["name"]: c["url"] for c in fourk}
    out, pending, cur = [body[0]], None, None
    for line in uhd[1:]:
        if line.startswith("#EXTINF"):
            cur = line
            continue
        if line.strip() and cur:
            nm = cur.rsplit(",", 1)[-1]
            out.append(cur)
            out.append(replace.get(nm, line))
            pending = nm
    for c in fourk:
        if c["name"] not in have:
            out.append(
                f'#EXTINF:-1 tvg-name="{c["name"]}" tvg-logo="{logo(c["name"])}" group-title="超高清",{c["name"]}')
            out.append(c["url"])
    n = sum(1 for l in out if l.startswith("#EXTINF"))
    out[0] = f"# ===== 超高清 ({n}) ====="
    groups["超高清"] = out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查不写入")
    args = ap.parse_args()

    log(f"拉取订阅: {SOURCE}")
    text = fetch_playlist()
    items = parse(text)
    log(f"订阅共 {len(items)} 个频道，开始实测…")

    with ThreadPoolExecutor(max_workers=8) as ex:
        checked = list(ex.map(probe, items))
    live = [c for c in checked if c["h"] > 0]
    live.sort(key=sort_key)
    dead = [c["name"] for c in checked if c["h"] <= 0]
    log(f"可用 {len(live)}/{len(checked)}" + (f"，失效: {dead}" if dead else ""))

    if args.check:
        for c in live:
            log(f"  {c['w']}x{c['h']}  {c['name']}")
        return

    if not os.path.exists(PLAYLIST):
        sys.exit(f"找不到 {PLAYLIST}")
    lines = open(PLAYLIST, encoding="utf-8").read().splitlines()
    head, groups, order = split_groups(lines)

    groups[GROUP] = render(live)
    update_uhd(groups, live)

    first = [g for g in ORDER_FIRST if g in groups]
    rest = [g for g in order if g not in first and g != GROUP]
    final = first + rest

    out = list(head)
    for g in final:
        body = groups[g]
        if body and body[0].startswith("# ====="):
            n = sum(1 for l in body
                    if l.startswith("#EXTINF") and not l.rsplit(",", 1)[-1].endswith("-备用"))
            body = [f"# ===== {g} ({n}) ====="] + body[1:]
        out.extend(body)

    open(PLAYLIST, "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")
    log(f"已更新 {PLAYLIST}：{sum(1 for l in out if l.startswith('#EXTINF'))} 条，时间 {time.strftime('%Y-%m-%d %H:%M')}")


if __name__ == "__main__":
    main()
