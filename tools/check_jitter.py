# -*- coding: utf-8 -*-
"""比对连拍图，判断模型局部（尤其是胸口）是否在快速抖动

思路：抽搐 = 相邻帧之间局部像素差异很大；正常呼吸/摆动幅度小、变化慢。
取胸口中部一小块区域，算相邻帧的平均绝对差（0-255）。
"""
import sys
from pathlib import Path

try:
    from PIL import Image
    import numpy as np
except ImportError:
    print("需要 Pillow / numpy")
    sys.exit(1)

logs = Path(r"D:\AI\logs")
files = sorted(logs.glob("burst-*.png"))
if len(files) < 3:
    print("连拍图不足:", len(files))
    sys.exit(1)

imgs = [np.asarray(Image.open(f).convert("RGB"), dtype=np.int16) for f in files]
h, w, _ = imgs[0].shape
print(f"帧数 {len(files)}  尺寸 {w}x{h}")

# 三个区域对比：脸（不该抖）/ 胸口 / 头发（本来就该动）
face_box = (int(w * 0.46), int(h * 0.24), int(w * 0.54), int(h * 0.32))
chest_box = (int(w * 0.46), int(h * 0.46), int(w * 0.54), int(h * 0.56))
hair_box = (int(w * 0.33), int(h * 0.30), int(w * 0.40), int(h * 0.55))

face_diffs = []
chest_diffs = []
hair_diffs = []
whole_diffs = []
for a, b in zip(imgs, imgs[1:]):
    d = np.abs(a - b)

    def box(bb):
        return float(d[bb[1]:bb[3], bb[0]:bb[2]].mean())

    face_diffs.append(box(face_box))
    chest_diffs.append(box(chest_box))
    hair_diffs.append(box(hair_box))
    whole_diffs.append(float(d.mean()))

print("脸   相邻帧平均差: " + ", ".join(f"{v:.2f}" for v in face_diffs))
print("胸口 相邻帧平均差: " + ", ".join(f"{v:.2f}" for v in chest_diffs))
print("头发 相邻帧平均差: " + ", ".join(f"{v:.2f}" for v in hair_diffs))
print("整幅 相邻帧平均差: " + ", ".join(f"{v:.2f}" for v in whole_diffs))

c = float(np.mean(chest_diffs))
f = float(np.mean(face_diffs))
print(f"\n脸={f:.2f}  胸口={c:.2f}")
if c < 1.5:
    print("结论：胸口稳定，没有抽搐")
elif c > f * 3:
    print("结论：胸口明显比脸动得多，仍有抽搐")
else:
    print("结论：胸口变化与头发/衣服飘动相当，属于正常动态")
