#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
单篇小红书笔记 —— 评论分析 + 自包含 HTML 报告
============================================

    python analysis/analyze_note.py <笔记目录>            # 生成报告
    python analysis/analyze_note.py <笔记目录> --open      # 生成后自动打开
    python analysis/analyze_note.py --latest              # 用最近一次采集结果

输入目录里应有采集器产出的 note.json 与 comments.jsonl（见 crawler/xhs_note_fetcher.py）。
输出：reports/<note_id>.html —— 图片以 base64 内嵌，单文件可离线打开、可转发。

依赖：只用到标准库 + 同目录的 sentiment.py / wordextract.py。
"""

from __future__ import annotations

import argparse
import base64
import collections
import datetime as dt
import json
import math
import mimetypes
import re
import statistics
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sentiment as S            # noqa: E402
import wordextract as W          # noqa: E402

# 云端版布局：report/ assets/ data/ 都在本目录下（平铺，便于单仓库部署），
# 不再是本地那种「父目录 + 兄弟目录」结构。
ROOT = Path(__file__).resolve().parent
NOTES_DIR = ROOT / "data" / "notes"
REPORT_DIR = ROOT / "reports"
TPL = ROOT / "report" / "template_note.html"
ECHARTS = ROOT / "assets" / "echarts.min.js"

MAX_IMAGES = 20
# 内嵌图片总量上限。这也是**传输体积**的保险丝：
# 报告 HTML 要走 Streamlit 的 websocket 发给前端。
MAX_IMAGE_BYTES = 12 * 1024 * 1024
# 内嵌前把图缩到这个宽度 + JPEG 质量。这是体积/画质的取舍旋钮。
# 实测一篇 18 图的笔记（原图 3.87 MB，本身就是 1080px 的已优化 web 图）：
#   宽 1080 → 报告 5.9 MB ｜ 800 → 4.3 MB ｜ 720 → 3.8 MB
# 收益递减（照片类内容就是这样），所以取 800 这个折中点 ——
# 报告里图片显示宽度有限，看不出差别，但体积降了近 30%。
MAX_EMBED_WIDTH = 800
JPEG_QUALITY = 82
TOP_COMMENTS = 20
MAX_SAMPLE = 1500


# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------
def load_note(dirpath: Path):
    np = dirpath / "note.json"
    cp = dirpath / "comments.jsonl"
    if not np.exists():
        raise SystemExit(f"缺少 {np}")
    if not cp.exists():
        raise SystemExit(f"缺少 {cp}")
    note = json.loads(np.read_text(encoding="utf-8"))
    comments = []
    for line in cp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                comments.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return note, comments


def parse_count(v) -> int:
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().replace(",", "")
    if not s:
        return 0
    try:
        if s.endswith("万"):
            return int(float(s[:-1]) * 10_000)
        if s.lower().endswith("k"):
            return int(float(s[:-1]) * 1_000)
        return int(float(s))
    except ValueError:
        return 0


def to_dt(ms) -> dt.datetime | None:
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    if ms < 10_000_000_000:
        ms *= 1000
    try:
        return dt.datetime.fromtimestamp(ms / 1000)
    except (OverflowError, OSError, ValueError):
        return None


def pct(a, b) -> float:
    return round(a / b * 100, 2) if b else 0.0


# --------------------------------------------------------------------------
# 图片内嵌
# --------------------------------------------------------------------------
def _shrink(raw: bytes, name: str):
    """把图片缩到适合内嵌的尺寸。返回 (bytes, mime)。

    目的**只是压体积**，不是修 bug：报告要经 Streamlit 的 websocket 发给前端，
    体积越小越顺。实测 18 张图从 5.9 MB 降到 4.3 MB（见 MAX_EMBED_WIDTH 的注释）。
    注意小红书原图本来就是 1080px 的已优化 web 图，所以再编码收益有限 ——
    遇到"缩完反而更大"就直接用原图，不要为了统一而让文件变大。
    没装 Pillow 时原样返回，功能不受影响。
    """
    fallback = (raw, mimetypes.guess_type(name)[0] or "image/jpeg")
    try:
        import io

        from PIL import Image
    except ImportError:
        return fallback
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        if im.width > MAX_EMBED_WIDTH:
            h = max(1, round(im.height * MAX_EMBED_WIDTH / im.width))
            im = im.resize((MAX_EMBED_WIDTH, h), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
        out = buf.getvalue()
        return (out, "image/jpeg") if len(out) < len(raw) else fallback
    except Exception:
        return fallback


def embed_images(dirpath: Path, note: dict):
    """把已下载的图片缩放后转 base64 内嵌。没下载就返回空。"""
    candidates = []
    for im in (note.get("images_local") or []):
        if im.get("file"):
            candidates.append((im["file"], im.get("bytes") or 0))
    if not candidates:
        return [], ""

    img_dir = dirpath / "images"
    out, total, cover = [], 0, ""
    for name, size in candidates[:MAX_IMAGES]:
        fp = img_dir / name
        if not fp.exists():
            continue
        raw = fp.read_bytes()
        data_bytes, mime = _shrink(raw, name)
        if total + len(data_bytes) > MAX_IMAGE_BYTES:
            break
        data = f"data:{mime};base64," + base64.b64encode(data_bytes).decode()
        out.append({"data": data, "bytes": len(data_bytes), "file": name})
        total += len(data_bytes)
        if not cover:
            cover = data
    return out, cover


# --------------------------------------------------------------------------
# 分析
# --------------------------------------------------------------------------
def analyze(note: dict, comments: list[dict]) -> dict:
    # --- 清洗 + 情感 ---
    rows = []
    for c in comments:
        d = to_dt(c.get("create_time"))
        content = (c.get("content") or "").strip()
        if not content:
            continue
        score, label, hits = S.score_text(content)
        rows.append({
            "content": content,
            "like_count": parse_count(c.get("like_count")),
            "sub_comment_count": parse_count(c.get("sub_comment_count")),
            "ip_location": (c.get("ip_location") or "").strip(),
            "nickname": (c.get("nickname") or "").strip(),
            "level": int(c.get("level") or 1),
            "dt": d,
            "date": d.date().isoformat() if d else "",
            "hour": d.hour if d else None,
            "weekday": d.weekday() if d else None,
            "length": len(content),
            "sentiment": label,
            "score": score,
            "hits": hits,
        })

    n = len(rows)
    if n == 0:
        raise SystemExit("没有可用评论（内容为空或全部解析失败）")

    senti = collections.Counter(r["sentiment"] for r in rows)
    level = collections.Counter(r["level"] for r in rows)
    users = {r["nickname"] for r in rows if r["nickname"]}
    likes = sum(r["like_count"] for r in rows)
    ip_cov = sum(1 for r in rows if r["ip_location"])

    dated = [r for r in rows if r["dt"]]

    by_hour = [0] * 24
    by_hour_pos = [0] * 24
    by_hour_neg = [0] * 24
    for r in dated:
        by_hour[r["hour"]] += 1
        if r["sentiment"] == "positive":
            by_hour_pos[r["hour"]] += 1
        elif r["sentiment"] == "negative":
            by_hour_neg[r["hour"]] += 1
    by_hour_net = [round((by_hour_pos[i] - by_hour_neg[i]) / by_hour[i] * 100, 1)
                   if by_hour[i] else 0 for i in range(24)]

    by_week = [0] * 7
    for r in dated:
        by_week[r["weekday"]] += 1

    day_map = collections.OrderedDict()
    for r in sorted(dated, key=lambda x: x["dt"]):
        k = r["date"]
        d = day_map.setdefault(k, {"d": k, "n": 0, "pos": 0, "neg": 0})
        d["n"] += 1
        if r["sentiment"] == "positive":
            d["pos"] += 1
        elif r["sentiment"] == "negative":
            d["neg"] += 1
    by_day = list(day_map.values())

    # --- 地域 ---
    geo_c = collections.Counter(r["ip_location"] for r in rows if r["ip_location"])
    geo_pos = collections.Counter(r["ip_location"] for r in rows
                                  if r["ip_location"] and r["sentiment"] == "positive")
    geo_neg = collections.Counter(r["ip_location"] for r in rows
                                  if r["ip_location"] and r["sentiment"] == "negative")
    geo = [{"name": k, "comments": v, "share": pct(v, ip_cov),
            "pos": pct(geo_pos.get(k, 0), v), "neg": pct(geo_neg.get(k, 0), v)}
           for k, v in geo_c.most_common(18)]

    # --- 热词 ---
    # 单篇评论量比全库小得多，阈值要跟着降，否则捞不出几个词
    words_raw, method = W.extract((r["content"] for r in rows), top_k=120,
                                  min_freq=max(2, n // 800))
    words = []
    for w, cnt in words_raw:
        if len(w) < 2:
            continue
        hit = [r for r in rows if w in r["content"]]
        if not hit:
            continue
        p = sum(1 for r in hit if r["sentiment"] == "positive")
        ng = sum(1 for r in hit if r["sentiment"] == "negative")
        words.append({"name": w, "value": cnt,
                      "pos": pct(p, len(hit)), "neg": pct(ng, len(hit)),
                      "net": pct(p - ng, len(hit)),
                      "emoji": any(e in w for e in EMOJI)})
    words.sort(key=lambda x: -x["value"])
    words = words[:110]

    # --- 互动意图分类（解释情感为什么偏中性）---
    kind_cnt = collections.Counter()
    for r in rows:
        # 纯表情/纯语气词的评论单列
        stripped = re.sub(r"[\s\W_]+", "", r["content"])
        emoji_only = (not stripped) or all(ch in EMOJI_CHARS for ch in stripped)
        r["kind"] = classify_kind(r["content"], emoji_only)
        kind_cnt[r["kind"]] += 1
    KINDS = [
        ("ask", "求资源 / 求关注", "#FFB3BE"),
        ("discuss", "实质讨论（≥10字）", "#FF2442"),
        ("social", "情感回应（5-9字）", "#FF7A8A"),
        ("short", "极短点评（≤4字）", "#C9CCD6"),
        ("emoji", "纯表情 / 纯语气词", "#8FA5C7"),
    ]
    kinds = [{"key": k, "name": nm, "color": c, "n": kind_cnt.get(k, 0),
              "pct": pct(kind_cnt.get(k, 0), n)} for k, nm, c in KINDS]

    # 均值对比：求资源型 vs 实质讨论，看"哪种评论更容易被点赞"
    avg_by_kind = {}
    for k, nm, _ in KINDS:
        sub = [r for r in rows if r["kind"] == k]
        avg_by_kind[k] = {
            "name": nm,
            "n": len(sub),
            "avg_likes": round(sum(r["like_count"] for r in sub) / len(sub), 2) if sub else 0,
            "avg_len": round(sum(r["length"] for r in sub) / len(sub), 1) if sub else 0,
        }

    # --- 长度分布 ---
    bins = [0, 5, 10, 20, 30, 50, 80, 120, 200, 10 ** 9]
    labels = ["1-5", "6-10", "11-20", "21-30", "31-50", "51-80", "81-120",
              "121-200", "200+"]
    len_counts = [0] * len(labels)
    for r in rows:
        for i in range(len(bins) - 1):
            if bins[i] < r["length"] <= bins[i + 1]:
                len_counts[i] += 1
                break
        else:
            len_counts[0] += 1

    # --- 高赞评论 ---
    top = sorted(rows, key=lambda r: -r["like_count"])[:TOP_COMMENTS]
    top_comments = [{
        "content": r["content"][:200], "likes": r["like_count"],
        "nickname": r["nickname"], "ip": r["ip_location"] or "—",
        "date": r["date"] or "—", "sentiment": r["sentiment"],
        "score": r["score"], "level": r["level"],
        "replies": r["sub_comment_count"],
    } for r in top]

    # --- 浏览器抽样：优先嵌全部，超上限则按赞数分层抽样 ---
    if n <= MAX_SAMPLE:
        pool = rows
    else:
        pool = sorted(rows, key=lambda r: -r["like_count"])[:MAX_SAMPLE]
    sample = [{
        "content": r["content"][:160], "likes": r["like_count"],
        "sentiment": r["sentiment"], "date": r["date"] or "—",
        "nickname": r["nickname"], "ip": r["ip_location"] or "—",
        "level": r["level"], "kind": r["kind"],
    } for r in pool]

    span_days = 0
    if dated:
        first, last = min(r["dt"] for r in dated), max(r["dt"] for r in dated)
        span_days = max(1, (last.date() - first.date()).days + 1)
        first_s, last_s = first.date().isoformat(), last.date().isoformat()
    else:
        first_s = last_s = "—"

    ok = senti["positive"] + senti["negative"]
    return {
        "rows": rows,
        "stats": {
            "total": n,
            "top": level[1], "sub": level[2],
            "users": len(users),
            "per_user": round(n / len(users), 2) if users else 0,
            "likes": likes,
            "avg_likes": round(likes / n, 2),
            "sub_ratio": pct(level[2], n),
            # 覆盖率可能 >100%：采集含二级评论，而笔记页显示的 comment_count 通常只算一级。
            "coverage": min(100.0, pct(n, parse_count(
                (note.get("interact") or {}).get("comment")) or n)),
            "span_days": span_days,
            "first_date": first_s, "last_date": last_s,
            "ip_cov": pct(ip_cov, n),
            "by_hour": by_hour, "by_hour_pos": by_hour_pos,
            "by_hour_net": by_hour_net,
            "by_week": by_week, "by_day": by_day,
            "len_labels": labels, "len_counts": len_counts,
        },
        "sentiment": {
            "positive": senti["positive"], "neutral": senti["neutral"],
            "negative": senti["negative"],
            "pos_pct": pct(senti["positive"], n),
            "neu_pct": pct(senti["neutral"], n),
            "neg_pct": pct(senti["negative"], n),
            "net": pct(senti["positive"] - senti["negative"], n),
            "pos_rate_among_polar": pct(senti["positive"], ok) if ok else 0,
        },
        "geo": geo, "words": words, "word_method": method,
        "top_comments": top_comments, "sample": sample,
        "kinds": kinds, "avg_by_kind": avg_by_kind,
    }


EMOJI = set("""
笑哭 大笑 微笑 偷笑 憨笑 坏笑 奸笑 呲牙 害羞 捂脸 无语 石化 呆 懵逼 惊呆
害怕 惊恐 流泪 大哭 哭惹 委屈 可怜 抓狂 发怒 生气 白眼 抠鼻 哈欠 困
得意 酷 机智 灵光一闪 思考 星星眼 色 舔屏 吃瓜 狗头 比心 赞 鼓掌 祈祷
加油 干杯 玫瑰 爱心 心碎 飞吻 炸弹 骷髅 便便 月亮 太阳 招财猫 红包
礼物 蛋糕 樱花 树叶 啤酒 咖啡 药丸 冲鸭 打call 星星 火 火箭 飞机
""".split())

# 站内表情名 + 语气词用到的字。整条评论只由这些字组成时，判为"纯表情/纯语气"。
EMOJI_CHARS = set("".join(EMOJI)) | set("哈哈哈嘻嘿嘿呵嗯哦啊呀额呃唉哎哟呜嗷啧嘞")

# 「求资源型」评论：目的不是表达观点，而是索取资料 / 求关注。
# 这类评论在引流帖里能占到 90%+，会把情感分布整体拉成中性，
# 不单独拆出来会严重误读一篇笔记的评论区生态。
ASK_PATTERNS = [
    r"想要", r"求[分享资料链接教程模板软件课导图报告知]", r"^求", r"求$",
    r"关注", r"已关", r"^斯", r"斯哈?", r"滴滴", r"蹲", r"码住", r"收藏了",
    r"私信", r"发我", r"给我", r"求带", r"带带我", r"想要\+", r"^[＋+]$",
    r"谢谢", r"感谢", r"留言", r"扣1", r"111", r"回复我",
]
_ASK_RE = re.compile("|".join(ASK_PATTERNS))


def classify_kind(content: str, emoji_only: bool) -> str:
    """把评论按**互动意图**分类，而不是按情感。

    ask     —— 求资源/求关注（"想要""求分享""关注斯"）
    social  —— 纯社交回应（"哈哈哈""谢谢""来了"）
    short   —— 极短但有内容（"牛""好看"）
    discuss —— 有实质表达（≥10 字且非求资源）
    """
    t = content.strip()
    if emoji_only:
        return "emoji"
    if _ASK_RE.search(t):
        return "ask"
    if len(t) <= 4:
        return "short"
    if len(t) < 10:
        return "social"
    return "discuss"



# --------------------------------------------------------------------------
# 组装报告
# --------------------------------------------------------------------------
def load_eval_text() -> str:
    p = ROOT / "data" / "processed" / "eval_summary.json"
    if not p.exists():
        return ""
    try:
        ev = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return ""
    best = ev.get("test_sample") or ev.get("label_sample")
    if not best:
        return ""
    name = "留出集" if ev.get("test_sample") else "开发集"
    return (f"情感模型人工校验准确率 {best['accuracy']*100:.1f}%"
            f"（{best['n']} 条{name}标注）")


def build_report(dirpath: Path, note: dict, result: dict) -> Path:
    if not TPL.exists():
        raise SystemExit(f"缺少报告模板：{TPL}")
    if not ECHARTS.exists():
        raise SystemExit(f"缺少 ECharts：{ECHARTS}")

    tpl = TPL.read_text(encoding="utf-8")
    js = ECHARTS.read_text(encoding="utf-8", errors="replace")
    if "</script" in js.lower():
        raise SystemExit("ECharts 源码含 </script，无法内联")

    wall, cover = embed_images(dirpath, note)
    t = to_dt(note.get("time"))

    payload = {
        "meta": {
            "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "note_source": dirpath.name,
            "word_method": result["word_method"],
            "sentiment_method": "自建领域词典 + 否定/程度/弱化上下文规则（可解释）",
            "sentiment_eval_text": load_eval_text(),
        },
        "note": {
            "note_id": note.get("note_id", ""),
            "title": note.get("title", ""),
            "desc": note.get("desc", ""),
            "type": note.get("type", "normal"),
            "author": note.get("author") or {},
            "interact": note.get("interact") or {},
            "tags": note.get("tags") or [],
            "images": note.get("images") or [],
            "time_str": t.strftime("%Y-%m-%d %H:%M") if t else "",
            "ip_location": note.get("ip_location", ""),
            "cover_data": cover,
        },
        "stats": result["stats"],
        "sentiment": result["sentiment"],
        "geo": result["geo"],
        "words": result["words"],
        "top_comments": result["top_comments"],
        "sample": result["sample"],
        "images_wall": wall,
        "kinds": result["kinds"],
        "avg_by_kind": result["avg_by_kind"],
    }

    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    text = text.replace("</", "<\\/")

    html = tpl.replace("/*__ECHARTS__*/", js).replace("/*__DATA__*/null", text)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    name = (note.get("note_id") or dirpath.name)[:40]
    out = REPORT_DIR / f"note_{name}.html"
    out.write_text(html, encoding="utf-8")
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def latest_note_dir() -> Path:
    if not NOTES_DIR.exists():
        raise SystemExit(f"还没有采集结果（{NOTES_DIR} 不存在）")
    dirs = [d for d in NOTES_DIR.iterdir()
            if d.is_dir() and (d / "note.json").exists()]
    if not dirs:
        raise SystemExit("没有找到带 note.json 的采集目录")
    return max(dirs, key=lambda d: (d / "note.json").stat().st_mtime)


def main() -> None:
    ap = argparse.ArgumentParser(description="单篇笔记评论分析 → 自包含 HTML 报告")
    ap.add_argument("dir", nargs="?", help="采集产物目录")
    ap.add_argument("--latest", action="store_true", help="使用最近一次采集结果")
    ap.add_argument("--open", action="store_true", help="生成后自动打开浏览器")
    args = ap.parse_args()

    dirpath = Path(args.dir) if args.dir else latest_note_dir()
    if not dirpath.is_absolute():
        dirpath = (ROOT / dirpath).resolve()
    if not dirpath.exists():
        raise SystemExit(f"目录不存在：{dirpath}")

    print(f"读取 {dirpath}")
    note, comments = load_note(dirpath)
    print(f"  笔记：{(note.get('title') or note.get('desc') or '')[:48]}")
    print(f"  评论：{len(comments)} 条")

    print("分析中…")
    result = analyze(note, comments)
    S_ = result["stats"]
    E = result["sentiment"]
    print(f"  去重用户 {S_['users']} 人 · 二级评论 {S_['sub_ratio']}% · "
          f"IP 覆盖 {S_['ip_cov']}%")
    print(f"  情感：正面 {E['pos_pct']}% / 中性 {E['neu_pct']}% / 负面 {E['neg_pct']}%"
          f" · 净值 {E['net']}")
    print(f"  热词方案 {result['word_method']} · 保留 {len(result['words'])} 个")

    out = build_report(dirpath, note, result)
    print(f"✓ 报告已生成：{out}")
    print(f"  {out.stat().st_size/1024/1024:.2f} MB（图片已内嵌，单文件可离线打开）")
    if args.open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
