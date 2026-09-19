#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
中文热词提取（jieba 可用则用，否则自动降级为 n-gram 新词发现）
==============================================================

降级方案原理（无监督新词发现，参考 "新词发现" 经典做法）：
  对 2~4 字候选串，用两个指标筛：
    * 内部凝固度 PMI：log2( p(w) / (p(left)·p(right)) )，越大越像一个词
    * 边界自由度 熵 ：左右邻字越杂，越说明它是个独立单元
  两者同时过阈值才保留，能捞出 "智商税""一键生成" 这类词典里没有的词。
"""

from __future__ import annotations

import collections
import math
from typing import Iterable, List, Tuple

try:  # 可选依赖
    import jieba  # type: ignore
    _HAS_JIEBA = True
except Exception:  # pragma: no cover
    _HAS_JIEBA = False

STOPWORDS = set("""
的 了 是 在 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着
没有 看 好 自己 这 那 他 她 它 我们 你们 他们 这个 那个 什么 怎么
为什么 因为 所以 但是 而且 如果 就是 可以 这样 那样 还是 已经 现在
一样 时候 出来 起来 过来 上去 下去 一下 一些 这些 那些 只是 不过
然后 感觉 觉得 应该 可能 需要 希望 知道 看到 大家 真的 确实 其实
哈哈哈 哈哈 啊 吧 呢 吗 呀 哦 嗯 哎 唉 额 哟 啦 嘛 哈 呀 哇 诶
以及 还有 可是 于是 因此 或者 虽然 即使 无论 反正 本来 原来 居然
竟然 果然 当然 至少 甚至 尤其 特别 非常 十分 比较 稍微 有点 稍微
需要 不用 不能 不会 不要 不是 那么 这么 多少 几个 一些 各种 每个
""".split())

# 单字过滤：中文单字多半没信息量
_CN = lambda s: all("\u4e00" <= c <= "\u9fff" for c in s)  # noqa: E731

# 边界功能字：以这些字打头/收尾的候选，基本都是"你了""啊啊"这类切分残渣。
# 刻意收得很窄——只放语气词、代词、连词。
# 注意不要放 能/会/有/中/太/真/更/要/说/来/去，否则会误杀
# "人工智能""未来""有效""真香""在线"等真词。
_FUNC_EDGE = set("的了是呢吧啊呀哦嗯嘛啦吗呗咯我你他她它们就也都和与或但而之其此该")


def _normalize(texts: Iterable[str]) -> List[str]:
    out = []
    for t in texts:
        t = "".join(ch for ch in (t or "") if "\u4e00" <= ch <= "\u9fff")
        if len(t) >= 2:
            out.append(t)
    return out


# --------------------------------------------------------------------------
# 方案 A：jieba
# --------------------------------------------------------------------------
def extract_with_jieba(texts: Iterable[str], top_k: int = 150):
    counter = collections.Counter()
    for t in texts:
        for tok in jieba.cut(t or ""):
            tok = tok.strip()
            if len(tok) < 2 or tok in STOPWORDS or not _CN(tok):
                continue
            counter[tok] += 1
    return counter.most_common(top_k)


# --------------------------------------------------------------------------
# 方案 B：n-gram 新词发现
# --------------------------------------------------------------------------
def extract_with_ngram(texts: Iterable[str], top_k: int = 150,
                       min_freq: int = 20, min_pmi: float = 2.0,
                       min_entropy: float = 1.0):
    docs = _normalize(texts)

    # 按长度分别统计，保证 PMI 各项分母同量纲（否则单字与多字概率不同尺度，PMI 无意义）
    counts: dict[int, collections.Counter] = {n: collections.Counter() for n in (1, 2, 3, 4)}
    left = collections.defaultdict(collections.Counter)
    right = collections.defaultdict(collections.Counter)

    for d in docs:
        L = len(d)
        for i in range(L):
            counts[1][d[i]] += 1
        for n in (2, 3, 4):
            for i in range(L - n + 1):
                g = d[i:i + n]
                counts[n][g] += 1
                if i > 0:
                    left[g][d[i - 1]] += 1
                if i + n < L:
                    right[g][d[i + n]] += 1

    totals = {n: (sum(c.values()) or 1) for n, c in counts.items()}

    def prob(s: str) -> float:
        n = len(s)
        if n not in counts:
            return 0.0
        return counts[n].get(s, 0) / totals[n]

    def entropy(counter: collections.Counter) -> float:
        tot = sum(counter.values())
        if tot == 0:
            return 0.0
        return -sum((c / tot) * math.log2(c / tot) for c in counter.values())

    scored: List[Tuple[str, float, int]] = []
    for n in (2, 3, 4):
        for g, freq in counts[n].items():
            if freq < min_freq:
                continue
            if g[0] in _FUNC_EDGE or g[-1] in _FUNC_EDGE or g in STOPWORDS:
                continue
            best_pmi = float("inf")
            ok = True
            for cut in range(1, n):
                a, b = g[:cut], g[cut:]
                pa, pb = prob(a), prob(b)
                if pa <= 0 or pb <= 0:
                    ok = False
                    break
                best_pmi = min(best_pmi, math.log2(prob(g) / (pa * pb)))
            if not ok or best_pmi < min_pmi:
                continue
            h = min(entropy(left[g]), entropy(right[g]))
            if h < min_entropy:
                continue
            scored.append((g, h, freq))

    # 打分：频次 × 边界熵；同分下长词优先
    scored.sort(key=lambda x: (-(x[2] * x[1]), -len(x[0])))

    picked: List[Tuple[str, int]] = []
    chosen: List[str] = []
    for g, h, freq in scored:
        # 子串抑制：若已被更长的入选词包含，且频次不占优，则丢弃
        if any(g in s for s in chosen):
            continue
        chosen.append(g)
        picked.append((g, freq))
        if len(picked) >= top_k:
            break
    return picked


def extract(texts: Iterable[str], top_k: int = 150, min_freq: int = 30):
    """统一入口，返回 [(词, 词频)] 与所用方案名。"""
    texts = list(texts)
    if _HAS_JIEBA:
        return extract_with_jieba(texts, top_k), "jieba"
    return extract_with_ngram(texts, top_k, min_freq=min_freq), "ngram-pmi"


if __name__ == "__main__":
    sample = [
        "这个AI绘画太好用了，一键生成海报，简直是设计师的噩梦",
        "人工智能生成内容真的会取代设计师吗，感觉有点焦虑",
        "文生视频越来越强了，一键生成视频，效率提升太多",
        "大模型写的文案其实一般般，还是有股AI味",
        "AI生成的内容以假乱真，以后版权怎么算呢",
    ] * 20
    words, method = extract(sample, top_k=15, min_freq=5)
    print("方案:", method)
    for w, c in words:
        print(f"  {w:<10} {c}")
