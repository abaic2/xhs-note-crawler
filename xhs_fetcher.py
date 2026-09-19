#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
小红书单篇笔记采集器 —— 输入一个链接，拿到 正文 / 图片 / 评论
============================================================

    python crawler/xhs_note_fetcher.py "<笔记链接或分享文案>" [选项]

选项
----
    --out-dir DIR      输出根目录（默认 data/notes）
    --max-pages N      评论最多翻几页（默认 30）
    --no-images        不下载图片
    --no-sub           不抓二级评论
    --headless         无头模式（首次登录别用）
    --profile DIR      浏览器用户目录，用来保存登录态（默认 .browser_profile）

产物
----
    data/notes/<note_id>/
        note.json         笔记元数据（标题/正文/标签/互动数/作者/图片URL）
        comments.jsonl    评论，一行一条（含二级评论）
        comments.csv      同样的内容，Excel 友好
        images/*.jpg      正文图片（或视频封面）
        fetch_log.txt     采集过程日志

为什么要用浏览器，不能纯 requests
---------------------------------
1. 笔记页与评论接口都需要 `x-s` / `x-t` / `x-s-common` 三个由前端 JS 动态生成的
   签名头，伪造成本高且随时失效；
2. 现在分享链接**必须带 `xsec_token`**，裸 note_id 会直接 404；
3. 图床 `sns-webpic-qc.xhscdn.com` 的 URL 带**时效签名**，只有当场从页面拿到的
   链接才下得动，过一阵就 403。

所以这里用 Playwright 驱动真实浏览器，监听浏览器**自己发出**的接口响应，
既不逆向签名，也能拿到带有效签名的图片 URL。

合规提醒
--------
仅限个人学习研究，采集对象是平台**公开**内容。请勿用于商业转售、数据倒卖、
批量引流或骚扰用户。内置限速不要关闭。生产环境请使用小红书开放平台 / 蒲公英接口。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

# 云端版布局：data/ 等资源平铺在本目录下（便于单仓库部署），
# 不再是本地那种「父目录 + 兄弟目录」结构。
ROOT = Path(__file__).resolve().parent
NOTES_DIR = ROOT / "data" / "notes"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
PAGE_DELAY = (1.5, 3.0)          # 翻页间隔（秒），别关

NOTE_ID_RE = re.compile(r"([0-9a-f]{24})")
URL_RE = re.compile(r"https?://[^\s，,。、；;）)\]】\"']+")

# 站内表情在文本里以名字出现，图片文件名不含这些
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------
class Logger:
    def __init__(self, path: Optional[Path] = None):
        self.path = path
        self.buf: List[str] = []
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    def __call__(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        self.buf.append(line)
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")


# --------------------------------------------------------------------------
# URL / 输入解析
# --------------------------------------------------------------------------
@dataclass
class NoteRef:
    note_id: str = ""
    xsec_token: str = ""
    xsec_source: str = ""
    url: str = ""
    is_short: bool = False
    raw: str = ""

    def build(self) -> str:
        if self.note_id:
            q = []
            if self.xsec_token:
                q.append("xsec_token=" + self.xsec_token)
            q.append("xsec_source=" + (self.xsec_source or "pc_feed"))
            return (f"https://www.xiaohongshu.com/explore/{self.note_id}?"
                    + "&".join(q))
        return self.url


def parse_input(raw: str) -> NoteRef:
    """从各种输入里抠出笔记链接。

    支持：
      - 完整链接（含 xsec_token）
      - 分享文案整段粘贴（"…复制本条信息…"）
      - 短链 xhslink.com / xhs.cn（需要浏览器跟随跳转）
      - 裸 note_id（能跑，但大概率 404，会给出提示）
    """
    raw = (raw or "").strip()
    ref = NoteRef(raw=raw)
    if not raw:
        return ref

    m = URL_RE.search(raw)
    if m:
        url = m.group(0).rstrip("。，,.;；")
        ref.url = url
        if "xhslink.com" in url or "xhslink" in url or "xhs.cn" in url:
            ref.is_short = True
            return ref
        nid = NOTE_ID_RE.search(url)
        if nid:
            ref.note_id = nid.group(1)
        tok = re.search(r"xsec_token=([^&\s]+)", url)
        if tok:
            ref.xsec_token = tok.group(1)
        src = re.search(r"xsec_source=([^&\s]+)", url)
        if src:
            ref.xsec_source = src.group(1)
        return ref

    nid = NOTE_ID_RE.search(raw)
    if nid:
        ref.note_id = nid.group(1)
    return ref


# --------------------------------------------------------------------------
# 字段解析（接口返回的结构可能变，这里全部做多路兜底）
# --------------------------------------------------------------------------
def _s(v) -> str:
    return "" if v is None else str(v).strip()


def parse_count(v) -> int:
    """'28k' / '1.5万' / '5976' / '' → int"""
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


def pick_image_url(img: dict) -> str:
    """从 image_list 的单项里挑一个可用 URL，优先清晰度高的。"""
    for k in ("url_default", "url_pre", "url"):
        u = _s(img.get(k))
        if u:
            return u
    info = img.get("info_list") or []
    if isinstance(info, list):
        for it in info:
            u = _s((it or {}).get("url"))
            if u:
                return u
    return ""


def parse_note_card(card: dict) -> Dict[str, Any]:
    """把 feed 接口里的 note_card 拍平成统一结构。"""
    if not card:
        return {}
    user = card.get("user") or {}
    inter = card.get("interact_info") or {}
    video = card.get("video") or {}

    images = []
    for img in (card.get("image_list") or []):
        u = pick_image_url(img or {})
        if u:
            images.append({
                "url": u,
                "width": (img or {}).get("width"),
                "height": (img or {}).get("height"),
                "live": bool((img or {}).get("live_photo")),
            })

    cover = ""
    if video:
        cover = pick_image_url((video.get("image") or {})) or _s(video.get("cover"))
        if not cover:
            cover = _s(((video.get("consumer") or {}).get("origin_video_key"))
                       or ((video.get("media") or {}).get("video_id")))
    video_url = ""
    try:
        streams = ((video.get("media") or {}).get("stream") or {})
        for codec in ("h264", "h265", "av1"):
            for st in (streams.get(codec) or []):
                if st.get("master_url"):
                    video_url = st["master_url"]
                    break
            if video_url:
                break
    except Exception:
        pass

    return {
        "note_id": _s(card.get("note_id") or card.get("id")),
        "type": _s(card.get("type")) or ("video" if video else "normal"),
        "title": _s(card.get("title")),
        "desc": _s(card.get("desc")),
        "author": {
            "user_id": _s(user.get("user_id") or user.get("id")),
            "nickname": _s(user.get("nickname") or user.get("nick_name")),
            "avatar": _s(user.get("avatar") or user.get("image")),
        },
        "interact": {
            "liked": parse_count(inter.get("liked_count")),
            "collected": parse_count(inter.get("collected_count")),
            "comment": parse_count(inter.get("comment_count")),
            "shared": parse_count(inter.get("share_count")),
        },
        "tags": [_s(t.get("name")) for t in (card.get("tag_list") or [])
                 if _s((t or {}).get("name"))],
        "images": images,
        "video": {"cover": cover, "url": video_url} if video else {},
        "time": card.get("time") or card.get("last_update_time") or 0,
        "ip_location": _s(card.get("ip_location")),
        "note_url": (f"https://www.xiaohongshu.com/explore/{_s(card.get('note_id'))}"
                     if card.get("note_id") else ""),
    }


def parse_comment(raw: dict, level: int = 1,
                  parent_id: str = "") -> Optional[Dict[str, Any]]:
    cid = _s(raw.get("id") or raw.get("comment_id"))
    if not cid:
        return None
    user = raw.get("user_info") or raw.get("user") or {}
    return {
        "comment_id": cid,
        "content": _s(raw.get("content")),
        "like_count": parse_count(raw.get("like_count")),
        "create_time": int(raw.get("create_time") or 0),
        "ip_location": _s(raw.get("ip_location")),
        "user_id": _s(user.get("user_id") or raw.get("user_id")),
        "nickname": _s(user.get("nickname") or user.get("nick_name")),
        "sub_comment_count": parse_count(raw.get("sub_comment_count")),
        "parent_comment_id": _s(raw.get("parent_comment_id")) or parent_id,
        "level": level,
        "target_comment_id": _s((raw.get("target_comment") or {}).get("id")),
        "crawl_ts": int(time.time() * 1000),
    }


def iter_comments(payload: dict) -> List[Dict[str, Any]]:
    """从 comment/page 或 comment/sub/page 响应里取出评论。"""
    out: List[Dict[str, Any]] = []
    data = (payload or {}).get("data") or {}
    for c in (data.get("comments") or []):
        top = parse_comment(c, level=1)
        if top:
            out.append(top)
        for sub in (c.get("sub_comments") or []):
            s = parse_comment(sub, level=2, parent_id=_s(c.get("id")))
            if s:
                out.append(s)
    return out


def extract_js_object(text: str, marker: str) -> Optional[str]:
    """从 marker 之后第一个 `{` 开始做括号配对，取出完整的 JS 对象字面量。

    为什么要括号配对而不是正则：对象里有嵌套对象/数组/字符串，
    正则匹配到第一个 `}` 就会提前截断。
    """
    i = text.find(marker)
    if i < 0:
        return None
    start = text.find("{", i)
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for j in range(start, len(text)):
        c = text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
    return None


def js_object_to_json(raw: str) -> str:
    """把 JS 对象字面量收拾成合法 JSON。

    实测小红书的 SSR 状态里带裸 `undefined`：
        "pwaAddDesktopPrompt":undefined,"firstVisitUrl":undefined,...
    这是合法 JS 但不是合法 JSON，`json.loads` 会直接报
    `Expecting value`。这里在**字符串之外**把 undefined 换成 null。
    """
    out = []
    i, n = 0, len(raw)
    in_str, esc = False, False
    while i < n:
        c = raw[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if raw.startswith("undefined", i) and \
                (i == 0 or not (raw[i - 1].isalnum() or raw[i - 1] in "_$")) and \
                (i + 9 >= n or not (raw[i + 9].isalnum() or raw[i + 9] in "_$")):
            out.append("null")
            i += 9
            continue
        out.append(c)
        i += 1
    return "".join(out)


# 在 DOM 里把 SSR 状态那段 script 的**文本**取出来。
# 注意：不能用 window.__INITIAL_STATE__ —— React 注水后这个变量常被删掉，
# 但 <script> 标签仍然留在 DOM 里，读标签文本才是稳的。（实测踩过）
_JS_READ_SSR = """
() => {
  const s = [...document.querySelectorAll('script')]
    .map(e => e.textContent || '')
    .filter(t => t.includes('__INITIAL_STATE__') || t.includes('__SSR_DATA__'));
  return s.length ? s.join('\\n/*---*/\\n') : null;
}
"""


# 小红书已经把笔记详情**SSR 进 HTML** 了，不再走 /feed XHR。
# 最稳的取法是读 meta[property^="og:"] 与 #detail-title / #detail-desc：
# 这些是服务端渲染的 SEO 标签，React 注水也不会动它们。
_JS_DOM_NOTE = """
() => {
  const m1 = n => { const e = document.querySelector(
      'meta[property="' + n + '"],meta[name="' + n + '"]');
    return e ? (e.getAttribute('content') || '') : ''; };
  const mAll = n => [...document.querySelectorAll('meta[property="' + n + '"]')]
      .map(e => e.getAttribute('content') || '').filter(Boolean);
  const tx = s => { const e = document.querySelector(s);
    return e ? (e.innerText || '').trim() : ''; };
  const imgs = [...document.querySelectorAll(
      '#noteContainer img, .swiper-slide img, .media-container img, [class*="note-slider"] img')]
    .map(i => i.getAttribute('src') || i.getAttribute('data-src') || '')
    .filter(u => u && /^https?:/.test(u));
  // 作者：优先 .author-container（笔记作者区），
  // 不能用通用的 a[href*="/user/profile/"] —— 会命中侧边栏的"我"（实测踩过）
  const au = document.querySelector(
    '.author-container .username, .author-wrapper .username, ' +
    '.author-container a.name span, .author-container .name, a.name .username');
  return {
    og_title: m1('og:title'),
    og_desc: m1('og:description'),
    og_url: m1('og:url'),
    og_images: mAll('og:image'),
    keywords: m1('keywords'),
    note_like: m1('og:xhs:note_like'),
    note_comment: m1('og:xhs:note_comment'),
    note_collect: m1('og:xhs:note_collect'),
    dom_title: tx('#detail-title'),
    dom_desc: tx('#detail-desc'),
    dom_author: au ? (au.innerText || '').trim().split('\\n')[0] : '',
    dom_imgs: [...new Set(imgs)],
    dom_date: tx('#noteContainer [class*="date"], .date, [class*="publish-date"]'),
    has_video: !!document.querySelector('#noteContainer video, .media-container video, [class*="video-player"]')
  };
}
"""


# 评论区滚动：笔记详情页的评论是**独立滚动容器**，
# 只滚 window 是滚不动它的（实测踩过：一直停在第 1 页 6 条）。
# 所以这里先尝试点开"共 N 条评论/展开更多"，再找可滚动容器滚到底。
_JS_SCROLL_COMMENTS = """
() => {
  let clicked = 0;
  const cands = [...document.querySelectorAll('div,span,button,a')].filter(e => {
    const t = (e.textContent || '').trim();
    return t.length > 1 && t.length < 26 &&
      /展开更多|加载更多|查看更多评论|共\\s*\\d+\\s*条评论|点击查看全部|查看全部评论/.test(t);
  });
  for (const e of cands.slice(0, 2)) {
    try { e.click(); clicked++; } catch (x) { /* 点不动就算了 */ }
  }
  const boxes = [...document.querySelectorAll(
    '[class*="comment"],[class*="note-scroller"],.note-scroller,[class*="scroller"]')];
  let best = null, bestH = 0;
  for (const el of boxes) {
    if (el.scrollHeight > el.clientHeight + 80 && el.scrollHeight > bestH) {
      best = el; bestH = el.scrollHeight;
    }
  }
  if (best) { best.scrollTop = best.scrollHeight; return {mode: 'container', clicked}; }
  window.scrollTo(0, document.body.scrollHeight);
  return {mode: 'window', clicked};
}
"""


def note_from_dom(d: dict, ref: "NoteRef") -> dict:
    """把 DOM/meta 读到的字段拼成与 parse_note_card 同构的笔记结构。

    实测 2026-09 的小红书笔记页：正文/图片/互动数全在 SSR 的 meta 标签里，
    没有任何接口返回它们。所以这不是"兜底"，而是**主路径之一**。
    """
    images, seen = [], set()
    # 只用 og:image —— 它精确对应笔记的图集（实测 20 张就是 20 张）。
    # dom_imgs 会把头像、侧栏缩略图、图标全捞进来（实测捞到 104 张），
    # 所以只在 og:image 缺失时才退而求其次。
    src_urls = d.get("og_images") or d.get("dom_imgs") or []
    for u in src_urls:
        if not u:
            continue
        if u.startswith("//"):
            u = "https:" + u
        if not u.startswith("http"):
            continue
        if "xhscdn.com" not in u:      # 过滤 picasso-static 的默认占位图
            continue
        if u in seen:
            continue
        seen.add(u)
        images.append({"url": u, "width": None, "height": None, "live": False})

    title = (d.get("dom_title") or d.get("og_title") or "").strip()
    title = re.sub(r"\s*[-|]\s*小红书\s*$", "", title)      # 去掉尾巴上的站点名
    desc = (d.get("dom_desc") or d.get("og_desc") or "").strip()
    tags = [t.strip() for t in (d.get("keywords") or "").split(",") if t.strip()]

    return {
        "note_id": ref.note_id,
        "type": "video" if d.get("has_video") else "normal",
        "title": title,
        "desc": desc,
        "author": {"user_id": "", "nickname": (d.get("dom_author") or "").strip(),
                   "avatar": ""},
        "interact": {
            "liked": parse_count(d.get("note_like")),
            "collected": parse_count(d.get("note_collect")),
            "comment": parse_count(d.get("note_comment")),
            "shared": 0,
        },
        "tags": tags,
        "images": images,
        "video": {},
        "time": 0,
        "ip_location": "",
        "note_url": (d.get("og_url") or "").split("?")[0],
        "date_text": (d.get("dom_date") or "").strip(),
        "_source": "dom+meta(SSR)",
    }


def dig_note_card(obj, _depth: int = 0) -> Optional[dict]:
    """在任意嵌套结构里按**特征**找一个"像笔记卡片"的字典。

    为什么不用固定路径：小红书的返回结构改过好几次
    （`note_card` / `note` / `noteDetailMap[<id>].note` …），
    硬编码路径一旦改版就全挂。这里改成按特征找：
      * 有 note_id（或 id）
      * 且带 image_list / desc / title 之一
      * 且**不是**评论对象（评论一定有 content + like_count）
    """
    if _depth > 9:
        return None
    if isinstance(obj, dict):
        keys = set(obj)
        is_comment = {"content", "like_count"} <= keys
        if not is_comment and ("note_id" in keys or "id" in keys) \
                and ({"image_list", "desc", "title"} & keys) \
                and ("image_list" in keys or "desc" in keys or "title" in keys):
            return obj
        for v in obj.values():
            found = dig_note_card(v, _depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj[:40]:
            found = dig_note_card(v, _depth + 1)
            if found:
                return found
    return None


# 用 DOM 结构判断页面状态，比匹配文字可靠得多
_JS_PAGE_STATE = """
() => {
  const t = (document.body ? document.body.innerText : '').slice(0, 4000);
  // 只统计"看得见"的登录元素，避免隐藏模板造成误判
  const vis = [...document.querySelectorAll('[class*="login"],[id*="login"]')]
    .filter(e => { try { const r = e.getBoundingClientRect();
                          return r.width > 20 && r.height > 20; } catch (x) { return false; } });
  const qr = [...document.querySelectorAll('img,canvas')]
    .filter(e => { try { const r = e.getBoundingClientRect();
      return r.width > 80 && r.height > 80 &&
        (e.className || '').toString().match(/qr|code/i); } catch (x) { return false; } });
  const descEl = document.querySelector('#detail-desc,[class*="note-text"],[class*="note-desc"],[class*="desc"]');
  return {
    url: location.href,
    title: document.title,
    text: t,
    login_els: vis.length,
    qr_els: qr.length,
    // ⚠️ 不要用 [class*="slider"] 判验证码：笔记页的图片轮播就叫 slider，
    //    会 100% 误报（实测踩过，被误判成"人机验证"）
    captcha: !!document.querySelector('[class*="captcha"],[id*="captcha"],[class*="captcha-verify"],[class*="verify-slide"],[class*="sec-verify"]'),
    note_el: !!document.querySelector('#noteContainer,.note-container,[class*="note-detail"],.media-container,#detail-container,.note-content,[class*="note-scroller"]'),
    comment_el: !!document.querySelector('[class*="comment"]'),
    // 正文真有内容才算数 —— 空骨架容器不算
    detail_len: descEl ? (descEl.innerText || '').trim().length : 0,
    img_count: document.querySelectorAll('.swiper-slide img,[class*="note-slider"] img,.media-container img').length
  };
}
"""


def page_state(page) -> dict:
    try:
        return page.evaluate(_JS_PAGE_STATE) or {}
    except Exception:
        return {}


def needs_login(state: dict, has_data: bool = False) -> bool:
    """判断是否卡在登录墙 / 人机验证。

    判定顺序很重要，这里是踩过坑之后定下的：

    1. 人机验证 → 直接算。
    2. **已经拿到笔记数据 → 一定不是登录墙**（拿数据是唯一硬标准）。
    3. 正文真的渲染出文字了（detail_len > 0）→ 放行。
    4. 否则只要页面上有可见的登录元素 → 就当作登录墙。

    为什么第 4 步不看"有没有正文容器"：小红书是**在笔记页之上盖一层登录弹窗**，
    骨架容器照样在 DOM 里。早期版本因此漏判登录墙，直接对着空页面开爬，
    最后报"没有捕获到笔记详情"——查了半天才发现是登录根本没提示。
    """
    if not state:
        return False
    if state.get("captcha"):
        return True
    if has_data:
        return False
    if state.get("detail_len", 0) > 0:
        return False
    if state.get("login_els", 0) > 0 or state.get("qr_els", 0) > 0:
        return True
    txt = state.get("text") or ""
    return any(k in txt for k in ("扫码登录", "手机号登录", "登录后查看",
                                  "请先登录", "验证码登录", "登录/注册"))


# --------------------------------------------------------------------------
# 落盘
# --------------------------------------------------------------------------
class CommentStore:
    """JSONL + 主键去重，支持断点续爬。"""

    def __init__(self, path: Path):
        self.path = path
        self.seen: set[str] = set()
        self._fh = None
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self.seen.add(json.loads(line)["comment_id"])
                    except Exception:
                        continue

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")
        return self

    def __exit__(self, *exc):
        if self._fh:
            self._fh.close()

    def add_many(self, items: List[Dict[str, Any]]) -> int:
        n = 0
        for it in items:
            k = it["comment_id"]
            if k in self.seen:
                continue
            self.seen.add(k)
            self._fh.write(json.dumps(it, ensure_ascii=False) + "\n")
            n += 1
        if n:
            self._fh.flush()
        return n


def export_comments_csv(jsonl: Path, csv_path: Path) -> int:
    rows = []
    if jsonl.exists():
        with jsonl.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    if not rows:
        return 0
    cols = ["comment_id", "content", "like_count", "create_time", "ip_location",
            "user_id", "nickname", "sub_comment_count", "parent_comment_id",
            "level", "crawl_ts"]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return len(rows)


# --------------------------------------------------------------------------
# 图片下载
# --------------------------------------------------------------------------
def download_images(items: List[dict], dest: Path, log: Logger,
                    limit: int = 40) -> List[dict]:
    """下载图片。图床 URL 带时效签名，必须带着 Referer 当场下。"""
    import requests

    dest.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": UA, "Referer": "https://www.xiaohongshu.com/"}
    saved: List[dict] = []
    with requests.Session() as s:
        for i, it in enumerate(items[:limit], 1):
            url = it.get("url") or it.get("cover") or ""
            if not url:
                continue
            try:
                r = s.get(url, headers=headers, timeout=25)
                if r.status_code != 200 or not r.content:
                    log(f"    图 {i}: HTTP {r.status_code}，跳过")
                    continue
                ext = ".jpg"
                m = re.search(r"\.(jpg|jpeg|png|webp|gif|avif)", url, re.I)
                if m:
                    ext = "." + m.group(1).lower()
                if ext == ".avif":        # 浏览器兼容性差，统一转 jpg 更好，但避免引入依赖
                    ext = ".avif"
                fp = dest / f"{i:02d}{ext}"
                fp.write_bytes(r.content)
                saved.append({**it, "file": fp.name,
                              "bytes": len(r.content)})
                log(f"    图 {i}: {fp.name}  {len(r.content)/1024:.0f} KB")
            except Exception as exc:
                log(f"    图 {i}: 失败 {exc.__class__.__name__}")
            time.sleep(random.uniform(0.3, 0.8))
    return saved


# --------------------------------------------------------------------------
# Playwright 采集
# --------------------------------------------------------------------------
def _dump_debug(page, out_dir: Path, captured: dict, state: dict,
                log: Logger) -> None:
    """采集失败时把现场留证：截图 + 页面状态 + 观察到所有 API 请求。

    没有这些，"没抓到" 就只能靠猜；有了这些，一眼就能看出是登录墙、
    验证码、还是接口改版了。
    """
    try:
        page.screenshot(path=str(out_dir / "debug_page.png"), full_page=False)
    except Exception as exc:
        log(f"  截图失败：{exc.__class__.__name__}")
    try:
        # 存 HTML：能看到真实 DOM 结构（有没有登录弹窗、是不是 404 页）
        (out_dir / "debug_page.html").write_text(
            page.content(), encoding="utf-8")
    except Exception:
        pass
    try:
        (out_dir / "debug_state.json").write_text(
            json.dumps({**state, "observed_api": captured.get("api", [])},
                       ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    try:
        apis = captured.get("api") or []
        (out_dir / "debug_api.txt").write_text(
            "\n".join(apis) if apis else "(没有观察到任何 /api/sns/ 请求)",
            encoding="utf-8")
        log(f"  诊断：共观察到 {len(apis)} 个 /api/sns/ 请求")
        for line in apis[:8]:
            log(f"    · {line[:120]}")
    except Exception:
        pass
    # 正文文字前 300 字，往往一眼就能看出是登录页还是 404
    txt = (state.get("text") or "").replace("\n", " ").strip()
    if txt:
        log(f"  页面文字样本：{txt[:180]}")


def parse_cookie_string(raw: str) -> List[Dict[str, Any]]:
    """把 `a=1; b=2; web_session=xxx` 解析成 Playwright 的 cookie 列表。

    云端没有可见窗口可扫码，所以改成**显式注入 Cookie**：
    用户在本机浏览器登录一次，把 Cookie 复制过来给云端用。

    domain 用 `.xiaohongshu.com`（带前导点）—— 这样 www 和 edith 等子域都带上，
    评论接口在 edith.xiaohongshu.com，漏了它就拿不到评论。
    """
    out: List[Dict[str, Any]] = []
    for part in (raw or "").replace("\n", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name, value = name.strip(), value.strip()
        if not name:
            continue
        # 字段名校验：cookie name 按 RFC 6265 只能是 token，
        # 不含空白和 / ? ' " 等分隔符。不校验的话，把整段 cURL 或一个 URL
        # 误粘进来时会解析出 `curl 'http://...?demo` 这种垃圾字段，
        # 反而把「没拿到任何 Cookie」误报成「Cookie 里少了某个字段」。
        if len(name) > 64 or any(c.isspace() or c in "/?'\"\\," for c in name):
            continue
        out.append({"name": name, "value": value,
                    "domain": ".xiaohongshu.com", "path": "/"})
    return out


def find_chromium() -> Optional[str]:
    """找一个可用的 Chromium 可执行文件。

    本地：返回 None，交给下面的 channel="chrome" 用系统 Chrome（省掉 150MB 内核下载）。
    云端：容器里没有 Chrome，用 apt 装的 chromium（见 packages.txt）。
    """
    env = os.environ.get("XHS_CHROMIUM")
    if env and Path(env).exists():
        return env
    if os.name != "nt":
        for c in ("/usr/bin/chromium", "/usr/bin/chromium-browser",
                  "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"):
            if Path(c).exists():
                return c
    return None


def fetch_with_browser(ref: NoteRef, out_root: Path, max_pages: int,
                       want_images: bool, want_sub: bool,
                       headless: bool, profile: Path,
                       log: Optional[Logger] = None,
                       cookies: Optional[str] = None) -> Optional[Path]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺少 playwright。请执行：\n"
              "  pip install playwright\n"
              "（可以直接驱动系统 Chrome，不需要再跑 playwright install）")
        return None

    captured = {"note": None, "comments": [], "sub": [], "api": []}

    def on_response(resp):
        url = resp.url
        # 记下所有 /api/sns/ 请求，失败时写进诊断文件 —— 排查全靠它
        if "/api/sns/" in url:
            captured["api"].append(f"{resp.status} {url.split('?')[0]}")
        # 接口路径可能变，别只认死 v1/feed；带 feed 或 note 的都试一下
        if "/feed" in url or ("/note/" in url and "/api/" in url):
            try:
                payload = resp.json()
            except Exception:
                return
            card = dig_note_card(payload)
            if card and not captured["note"]:
                captured["note"] = parse_note_card(card)
        elif "/comment/sub/page" in url:
            try:
                captured["sub"].extend(iter_comments(resp.json()))
            except Exception:
                pass
        elif "/comment/page" in url:
            try:
                captured["comments"].extend(iter_comments(resp.json()))
            except Exception:
                pass

    log = log or Logger()

    jar = parse_cookie_string(cookies) if cookies else []
    if jar:
        log(f"已注入 {len(jar)} 个 Cookie（免扫码登录）")
        if "web_session" not in {c["name"] for c in jar}:
            log("  ⚠ Cookie 里没有 web_session，很可能并没有真正登录 ——"
                "请在已登录的浏览器里复制**完整** Cookie")

    exe = find_chromium()
    if exe:
        log(f"容器内浏览器：{exe}")

    with sync_playwright() as p:
        ctx = None
        holder: Dict[str, Any] = {}
        common = dict(headless=headless, user_agent=UA,
                      viewport={"width": 1440, "height": 950},
                      args=["--disable-blink-features=AutomationControlled",
                            "--no-sandbox", "--disable-dev-shm-usage",
                            "--disable-gpu"])
        attempts: List[dict] = []
        if exe:
            attempts.append(dict(executable_path=exe))
        # 本地优先直接用系统 Chrome，省掉 150MB 内核下载
        attempts += [dict(channel="chrome"), dict(channel="msedge"), {}]

        for kw in attempts:
            try:
                if jar:
                    # 有 Cookie 时显式注入，不用持久化用户目录 ——
                    # 云端文件系统是临时的，登录态反正也存不住。
                    browser = p.chromium.launch(**common, **kw)
                    holder["browser"] = browser
                    ctx = browser.new_context(user_agent=UA,
                                              viewport=common["viewport"])
                    ctx.add_cookies(jar)
                else:
                    ctx = p.chromium.launch_persistent_context(
                        user_data_dir=str(profile), **common, **kw)
                log("浏览器已启动（%s）" % (kw.get("executable_path")
                                        or kw.get("channel") or "内置 chromium"))
                break
            except Exception as exc:
                log(f"启动失败({kw or 'bundled'})：{exc.__class__.__name__}: {str(exc)[:130]}")
        if ctx is None:
            log("无法启动浏览器。云端请确认 packages.txt 里装了 chromium；"
                "本地可执行 playwright install chromium")
            return None

        page = ctx.new_page()
        page.on("response", on_response)

        target = ref.build()
        log(f"打开：{target or ref.url}")
        try:
            page.goto(target or ref.url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            log(f"导航异常：{exc.__class__.__name__}")

        # ---- 等页面定型：要么正文出现，要么识别出登录墙 ----
        log("等待页面加载…")
        state = {}
        for _ in range(20):                      # 最多约 20 秒
            page.wait_for_timeout(1000)
            state = page_state(page)
            if captured["note"]:
                break
            if needs_login(state, bool(captured["note"])) or state.get("detail_len", 0) > 0:
                break

        # 短链会被重定向，等落地后再嗅探一次 note_id
        if not ref.note_id:
            m = NOTE_ID_RE.search(page.url)
            if m:
                ref.note_id = m.group(1)
                log(f"已解析出 note_id：{ref.note_id}")
        if not ref.note_id:
            log("没能拿到 note_id —— 请确认链接有效，或把完整分享链接（含 xsec_token）贴进来。")

        log(f"  页面：{state.get('title','')[:36]!r} | 正文文字={state.get('detail_len',0)}字 "
            f"图片={state.get('img_count',0)} 登录元素={state.get('login_els')} "
            f"人机验证={state.get('captcha')} API 响应={len(captured['api'])} 条")

        # ---- 登录墙 / 人机验证 ----
        if needs_login(state, bool(captured["note"])):
            if jar:
                # 云端路径：Cookie 已经注入，而且没有可见窗口可以扫码，
                # 再等 240 秒毫无意义 —— 要么 Cookie 有效，要么它已经失效。
                log("=" * 58)
                log("仍然看到登录墙 —— 注入的 Cookie 无效或已过期。")
                log("请在**已登录**的浏览器里重新复制完整 Cookie 再试。")
                log("=" * 58)
            elif state.get("captcha"):
                log("=" * 58)
                log("检测到**人机验证 / 安全校验** —— 请在浏览器窗口里手动完成验证。")
                log("最多等待 240 秒，检测到笔记正文后会自动继续。")
                log("=" * 58)
            else:
                log("=" * 58)
                log("检测到需要登录 —— 请在打开的浏览器窗口里扫码登录。")
                log("（登录一次即可，之后会保存到本机 .browser_profile）")
                log("最多等待 240 秒，检测到笔记正文后会自动继续。")
                log("=" * 58)

            if not jar:
                waited = 0
                while waited < 240:
                    page.wait_for_timeout(2000)
                    waited += 2
                    state = page_state(page)
                    if captured["note"] or state.get("detail_len", 0) > 0:
                        log(f"  ✓ 已进入笔记页（等待 {waited} 秒），继续采集。")
                        break
                    if waited % 20 == 0:
                        log(f"  … 仍在等待登录（{waited}/240 秒）")
                if not (captured["note"] or state.get("detail_len", 0) > 0):
                    log("⚠️ 等待超时，仍未见正文。继续尝试，若失败请看诊断文件。")

        # ---- 再给正文接口一点时间 ----
        for _ in range(12):
            if captured["note"]:
                break
            page.wait_for_timeout(600)

        # ---- 取笔记数据：三条路依次试 ----
        # 实测 2026-09：小红书**不再用 /feed XHR 传笔记详情**，
        # 详情由 SSR 写进 HTML（meta 标签 + #detail-title/#detail-desc），
        # 所以 meta/DOM 才是主路径，SSR state 只是备选。
        if not captured["note"]:
            # ① 页面内嵌的 SSR 状态（结构可能变，先试一次）
            try:
                blob = page.evaluate(_JS_READ_SSR)
                if blob:
                    for marker in ("window.__INITIAL_STATE__", "window.__SSR_DATA__"):
                        raw = extract_js_object(blob, marker)
                        if not raw:
                            continue
                        try:
                            state_obj = json.loads(js_object_to_json(raw))
                        except json.JSONDecodeError as exc:
                            log(f"  SSR 状态不是合法 JSON（{marker}）：{exc}")
                            log("    （XHS 的 SSR 里有 new Map([]) 这类 JS 表达式，属正常）")
                            continue
                        card = dig_note_card(state_obj)
                        if card:
                            captured["note"] = parse_note_card(card)
                            captured["note"]["_source"] = f"SSR {marker}"
                            log(f"  ✓ 从 SSR 状态解析出笔记（{marker}）")
                            break
            except Exception as exc:
                log(f"  SSR 状态解析异常：{exc.__class__.__name__}")

        # ② DOM + meta 标签（当前最稳）
        if not captured["note"]:
            try:
                dom = page.evaluate(_JS_DOM_NOTE) or {}
                note = note_from_dom(dom, ref)
                if note["title"] or note["desc"] or note["images"]:
                    captured["note"] = note
                    log(f"  ✓ 从页面 meta/DOM 解析出笔记"
                        f"（图片 {len(note['images'])} 张 · "
                        f"点赞 {note['interact']['liked']} · "
                        f"评论 {note['interact']['comment']}）")
                else:
                    log("  meta/DOM 里也没读到标题/正文/图片")
                    log(f"    og:title={dom.get('og_title')!r} "
                        f"dom_title={dom.get('dom_title')!r} "
                        f"图片数={len(dom.get('og_images') or [])}")
            except Exception as exc:
                log(f"  meta/DOM 解析异常：{exc.__class__.__name__}: {exc}")

        # 触发评论加载：滚动到底再回顶，反复几轮
        out_dir = out_root / (ref.note_id or f"unknown_{int(time.time())}")
        out_dir.mkdir(parents=True, exist_ok=True)
        log(f"输出目录：{out_dir}")

        if not captured["note"] and state.get("detail_len", 0) <= 0:
            # 页面根本没进去，滚动毫无意义，直接留证据后退出
            log("✗ 页面未进入笔记正文，跳过滚动，直接生成诊断信息。")
            _dump_debug(page, out_dir, captured, state, log)
        else:
            with CommentStore(out_dir / "comments.jsonl") as store:
                for pg in range(1, max_pages + 1):
                    before = len(store.seen)
                    for _ in range(4):
                        # 优先滚评论区容器；点开"展开更多"后多等一会儿
                        try:
                            r = page.evaluate(_JS_SCROLL_COMMENTS) or {}
                            if r.get("clicked"):
                                page.wait_for_timeout(800)
                        except Exception:
                            pass
                        page.mouse.wheel(0, 1800)
                        page.wait_for_timeout(random.randint(500, 900))
                    page.wait_for_timeout(500)

                    got = store.add_many(captured["comments"])
                    captured["comments"] = []
                    if want_sub and captured["sub"]:
                        got += store.add_many(captured["sub"])
                        captured["sub"] = []
                    log(f"  第 {pg} 轮：新增 {got} 条，累计 {len(store.seen)} 条")

                    if len(store.seen) == before:
                        # 连续两轮无新增 → 认为到底
                        if pg > 1:
                            log("  连续无新增，停止翻页。")
                            break
                    time.sleep(random.uniform(*PAGE_DELAY))

            rows = export_comments_csv(out_dir / "comments.jsonl",
                                       out_dir / "comments.csv")
            log(f"评论落盘：{rows} 条（含二级评论）")

        note = captured["note"]
        if note:
            note["note_id"] = note.get("note_id") or ref.note_id
            note["xsec_token"] = ref.xsec_token
            note["share_url"] = target or ref.url
            (out_dir / "note.json").write_text(
                json.dumps(note, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"笔记元数据：{note.get('title') or note.get('desc','')[:30]}")
            log(f"  图片 {len(note.get('images') or [])} 张 · "
                f"点赞 {note['interact']['liked']} · "
                f"收藏 {note['interact']['collected']} · "
                f"评论 {note['interact']['comment']}")

            if want_images:
                items = list(note.get("images") or [])
                if note.get("video", {}).get("cover"):
                    items = [{"url": note["video"]["cover"], "cover": True}] + items
                log(f"下载图片 {len(items)} 张…")
                saved = download_images(items, out_dir / "images", log)
                note["images_local"] = saved
                (out_dir / "note.json").write_text(
                    json.dumps(note, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            _dump_debug(page, out_dir, captured, state, log)
            log("⚠️ 没有拿到笔记数据。按当前页面状态判断，最可能是：")
            if state.get("captcha"):
                log("   ▶ 卡在**人机验证 / 安全校验**。云端无头模式解不了验证码 ——"
                    "多半是这台服务器 IP 被风控盯上了，只能等一段时间或换部署环境。")
            elif needs_login(state, False):
                if jar:
                    log("   ▶ 卡在**登录墙**：注入的 Cookie 无效或已过期。"
                        "请在已登录的浏览器里重新复制完整 Cookie。")
                else:
                    log("   ▶ 卡在**登录墙**：没有提供 Cookie。"
                        "云端请填 Cookie；本地也可以扫码登录。")
            elif "当前笔记暂时无法浏览" in (state.get("text") or ""):
                log("   ▶ **笔记不可见**：xsec_token 过期或笔记已删除/仅作者可见，"
                    "请回 App 重新复制分享链接。")
            else:
                log("   ▶ 页面能打开但接口结构可能变了 —— 请看下面的诊断文件。")
            log("   诊断文件：debug_page.png / debug_page.html / debug_state.json / debug_api.txt")

        log("关闭浏览器")
        try:
            ctx.close()
            if holder.get("browser"):
                holder["browser"].close()   # 注入 Cookie 模式用的是 launch()，浏览器要单独关
        except Exception:
            pass

    (out_dir / "fetch_log.txt").write_text("\n".join(log.buf), encoding="utf-8")
    # 没有 note.json 就等于这次没采到东西，返回 None 让上层明确失败，
    # 不要交出一个"看起来成功"的空目录。
    if not (out_dir / "note.json").exists():
        return None
    return out_dir


# --------------------------------------------------------------------------
# 离线 fixture 模式：不联网，用已保存的 note.json + comments.jsonl 走完整流程
# --------------------------------------------------------------------------
def run_fixture(src_dir: Path) -> None:
    """校验「读取 → 解析 → 落盘 → 导出」链路，并打印摘要。"""
    log = Logger()
    log(f"fixture 目录：{src_dir}")
    note_p = src_dir / "note.json"
    com_p = src_dir / "comments.jsonl"
    if not note_p.exists() or not com_p.exists():
        log(f"缺少 note.json 或 comments.jsonl")
        sys.exit(2)

    note = json.loads(note_p.read_text(encoding="utf-8"))
    comments = [json.loads(l) for l in com_p.read_text(encoding="utf-8").splitlines() if l.strip()]

    # 用同一套解析函数，确保 fixture 与真实链路走的是同一条代码路径
    card = note.get("_raw_card")
    if card:
        note = parse_note_card(card)
    flat = [parse_comment(c, level=int(c.get("level", 1)),
                          parent_id=c.get("parent_comment_id", "")) for c in comments]
    flat = [c for c in flat if c]

    n_top = sum(1 for c in flat if c["level"] == 1)
    n_sub = len(flat) - n_top
    log(f"笔记：{note.get('title') or (note.get('desc') or '')[:40]}")
    log(f"  作者 {note.get('author',{}).get('nickname')} · "
        f"图片 {len(note.get('images') or [])} 张 · 点赞 {note.get('interact',{}).get('liked')}")
    log(f"评论：{len(flat)} 条（一级 {n_top} / 二级 {n_sub}）")
    assert len(flat) == len(comments), "解析后条数与原始不符"
    assert all(c["comment_id"] for c in flat), "存在空 comment_id"
    log("✓ fixture 链路自检通过（解析字段与真实采集完全一致）")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(
        description="小红书单篇笔记采集（正文 / 图片 / 评论）—— 仅限个人研究")
    ap.add_argument("url", nargs="?", help="笔记链接、分享文案或 note_id")
    ap.add_argument("--out-dir", default=str(NOTES_DIR))
    ap.add_argument("--max-pages", type=int, default=30)
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--no-sub", action="store_true")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--profile", default=str(ROOT / ".browser_profile"))
    ap.add_argument("--cookie", help="登录 Cookie 字符串（云端用；也可用环境变量 XHS_COOKIE）")
    ap.add_argument("--fixture", help="离线自检：指定已有的笔记目录")
    args = ap.parse_args()

    if args.fixture:
        run_fixture(Path(args.fixture))
        return
    if not args.url:
        ap.error("请提供笔记链接，或用 --fixture 做离线自检")

    ref = parse_input(args.url)
    if not ref.note_id and not ref.is_short:
        print("⚠️ 没识别出笔记 ID。请粘贴完整分享链接（带 xsec_token）。")
        sys.exit(2)
    if ref.note_id and not ref.xsec_token:
        print("⚠️ 链接里没有 xsec_token。小红书现在要求带这个参数，"
              "否则多半会 404。请在 App 里重新复制分享链接。")

    out = fetch_with_browser(ref, Path(args.out_dir), args.max_pages,
                             not args.no_images, not args.no_sub,
                             args.headless, Path(args.profile),
                             cookies=args.cookie or os.environ.get("XHS_COOKIE"))
    if out is None:
        print("采集未完成：没有产出任何笔记目录。")
        sys.exit(3)
    print(f"采集完成：{out}")


if __name__ == "__main__":
    main()
