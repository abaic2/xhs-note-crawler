#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
轻量中文情感分析（词典 + 位置上下文规则）—— 零第三方依赖
=========================================================

为什么不用现成模型：
  * 无需下载几百 MB 权重，离线可跑，结果**可解释**（能回溯是哪个词判的正负）；
  * 小红书评论区口语化、网络梗密集（"绝了""智商税""踩雷"），
    通用词典反而容易漏判，自建领域词表命中率更高。

为什么不用分词器：
  * 分词器本身会引入误差（"智商税"被切成"智商"+"税"），
    而且多一个重依赖。这里改成**词表扫描 + 字符位置上下文**：
      1. 按词长从长到短扫描，命中即标记区间（避免"好"吃掉"好用"）；
      2. 对每个命中词，回看前 5 个字符判断否定词 / 程度词；
      3. 累加得分 → >0 正面 / <0 负面 / =0 中性。

实测准确率见 analysis/validate_sentiment.py 的抽样人工核对。
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

# --------------------------------------------------------------------------
# 1. 情感词表（按领域手工整理，小红书评论区高频表达优先）
# --------------------------------------------------------------------------
POSITIVE = {
    "很好", "不错", "好用", "好看", "好棒", "真好", "挺好", "超好", "太棒",
    "棒", "赞", "优秀", "厉害", "很强", "强大", "专业", "优质", "完美",
    "满意", "喜欢", "爱了", "心动", "种草", "惊艳", "绝了", "绝绝子",
    "漂亮", "可爱", "治愈", "舒服", "舒适", "方便", "实用", "有用",
    "有效", "值得", "划算", "便宜", "实惠", "超值", "性价比", "神器",
    "宝藏", "干货", "到位", "清楚", "清晰", "感谢", "谢谢", "辛苦",
    "支持", "期待", "加油", "学到了", "涨知识", "牛", "牛逼", "聪明",
    "智慧", "有趣", "有意思", "幽默", "推荐", "入手", "回购", "真香",
    "无敌", "顶级", "高级", "精致", "进步", "突破", "创新", "效率",
    "省心", "省事", "靠谱", "爽", "开心", "高兴", "幸福", "开眼界",
    "受教", "膜拜", "yyds", "可以的", "太可了", "确实不错", "真不错",
    "给力", "太给力了", "良心", "细致", "贴心",
}

NEGATIVE = {
    "很差", "垃圾", "讨厌", "失望", "难看", "难用", "难吃", "难受",
    "丑", "糟糕", "败笔", "翻车", "踩雷", "避雷", "后悔", "智商税",
    "割韭菜", "骗人", "骗子", "坑人", "套路", "虚假", "造假", "假的",
    "无聊", "尴尬", "恶心", "反感", "受不了", "无语", "离谱", "扯淡",
    "太贵", "昂贵", "浪费", "没用", "无效", "鸡肋", "广告", "推销",
    "硬广", "带货", "恰饭", "水军", "刷的", "封号", "限流", "违规",
    "侵权", "抄袭", "盗图", "搬运", "看不懂", "听不懂", "迷惑", "费解",
    "危险", "风险", "担心", "害怕", "不敢", "劝退", "慎入",
    "恐怖", "下降", "退步", "变差", "变味", "过度", "泛滥", "审美疲劳",
    "麻了", "绷不住", "裂开", "一般般", "没什么用", "不至于", "劝退",
    # 口语化否定式（"假""贵"单字风险大，改收短语）
    "有点假", "太假", "很假", "是假的", "好假", "太贵了", "好贵", "贵死",
    "有点丑", "不太行", "不行", "不咋地", "没意思", "缺点",
}

# ⚠️ 刻意排除的高频"主题词"，它们在本语料里是**话题本身**而非情绪词，
# 加进来会造成大量误判（都是实测踩过的坑）：
#   问题   —— "解决问题""没问题" 被误判为负面
#   取代/替代/失业/退化/降智 —— AIGC 话题的讨论对象，"AI 会取代设计师吗"是中性问题
#   就这   —— 作为子串命中"手机就这么…"，严重误伤
# 若换成其它领域语料，这些词可能应该加回来。

NEGATIONS = {"不", "没", "没有", "无", "别", "非", "未", "毫无", "不是",
             "不要", "不能", "不会", "并不", "从不", "从未", "莫", "勿",
             "难", "别", "不咋", "不太", "不怎么"}

INTENSIFIERS = {"很", "太", "超", "非常", "特别", "真", "巨", "贼", "极",
                "最", "十分", "超级", "尤其", "格外", "相当", "死", "爆",
                "炸", "真的", "一整个", "也太"}

# 弱化词：命中则强度打折
MITIGATORS = {"有点", "稍微", "略", "还算", "勉强", "大概", "可能", "似乎"}

# 网络用语 / 表情符号（正则，独立加权）：(正则, 极性, 权重, 展示名)
NET_RULES: List[Tuple[re.Pattern, int, float, str]] = [
    (re.compile(r"哈哈+|hhh+|嘿嘿|嘻嘻|笑死|笑不活|xswl|好家伙|绝绝子|yyds"),
     +1, 1.2, "笑声/赞语"),
    (re.compile(r"\[(赞|爱心|玫瑰|鼓掌|强|给力|太开心|微笑|比心)\]"),
     +1, 1.0, "正面表情"),
    (re.compile(r"谢谢|感谢|辛苦|学到了|收藏了|码住"), +1, 0.8, "致谢"),
    (re.compile(r"\[(哭|流泪|裂开|难过|发怒|吐|无语|汗)\]"), -1, 1.0, "负面表情"),
    # 短语气词必须卡边界：否则 "限额" 里的"额"、"融化了"里的"化了"会误命中
    (re.compile(r"(?<![\u4e00-\u9fff])呵呵(?![\u4e00-\u9fff])|emmm+|"
                r"(?<![\u4e00-\u9fff])额+(?![\u4e00-\u9fff])|"
                r"(?<![\u4e00-\u9fff])啊这|麻了|绷不住"), -1, 0.7, "无奈/质疑"),
]

# 预编译词表：长词优先匹配，避免"好"覆盖"好用"
_POS_SORTED = sorted(POSITIVE, key=len, reverse=True)
_NEG_SORTED = sorted(NEGATIVE, key=len, reverse=True)
_ALL_WORDS = sorted(POSITIVE | NEGATIVE, key=len, reverse=True)
_POLARITY: Dict[str, int] = {w: 1 for w in POSITIVE}
_POLARITY.update({w: -1 for w in NEGATIVE})

_CTX_BACK = 5   # 向前回看的字符数

# 上下文里的"假否定"片段：这些字面含否定字（不/非/无），但整词并不否定。
# 检测否定前必须先把它们挖掉，否则 "非常不错" 会被 "非" 翻成负面。
_FALSE_NEG = {"无比", "无论", "不但", "不仅", "不过", "不少", "不断",
              "不停", "不同", "不像样", "不由自主"}


def _mask(s: str, words) -> str:
    """把 s 中命中 words 的片段替换成 \\x00，避免跨词子串误匹配。"""
    out = list(s)
    for w in words:
        start = 0
        while True:
            k = s.find(w, start)
            if k < 0:
                break
            for x in range(k, k + len(w)):
                out[x] = "\x00"
            start = k + 1
    return "".join(out)

# 快速否定：词表里出现过的所有字符。若文本与它无交集，可直接判定为中性，
# 省掉上百次 str.find —— 65k 条评论下能把耗时砍掉一个数量级。
_LEX_CHARS = {ch for w in _ALL_WORDS for ch in w} | \
             {ch for w in NEGATIONS | INTENSIFIERS | MITIGATORS for ch in w} | \
             {"哈", "h", "嘻", "嘿", "[", "谢", "赞"}


def _find_hits(text: str) -> List[Tuple[int, int, str, int]]:
    """扫描词表，返回 [(start, end, word, polarity)]，长词优先，区间不重叠。"""
    taken = [False] * len(text)
    hits: List[Tuple[int, int, str, int]] = []
    for w in _ALL_WORDS:
        start = 0
        while True:
            i = text.find(w, start)
            if i < 0:
                break
            j = i + len(w)
            if not any(taken[i:j]):
                for k in range(i, j):
                    taken[k] = True
                hits.append((i, j, w, _POLARITY[w]))
            start = i + 1
    hits.sort()
    return hits


_Q_TAIL = re.compile(r"[吗呢？?]\s*$")
_Q_HEAD = re.compile(r"^(到底|是不是|有没有|怎么|为什么|如何|什么|哪|多少|几个|能否)")


def _is_question(t: str) -> bool:
    """疑问句识别。

    疑问句通常**不表达说话人自己的褒贬**（"好用吗"不等于"好用"），
    这类句子一律往中性拉，能显著减少误判。
    连结尾 2 字内的"吗/呢"也算，用来兜住"好看吗书"这种倒装语序。
    """
    return bool(_Q_TAIL.search(t) or _Q_HEAD.match(t) or re.search(r"[吗呢]", t[-3:]))


def score_text(text: str) -> Tuple[float, str, List[str]]:
    """返回 (得分, 标签, 命中词)。"""
    if not text or not text.strip():
        return 0.0, "neutral", []

    t = re.sub(r"\s+", " ", text.strip())
    if not (set(t) & _LEX_CHARS):        # 快速路径：不可能命中任何情感词
        return 0.0, "neutral", []

    score = 0.0
    hits: List[str] = []

    # --- 网络用语 / 表情 ---
    for pat, pol, w, name in NET_RULES:
        n = len(pat.findall(t))
        if n:
            score += pol * w * min(n, 3)
            hits.append(name)

    # --- 词典命中 + 位置上下文 ---
    prev_end = 0
    for i, j, word, pol in _find_hits(t):
        # ⚠️ 回看窗口不能跨进上一个情感词内部。
        # 反例："非常不错很实用的工具"——"实用"若回看到"不错"里的"不"，
        # 会被误判成否定（→ 负面）。所以窗口左边界取 max(5字, 上一个命中词结尾)。
        ctx = t[max(0, i - _CTX_BACK, prev_end):i]
        mult = 1.0
        # 先挖掉程度词与假否定片段，再判断否定——否则 "非"（来自"非常"）会误翻极性
        masked_neg = _mask(ctx, INTENSIFIERS | MITIGATORS | _FALSE_NEG)
        if any(neg in masked_neg for neg in NEGATIONS):
            mult *= -1.0        # "不好用" → 翻转
        masked_int = _mask(ctx, NEGATIONS | MITIGATORS)
        if any(inten in masked_int for inten in INTENSIFIERS):
            mult *= 1.6         # "非常好用" → 加强
        if any(mit in ctx for mit in MITIGATORS):
            mult *= 0.6         # "有点好用" → 减弱
        score += pol * mult
        hits.append(word)
        prev_end = j

    # 疑问句收敛：除非情绪非常强烈（|score| ≥ 2，如"这也太好用了吧？"），否则判中性
    if _is_question(t) and abs(score) < 2.0:
        return round(score, 3), "neutral", hits

    label = "positive" if score > 0.15 else ("negative" if score < -0.15 else "neutral")
    return round(score, 3), label, hits


def classify(content: str) -> str:
    return score_text(content)[1]


def label_cn(label: str) -> str:
    return {"positive": "正面", "negative": "负面", "neutral": "中性"}[label]


if __name__ == "__main__":
    cases = [
        "这个也太好用了吧，真的绝了，必须要入手",
        "不好用，纯纯智商税，别买了浪费钱",
        "哈哈哈哈笑死我了，评论区太有意思了",
        "所以这个东西到底能不能替代设计师？",
        "画得挺好看的，但是感觉有点假",
        "我们公司已经在用了，效率提升明显，但数据安全还是让人担心",
        "还行吧，一般般，没什么惊艳的",
        "这也太智能了吧，简直是生产力神器[赞]",
    ]
    for c in cases:
        s, lab, hits = score_text(c)
        print(f"{label_cn(lab):<3} score={s:>6} hits={hits}\n     {c}")
