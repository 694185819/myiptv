# -*- coding: utf-8 -*-
"""
刷新 IPTV-HD.m3u 中的「央视频」分组。

背景：央视频(R) App 的直播接口返回的地址是「时间窗口快照」，
每条地址只包含生成时刻前约 5 分钟的节目切片，播完即止，
因此需要在想看的时刻重新生成一次，或改用本地常驻代理。

用法:
    python refresh-ysp.py                  # 用本地代理(推荐，持续直播)
    python refresh-ysp.py --snapshot       # 重新生成直连快照分组

依赖:
    - 方式一(代理): 运行 ysp-live 服务，默认 http://127.0.0.1:8766
    - 方式二(快照): 需要同目录存在 ysp-live.py（见 https://github.com/funwukong/ysp-live）
"""
import argparse
import importlib.util
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
PROXY = "http://127.0.0.1:8766"
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
GROUP = "央视频"
SKIP = re.compile(r"4K|8K|超高清")           # 与列表规则一致：排除超高清台


def log(msg):
    print(msg, flush=True)


def load_ysp_module():
    path = os.path.join(HERE, "ysp-live.py")
    if not os.path.exists(path):
        sys.exit("找不到 ysp-live.py，请先下载 https://github.com/funwukong/ysp-live")
    spec = importlib.util.spec_from_file_location("ysp", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


def probe(url, timeout=45):
    """用 ffprobe 读出真实分辨率，读不到视为不可用。"""
    try:
        p = subprocess.run(
            [FFPROBE, "-v", "error", "-rw_timeout", "15000000",
             "-analyzeduration", "8000000", "-probesize", "2000000",
             "-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-of", "csv=p=0", url],
            capture_output=True, text=True, timeout=timeout)
        lines = p.stdout.strip().splitlines()
        if lines:
            return int(lines[0].split(",")[1])
    except Exception:
        pass
    return 0


def fetch_snapshot():
    """直连快照：调用 JCE 时移接口 / bkliveinfo 备用接口取地址。"""
    mod = load_ysp_module()
    now = int(time.time())

    def one(ch):
        slug, name, sid, pid, defn = ch
        if SKIP.search(name):
            return None
        for _ in range(2):
            try:
                u = mod.jce_timeshift_url(pid, sid, now - 300, now, defn)
                h = probe(u)
                if h:
                    return {"name": name, "url": u, "h": h}
            except Exception:
                pass
        try:
            for u in mod.bk_playurls(sid, pid, defn)[:3]:
                h = probe(u)
                if h:
                    return {"name": name, "url": u, "h": h}
        except Exception:
            pass
        return None

    out = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(one, mod.CHANNELS):
            if r:
                out.append(r)
    log(f"快照可用 {len(out)} 路")
    return out


def fetch_proxy():
    """常驻代理：订阅本地 ysp-live 的 all.m3u，持续直播。"""
    url = f"{PROXY}/all.m3u"
    try:
        text = urllib.request.urlopen(url, timeout=20).read().decode("utf-8")
    except Exception as e:
        sys.exit(f"连接本地代理失败 {url}: {e}\n  请先运行: python ysp-live.py 8766")
    out, name = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            name = line.rsplit(",", 1)[-1].strip()
        elif line and not line.startswith("#") and name:
            if not SKIP.search(name):
                full = line if line.startswith("http") else f"{PROXY}/{line.lstrip('/')}"
                out.append({"name": name, "url": full, "h": 1080})
            name = None
    log(f"代理订阅 {len(out)} 路")
    return out


def sort_key(c):
    n = c["name"]
    m = re.match(r"^CCTV-(\d+)(\+)?", n)
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
    c = re.sub(r"^\[[^\]]+\]\s*", "", name)
    c = re.sub(r'[\\/:*?"<>|]', "", c).replace(" ", "")
    return "https://live.fanmingming.cn/tv/" + urllib.parse.quote(c) + ".png"


def render(channels):
    lines = []
    for c in channels:
        n = c["name"]
        lines.append(
            f'#EXTINF:-1 tvg-name="{n}" tvg-logo="{logo(n)}" group-title="{GROUP}",{n}')
        lines.append(c["url"])
    return lines


def split_groups(lines):
    """把播放列表解析成 {分组名: [条目行...]}，保持原顺序。"""
    head, groups, order, cur = [], {}, [], None
    for line in lines:
        if line.startswith("# ====="):
            name = line.strip("# =").split("(")[0].strip()
            cur = name
            if name not in groups:
                groups[name] = []
                order.append(name)
            groups[name].append(line)          # 保留原分组标题（稍后校正计数）
            continue
        if line.startswith("#EXTM3U"):
            head.append(line)
            continue
        if line.startswith("#"):
            if cur is not None:
                groups[cur].append(line)
            else:
                head.append(line)
            continue
        if not line.strip():
            continue
        if cur is None:
            head.append(line)
        else:
            groups[cur].append(line)
    return head, groups, order


ORDER_FIRST = ["超高清", GROUP]


def splice(channels, mode):
    if not os.path.exists(PLAYLIST):
        sys.exit(f"找不到 {PLAYLIST}")
    raw = open(PLAYLIST, encoding="utf-8").read().splitlines()
    head, groups, order = split_groups(raw)

    # 过滤掉旧注释里的提示行，重新加
    head = [l for l in head if not l.startswith("# 央视频分组为") and not l.startswith("# 超高清分组")]

    if mode == "proxy":
        note = "# 央视频分组走本地代理 http://127.0.0.1:8766/all.m3u（持续直播，需本机保持 ysp-live 运行）"
    else:
        note = (f"# 央视频分组为直连快照（{time.strftime('%Y-%m-%d %H:%M')} 生成）："
                "地址有效期约 5 分钟，过期需重新运行本脚本")
    # 注释紧跟 #EXTM3U 之后，其余头部注释原样保留
    rest_head = [l for l in head[1:] if l.strip()]
    head = [head[0], note] + rest_head

    # 写入新央视频分组
    groups[GROUP] = [f"# ===== {GROUP} ({len(channels)}) ====="] + render(channels)

    # 固定顺序：超高清 -> 央视频 -> 其余保持原顺序
    first = [g for g in ORDER_FIRST if g in groups]
    rest = [g for g in order if g not in first and g != GROUP]
    if GROUP not in order:
        rest = [g for g in order if g not in first]
    final_order = first + rest

    out = list(head)
    for g in final_order:
        body = groups[g]
        # 校正分组标题里的计数
        if body and body[0].startswith("# ====="):
            n = sum(1 for l in body
                    if l.startswith("#EXTINF") and not l.rsplit(",", 1)[-1].endswith("-备用"))
            body = [f"# ===== {g} ({n}) ====="] + body[1:]
        out.extend(body)

    open(PLAYLIST, "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")
    log(f"已写入 {PLAYLIST}，共 {sum(1 for l in out if l.startswith('#EXTINF'))} 条")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", action="store_true",
                    help="生成直连快照（默认使用本地代理）")
    args = ap.parse_args()
    if args.snapshot:
        channels = fetch_snapshot()
        mode = "snapshot"
    else:
        channels = fetch_proxy()
        mode = "proxy"
    channels.sort(key=sort_key)
    splice(channels, mode)
    log("完成")


if __name__ == "__main__":
    main()
