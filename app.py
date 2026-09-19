#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
小红书笔记分析 · 云端版（输入链接 → 采集 → 分析 → 出报告）
============================================================

    streamlit run app.py

和本地版的区别（也是这个应用能在云端跑起来的原因）
--------------------------------------------------
| | 本地版 | 云端版（本应用） |
|---|---|---|
| 浏览器 | 系统 Chrome（`channel="chrome"`） | 容器里 apt 装的 chromium（`executable_path`） |
| 登录 | 弹出窗口**扫码** | **注入 Cookie**（云端没有可见窗口可扫） |
| 登录态 | `.browser_profile` 持久化 | 不需要，每次请求自带 Cookie |
| 图片 | 落盘到本地目录 | 落盘后立即内嵌 base64 进报告 |

⚠️ 需要你自备 Cookie，相当于把账号的访问权交给这个应用。
   建议把本应用设为 **Private**（Settings → Sharing），不要公开分享。
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import streamlit as st                      # noqa: E402
# 显式导入：st.components.v1 不是所有 Streamlit 版本都会自动挂到 st 上
from streamlit.components.v1 import html as st_html   # noqa: E402

import analyze_note as AN                   # noqa: E402
import xhs_fetcher as F                     # noqa: E402

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "data" / "notes"

RED, RED_SOFT = "#FF2442", "#FFF0F2"
T1, T2, T3 = "#1B1F26", "#5A6070", "#8C93A3"
LINE = "#E8EAF0"

st.set_page_config(page_title="小红书笔记分析 · 云端版",
                   page_icon="📕", layout="wide")

CSS = f"""
<style>
.stApp {{ background:#F5F6F8; }}
.block-container {{ padding-top:1.4rem; padding-bottom:3rem; max-width:1360px; }}
section[data-testid="stSidebar"] {{ background:#FFF8F9; border-right:1px solid {LINE}; }}
.hero {{
  border-radius:16px; padding:24px 28px; color:#fff; margin-bottom:16px;
  background:linear-gradient(120deg,#FF2442 0%,#FF5C7C 50%,#FF8FA3 100%);
}}
.hero h1 {{ font-size:25px; font-weight:800; margin:0 0 6px; color:#fff; }}
.hero p {{ margin:0; font-size:13.5px; color:rgba(255,255,255,.93); line-height:1.7; }}
.card {{ background:#fff; border:1px solid {LINE}; border-radius:13px;
         padding:15px 18px; margin-bottom:12px; }}
.card h3 {{ font-size:14px; font-weight:700; margin:0 0 5px; color:{T1}; }}
.card p {{ font-size:12.5px; color:{T3}; margin:0; line-height:1.65; }}
.kpis {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:11px; }}
.kpi {{ background:#fff; border:1px solid {LINE}; border-radius:12px; padding:12px 15px;
        position:relative; overflow:hidden; }}
.kpi::before {{ content:""; position:absolute; left:0; top:0; width:3px; height:100%;
                background:{RED}; opacity:.9; }}
.kpi .k {{ font-size:11.5px; color:{T3}; }}
.kpi .v {{ font-size:22px; font-weight:800; letter-spacing:-.5px; color:{T1}; }}
.kpi .n {{ font-size:11px; color:{T3}; }}
.warnbox {{ background:#FFF7E6; border:1px solid #F5DBA6; border-radius:11px;
            padding:12px 15px; font-size:12.5px; color:#7A5416; line-height:1.7; }}
.okbox {{ background:#EFFAF3; border:1px solid #C9EBD8; border-radius:11px;
          padding:12px 15px; font-size:12.5px; color:#1D7A45; line-height:1.7; }}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# 日志桥接：把采集器的日志实时刷到页面上
# ---------------------------------------------------------------------------
class UILogger(F.Logger):
    """继承采集器的 Logger，额外把新行推给 Streamlit。"""

    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def __call__(self, msg: str) -> None:
        super().__call__(msg)
        try:
            self.sink(list(self.buf))
        except Exception:
            pass                # 界面刷新失败不该影响采集


# ---------------------------------------------------------------------------
# Cookie 读取
# ---------------------------------------------------------------------------
def secret_cookie() -> str:
    """从 Streamlit secrets 读 Cookie（可选）。没配 secrets 时返回空串。"""
    try:
        return str(st.secrets.get("XHS_COOKIE", "") or "")
    except Exception:
        return ""               # 没有 secrets.toml 时 st.secrets 会直接抛错


def extract_cookie(text: str) -> str:
    """从「Cookie 串」**或**「Copy as cURL 的整段命令」里取出 Cookie。

    为什么支持 cURL：让用户手工从 Request Headers 里挑出 cookie 那一行很容易出错
    —— 尤其 `web_session` 是 HttpOnly，Console 里 `document.cookie` 读不到，
    只能在 Network 面板里找。而右键 → **Copy as cURL** 一次就复制了全部请求头，
    比手动挑选可靠得多。实测也验证了：有人会不自觉地去看**应用自己**的
    Network（全站静态资源），那里根本不可能有小红书的 Cookie。
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    # Chrome 的 "Copy as cURL (cmd)" 会把双引号转义成 ^"，先还原
    raw = raw.replace('^"', '"').replace("\\\n", " ")

    # ① cURL 命令 → 找 -H 'cookie: ...'（-b/--cookie 也认）
    if "curl" in raw.lower() or re.search(r"-H\s", raw):
        m = (re.search(r"-H\s+['\"]cookie:\s*([^'\"]+)['\"]", raw, re.I | re.S)
             or re.search(r"(?:-b|--cookie)\s+['\"]([^'\"]+)['\"]",
                          raw, re.I | re.S))
        if m:
            return m.group(1).strip()

    # ② 只粘了 Request Headers 里 `cookie: xxx` 那一行 → 必须剥掉前缀！
    #    否则 parse 出来的第一个字段名会变成 "cookie: web_session"，
    #    后面校验就找不到 web_session，反而报「缺少登录凭证」。
    m = re.match(r"\s*cookie\s*:\s*(.+)$", raw, re.I | re.S)
    if m:
        return m.group(1).strip()

    # ③ 兜底：文本里任意位置出现 cookie: 就取它后面
    m = re.search(r"cookie:\s*([^\r\n]+)", raw, re.I)
    if m:
        return m.group(1).strip()

    return raw


def cookie_ok(raw: str) -> tuple[bool, str]:
    if not raw.strip():
        return False, "还没填 Cookie"
    jar = F.parse_cookie_string(raw)
    names = {c["name"] for c in jar}
    if not jar:
        return False, ("这段文本里没解析出任何 Cookie。**最常见的原因**是"
                       "在「本应用」的 Network 面板里复制的 —— 那里只有应用"
                       "自己的 JS 和字体，没有小红书的 Cookie。"
                       "请回到 `xiaohongshu.com` 那个标签页再复制。")
    if "web_session" not in names:
        return False, ("Cookie 里缺少 `web_session`（登录凭证）。"
                       "注意它是 HttpOnly，`document.cookie` 读不到 ——"
                       "请到**小红书那个标签页**里用「Copy as cURL」拷贝，"
                       "整段粘进来即可。")
    return True, f"已识别 {len(jar)} 个 Cookie"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run_pipeline(url: str, cookie: str, max_pages: int, images: bool) -> dict:
    """采集 → 分析 → 出报告。返回一个结果字典，失败时 result['ok'] 为 False。"""
    out = {"ok": False, "error": "", "report_html": "", "report_path": None,
           "note_dir": None, "elapsed": 0.0}

    box = st.empty()

    def sink(lines):
        box.code("\n".join(lines[-16:]), language=None)

    log = UILogger(sink)
    t0 = time.time()

    ref = F.parse_input(url)
    if not ref.note_id and not ref.url:
        out["error"] = "没能从这段文字里识别出笔记链接或 note_id"
        return out
    log(f"解析链接：note_id={ref.note_id or '(待跳转解析)'} "
        f"xsec_token={'有' if ref.xsec_token else '无'}")

    with st.spinner("正在采集（首次跑要启动浏览器，约 20-60 秒）…"):
        note_dir = F.fetch_with_browser(
            ref, WORK, max_pages, images, True,
            headless=True, profile=ROOT / ".browser_profile",
            log=log, cookies=cookie)
    out["note_dir"] = str(note_dir) if note_dir else None

    if note_dir is None:
        # 直接把采集器日志的最后几行端上来。采集器**已经**给出了具体诊断
        # （Cookie 失效 / 人机验证 / 笔记不可见 / 结构变了），
        # 再列一通"所有可能原因"只会让人猜 —— 应该让用户看到真实的错误。
        tail = [ln for ln in log.buf[-8:] if ln.strip()]
        out["error"] = (
            "采集失败。以下是采集器的原始日志：\n\n```\n"
            + "\n".join(tail)
            + "\n```\n\n对照一下：\n\n"
            "· 「Cookie 无效或已过期」→ 重新复制 Cookie\n"
            "· 「人机验证 / 安全校验」→ 服务器 IP 被风控了，等一阵再试\n"
            "· 「笔记不可见」→ xsec_token 过期，回 App 重新复制分享链接\n"
            "· 「启动失败」→ 云端缺少 chromium，检查 packages.txt")
        out["elapsed"] = time.time() - t0
        box.empty()
        return out

    log("采集完成，开始分析…")
    with st.spinner("正在分析并生成报告…"):
        note, comments = AN.load_note(note_dir)
        if not comments:
            out["error"] = "这篇笔记没有采到任何评论（可能是评论为 0，或接口结构变了）"
            out["elapsed"] = time.time() - t0
            return out
        result = AN.analyze(note, comments)
        rp = AN.build_report(note_dir, note, result)
        out["report_path"] = str(rp)
        out["report_html"] = rp.read_text(encoding="utf-8")

    out["ok"] = True
    out["elapsed"] = time.time() - t0
    out["note"] = note
    out["stats"] = result["stats"]
    out["sentiment"] = result["sentiment"]
    out["kinds"] = result["kinds"]      # 注意：kinds 在 result 顶层，不在 result["stats"] 里
    box.empty()
    return out


def run_sample() -> dict:
    """用仓库自带的离线样例跑一遍：不联网、不需要 Cookie。

    存在的意义是把问题**分段隔离**：先确认「分析 → 出报告 → 渲染」是通的，
    再去查「采集」那一段。否则一旦失败，根本分不清是 Cookie、风控，
    还是分析代码本身有问题。
    """
    out = {"ok": False, "error": "", "report_html": "", "report_path": None,
           "note_dir": None, "elapsed": 0.0}
    sample = ROOT / "samples" / "sample_note"
    if not (sample / "note.json").exists():
        out["error"] = "仓库里缺少 samples/sample_note"
        return out
    t0 = time.time()
    with st.spinner("正在分析离线样例…"):
        note, comments = AN.load_note(sample)
        if not comments:
            out["error"] = "样例里没有评论数据"
            return out
        result = AN.analyze(note, comments)
        rp = AN.build_report(sample, note, result)
        out.update(report_path=str(rp),
                   report_html=rp.read_text(encoding="utf-8"),
                   ok=True, elapsed=time.time() - t0, is_sample=True,
                   note=note, stats=result["stats"],
                   sentiment=result["sentiment"],
                   kinds=result["kinds"],       # 同上：在顶层
                   note_dir=str(sample))
    return out


def embed_html(html: str, height: int = 1250) -> None:
    """把完整 HTML 报告嵌进页面。

    Streamlit 1.64 起把 `st.components.v1.html` 标为过期，提示改用 `st.iframe`；
    但两者签名不同 —— `st.iframe` **不接受 `scrolling` 参数**。
    这里优先用新 API，失败再回退，避免云端版本升级后直接报错。
    """
    fn = getattr(st, "iframe", None)
    if callable(fn):
        try:
            fn(html, height=height)
            return
        except Exception:
            pass
    st_html(html, height=height, scrolling=True)


def want_demo() -> bool:
    """URL 上带 `?demo=1` 时自动跑离线样例。

    好处有两个：① 可以直接分享一个**不需要 Cookie** 的演示链接；
    ② 让「整页渲染」这件事可以被自动化验证，而不用手点按钮。
    """
    try:
        v = st.query_params.get("demo")
    except Exception:
        return False
    if isinstance(v, list):
        v = v[0] if v else None
    return str(v or "").strip().lower() not in ("", "0", "false", "none")


def fmt_pct(v) -> str:
    """百分比格式化：小于 1% 时保留两位小数。

    直接 f"{0.94}%" 会显示成 0.94，但结合语境容易被当成 0；
    而 int 截断更糟 —— 0.94 直接变 0，把「有实质讨论」误报成「完全没有」。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{f:.2f}%" if 0 < f < 1 else f"{f:g}%"


def render_result(res: dict) -> None:
    st.markdown('<div class="okbox">✓ 分析完成</div>', unsafe_allow_html=True)
    st.write("")

    note = res.get("note") or {}
    s = res.get("stats") or {}
    se = res.get("sentiment") or {}
    # kinds 是 list[dict]，先转成 key → item 的映射
    kinds = {x.get("key"): x for x in (res.get("kinds") or [])}
    interact = note.get("interact") or {}

    cols = st.columns(4)
    cards = [
        # 字段名是 `top`（主楼）/ `sub`（二级），不是 top_level
        ("评论数", f"{s.get('total', 0):,}",
         f"主楼 {s.get('top', 0):,} · 二级 {s.get('sub', 0):,}"),
        ("情感净值", f"{se.get('net', 0):+.1f}",
         f"正面 {se.get('pos_pct', 0)}% · 负面 {se.get('neg_pct', 0)}%"),
        ("实质讨论", fmt_pct((kinds.get("discuss") or {}).get("pct", 0)),
         "评论里真正在表达观点的比例"),
        ("采集耗时", f"{res['elapsed']:.0f}s",
         "离线样例（未联网）" if res.get("is_sample")
         else f"图片 {len(note.get('images') or [])} 张"),
    ]
    for c, (label, val, note_txt) in zip(cols, cards):
        c.markdown(f'<div class="kpi"><div class="k">{label}</div>'
                   f'<div class="v">{val}</div><div class="n">{note_txt}</div></div>',
                   unsafe_allow_html=True)

    st.write("")
    st.markdown(
        f'<div class="card"><h3>{note.get("title") or "（无标题）"}</h3>'
        f'<p>作者 {((note.get("author") or {}).get("nickname") or "—")} · '
        f'点赞 {interact.get("liked", 0):,} · 收藏 {interact.get("collected", 0):,} · '
        f'评论 {interact.get("comment", 0):,}</p></div>', unsafe_allow_html=True)

    left, right = st.columns([3, 1])
    with right:
        if res.get("report_html"):
            st.download_button("⬇ 下载完整报告 HTML", res["report_html"],
                               file_name=(Path(res["report_path"]).name),
                               mime="text/html", use_container_width=True)
    with left:
        st.caption("完整报告（图集 · 情感 · 时段 · 地域 · 热词 · 高赞评论 · 全部评论）"
                   "↓ 在下面这个框里滚动查看，或下载后单独打开")

    if res.get("report_html"):
        embed_html(res["report_html"], height=1250)


def main() -> None:
    st.markdown(
        '<div class="hero"><h1>📕 小红书笔记分析</h1>'
        '<p>贴一个笔记链接 → 自动抓取<b>正文、图片、评论</b> → '
        '出情感/时段/地域/热词/意图构成分析报告。</p></div>',
        unsafe_allow_html=True)

    # ---------------- 侧边栏 ----------------
    with st.sidebar:
        st.markdown("#### 1. 登录 Cookie")
        raw_cookie = st.text_area(
            "粘贴 Cookie 或整段 cURL", value=secret_cookie(), height=120,
            help="两种都行：一段 `k=v; k2=v2`，或「Copy as cURL」的整段命令。",
            label_visibility="collapsed",
            placeholder="web_session=xxx; a1=yyy; ...\n—— 或者直接粘 curl 'https://...' -H 'cookie: ...' 整段")
        cookie = extract_cookie(raw_cookie)
        if cookie != raw_cookie.strip() and cookie:
            st.caption(f"✓ 已从 cURL 里提取出 Cookie（{len(cookie)} 字符）")
        else:
            st.caption("两种都行：Cookie 串，或「Copy as cURL」的整段命令（会自动提取）")
        ok, msg = cookie_ok(cookie)
        (st.success if ok else st.warning)(msg, icon="✅" if ok else "⚠️")

        with st.expander("怎么拿 Cookie？（必读，90% 的人第一次都拿错）"):
            st.markdown(
                "**关键：一定要在「小红书」那个标签页里操作。**\n\n"
                "有人会在**本应用**的 Network 面板里找 —— 那里面只有应用自己的\n"
                "JS 和字体（`react-dom`、`emotion`、`lodash` 那些），"
                "**不可能有小红书的 Cookie**。Network 面板只显示当前标签页。\n\n"
                "正确步骤：\n"
                "1. **新开一个标签页**，打开 `xiaohongshu.com` 并确认已登录"
                "（右上角有你的头像）\n"
                "2. 在这个标签页里按 `F12` → 切到 **Network**\n"
                "3. 按 `F5` **刷新**（不刷新可能没有请求）\n"
                "4. 点最上面那条 `www.xiaohongshu.com`（Type 是 `document`）的请求\n"
                "5. **右键 → Copy → Copy as cURL**\n"
                "6. 回到本应用，把**整段**粘进上面的框 —— 会自动提取 Cookie\n\n"
                "为什么不用 `document.cookie`：`web_session`（真正的登录凭证）是\n"
                "**HttpOnly**，JS 读不到。所以只能在请求头里取。")

        st.markdown("---")
        st.markdown("#### 2. 采集设置")
        max_pages = st.slider("最多滚动几轮（每轮约 20 条评论）",
                              3, 40, 20, step=1)
        images = st.checkbox("下载图片并内嵌进报告", value=True)
        st.caption("轮数越多越慢，也越容易触发风控。够用就行。")

        st.markdown("---")
        st.markdown(
            '<div class="warnbox">⚠️ <b>建议把本应用设为 Private</b><br>'
            '你的 Cookie 等同于账号访问权。'
            'Settings → Sharing → 只允许自己访问。</div>',
            unsafe_allow_html=True)

    # ---------------- 主区 ----------------
    url = st.text_input(
        "笔记链接", placeholder="https://www.xiaohongshu.com/explore/xxxxxxxx?xsec_token=...",
        label_visibility="collapsed")
    st.caption("在 App 里用「分享 → 复制链接」，**必须带 `xsec_token`**，"
               "否则会被 302 到 404。整段分享文案直接粘也可以。")

    c1, c2 = st.columns([1, 4])
    start = c1.button("开始分析", type="primary", use_container_width=True)
    clear = c2.button("清空结果", use_container_width=False)

    if clear:
        st.session_state.pop("res", None)

    if start:
        if not ok:
            st.error(f"先解决 Cookie 问题：{msg}")
        elif not url.strip():
            st.error("先贴一个笔记链接")
        else:
            st.session_state["res"] = run_pipeline(
                url.strip(), cookie, max_pages, images)

    with st.expander("🧪 想先确认能跑通？用离线样例试一遍（不需要 Cookie）"):
        st.caption("样例是公开数据集里的一篇真实笔记（2,663 条评论）。"
                   "不联网、不需要 Cookie，用来单独验证「分析 → 出报告 → 渲染」这一段。"
                   "如果样例能出报告、真链接不行，那问题就一定在采集环节。")
        if st.button("跑一遍离线样例"):
            st.session_state["res"] = run_sample()

    # ?demo=1 → 直接跑离线样例（分享用；也让渲染可被自动化验证）
    if not st.session_state.get("res") and want_demo():
        st.session_state["res"] = run_sample()

    res = st.session_state.get("res")
    if res:
        st.markdown("---")
        if res["ok"]:
            render_result(res)
        else:
            st.error(res["error"])
            if res.get("note_dir"):
                st.caption(f"采集产物目录：{res['note_dir']}"
                           "（含 debug_page.html / debug_api.txt 等诊断文件）")
    else:
        st.markdown(
            '<div class="card"><h3>使用步骤</h3><p>'
            '① 左侧填 Cookie（见「怎么拿 Cookie？」） → '
            '② 贴笔记链接 → ③ 点「开始分析」</p></div>',
            unsafe_allow_html=True)
        st.markdown(
            '<div class="card"><h3>为什么云端要填 Cookie？</h3><p>'
            '本地版可以弹出浏览器让你扫码登录；云端容器没有可见窗口，'
            '二维码没人看得见。所以改成你把已登录的 Cookie 带进来。'
            '代价是 Cookie 会过期，需要偶尔更新。</p></div>',
            unsafe_allow_html=True)
        st.markdown(
            '<div class="card"><h3>可能失败的原因（提前说清楚）</h3><p>'
            '① <b>服务器 IP 被风控</b> —— 云主机是机房 IP，'
            '小红书可能弹验证码，无头模式解不了。这是云端方案最现实的失败点。<br>'
            '② <b>Cookie 过期</b> —— 换一个即可。<br>'
            '③ <b>内存不够</b> —— chromium 比较吃内存，'
            '笔记评论特别多时可能被云端掐断。<br>'
            '④ <b>xsec_token 过期</b> —— 回 App 重新复制链接。</p></div>',
            unsafe_allow_html=True)


if __name__ == "__main__":
    main()
