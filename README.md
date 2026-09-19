# 小红书笔记分析 · 云端版

贴一个笔记链接，自动抓取**正文、图片、评论**，并生成情感 / 时段 / 地域 / 热词 / 互动意图的分析报告。

部署在 Streamlit Community Cloud 上，**输入链接就能跑** —— 不需要你本机开着电脑。

---

## 和本地版的区别

| | 本地版 | **云端版（本应用）** |
|---|---|---|
| 浏览器 | 系统 Chrome（`channel="chrome"`） | 容器里 apt 装的 chromium（`executable_path`） |
| 登录 | 弹出窗口**扫码** | **注入 Cookie** —— 云端没有可见窗口，二维码没人看得见 |
| 登录态 | `.browser_profile` 持久化 | 不需要，每次请求自带 Cookie |
| 图片 | 落盘在本地目录 | 落盘后立即内嵌 base64 进报告，随报告一起给你 |

换句话说：**云端唯一真正做不到的是「扫码」**，用「你自己把 Cookie 带进来」绕过去了。
代价是 Cookie 会过期，需要偶尔更新。

---

## 怎么用

1. **拿 Cookie** —— 这是最关键的一步

   `web_session` 是 **HttpOnly**，所以在 Console 里执行 `document.cookie` **读不到**。正确做法：

   1. 浏览器里**登录**小红书（xiaohongshu.com）
   2. 按 `F12` → **Network** 面板
   3. 刷新页面，点任意一个 `xiaohongshu.com` 的请求
   4. 在 **Request Headers** 里找到 `cookie:` 那一行
   5. **整行的值全部复制**

2. **贴链接** —— 在小红书 App 里用「分享 → 复制链接」，**必须带 `xsec_token`**，
   裸 note_id 会被 302 到 404。整段分享文案直接粘也行。

3. 点「开始分析」，等 20–60 秒。

---

## 先跑离线样例（推荐）

应用里有个「**跑一遍离线样例**」按钮，用的是仓库自带的公开数据集真实笔记（2,663 条评论）。

它**不联网、不需要 Cookie**，用来把问题分段隔离：
如果样例能出报告、真链接不行，那问题一定在采集环节，不用怀疑分析代码。

---

## 可能失败的原因（提前说清楚）

| 现象 | 原因 | 怎么办 |
|---|---|---|
| 提示 Cookie 无效/过期 | `web_session` 失效 | 重新复制 Cookie |
| 提示**人机验证** | **服务器是机房 IP，被风控盯上** | 这是云端方案最现实的失败点，等一段时间再试 |
| 提示笔记不可见 | `xsec_token` 过期，或笔记已删 | 回 App 重新复制分享链接 |
| 进程被掐断 | chromium 吃内存，评论特别多的笔记可能超限 | 把「最多滚动几轮」调小 |
| 起不来浏览器 | apt 的 chromium 缺失或依赖不全 | 检查 `packages.txt` |

---

## ⚠️ 安全与合规

- **Cookie 等同账号访问权。** 建议把本应用设为 **Private**
  （Settings → Sharing → 只允许自己访问），不要公开分享。
- 只采集平台**公开**内容，仅限个人学习研究。
  **不得**用于商业转售、数据倒卖、批量引流或骚扰用户。
- 内置限速（每轮 1.5–3 秒随机间隔），**不要关**。
- 生产/商用请使用小红书开放平台等官方接口。

---

## 本地跑

```bash
pip install -r requirements.txt
# 本地没有 /usr/bin/chromium 时，指向系统 Chrome：
#   Linux/macOS:  export XHS_CHROMIUM=$(which google-chrome)
#   Windows:      set XHS_CHROMIUM=C:\Program Files\Google\Chrome\Application\chrome.exe
streamlit run app.py
```

环境变量 `XHS_CHROMIUM` 可以不设 —— 不设时它会依次尝试系统 Chrome / Edge / 内置 chromium，
和你本地版行为一致。

---

## 文件说明

| 文件 | 作用 |
|---|---|
| `app.py` | Streamlit 界面 + 主流程编排 |
| `xhs_fetcher.py` | 采集器（浏览器驱动、Cookie 注入、笔记/评论解析、图片下载） |
| `analyze_note.py` | 情感 + 热词 + 时段 + 地域 + 意图分类 → 自包含 HTML 报告 |
| `sentiment.py` | 情感词典引擎（零模型依赖、可解释） |
| `wordextract.py` | 热词提取（有 jieba 用分词，没有降级为 n-gram） |
| `report/template_note.html` | 报告模板 |
| `assets/echarts.min.js` | 内联进报告，保证离线可开 |
| `samples/sample_note/` | 离线样例（公开数据集真实笔记） |
| `packages.txt` | 云端 apt 依赖：`chromium` + 中文字体 |
