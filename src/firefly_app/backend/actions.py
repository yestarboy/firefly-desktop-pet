# -*- coding: utf-8 -*-
"""
把 LLM 回复里的（动作名）解析成动作事件。
只认词表里存在的名字，其它括号内容原样保留。
"""

# 只收“模型里真的有文件”且“说话时触发也安全”的动作。
# 注意：Firefly-desktop 里 model3.json 登记了 11 个动作 / 10 个表情，
# 但实际只有 Scene0/4/22/26710/02 和 expression1/2/3 存在，
# 其余（叉腰开心、拍手、抱胸、问号、眼泪…）都是空登记，调用了也播不出来。
MOTION_VOCAB = {
    # 动作组（有 motion 文件、无音效）
    "思考",
    # 表情组（纯 Expression，无音效）
    "墨镜", "裂开",
    # 有动作文件、但自带喵叫音效（会和 TTS 短暂叠音，介意就删掉）
    "猫耳",
    # 特殊：恢复默认表情（摘掉墨镜等）。前端单独处理，不属于任何动作组
    "恢复",
}

_OPEN = "（("
_CLOSE = "）)"


class ActionFilter:
    """流式解析器：喂进增量文本，吐出（可显示文本, [动作名]）"""

    def __init__(self, vocab=None, max_len=8):
        self.vocab = set(vocab or MOTION_VOCAB)
        self.max_len = max_len
        self._buf = ""
        self._in_tag = False

    def feed(self, chunk: str):
        out, motions = [], []
        for ch in chunk:
            if self._in_tag:
                if ch in _CLOSE:
                    name = self._buf.strip()
                    if name in self.vocab:
                        motions.append(name)
                    else:
                        out.append("（" + self._buf + ch)
                    self._buf, self._in_tag = "", False
                else:
                    self._buf += ch
                    if len(self._buf) > self.max_len:
                        out.append("（" + self._buf)
                        self._buf, self._in_tag = "", False
            elif ch in _OPEN:
                self._in_tag = True
                self._buf = ""
            else:
                out.append(ch)
        return "".join(out), motions

    def flush(self) -> str:
        """流结束时调用：把没闭合的（ 吐出来，别吞字"""
        if self._in_tag:
            rest = "（" + self._buf
            self._buf, self._in_tag = "", False
            return rest
        return ""