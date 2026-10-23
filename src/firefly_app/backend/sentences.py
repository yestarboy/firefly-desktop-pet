# -*- coding: utf-8 -*-
"""
流式逐句切分器。

LLM 的回复是一段段吐出来的，我们希望在“一句话说完”时就立刻送去合成，
而不是等整段结束。难点：
- 一句话可能被拆成多个 chunk 到达
- 过长的句子（没有标点）需要主动切断，否则单次合成会很慢
- 太短的碎片（“嗯。”“好的。”）单独合成质量差，要并到下一句
"""

# 硬断句：句末标点
_END = "。！？!?…；;\n"
# 软断句：句子太长时在这些地方切
_SOFT = "，,、：:"


class SentenceSplitter:
    def __init__(self, min_len: int = 4, max_len: int = 45):
        self.buf = ""
        self.min_len = min_len
        self.max_len = max_len

    def feed(self, chunk: str):
        """喂入增量文本，返回本次可以送去合成的完整句子列表"""
        if chunk:
            self.buf += chunk

        out = []
        while True:
            idx = self._find_cut()
            if idx < 0:
                break
            sent = self.buf[: idx + 1].strip()
            self.buf = self.buf[idx + 1:]
            if sent and not self._is_punct_only(sent):
                out.append(sent)
        return out

    def flush(self):
        """流结束时调用，把剩下没标点的半句也交出去"""
        sent = self.buf.strip()
        self.buf = ""
        if sent and not self._is_punct_only(sent):
            return [sent]
        return []

    # ---- 内部 ----

    def _find_cut(self) -> int:
        # 1) 优先在句末标点处切（至少攒够 min_len 个字符，避免“嗯。”这种碎片）
        for i, ch in enumerate(self.buf):
            if ch in _END and i + 1 >= self.min_len:
                return i
        # 2) 攒太长了还没句号：退而求其次在逗号处切
        if len(self.buf) > self.max_len:
            for i in range(min(len(self.buf) - 1, self.max_len), self.min_len, -1):
                if self.buf[i] in _SOFT:
                    return i
            return self.max_len - 1   # 连逗号都没有，硬切
        return -1

    @staticmethod
    def _is_punct_only(s: str) -> bool:
        return all(ch in "。！？!?…；;，,、：: \t\n" for ch in s)
