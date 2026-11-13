# models3d

放 3D 角色模型（MMD / PMX）。**模型文件不入库**（体积大、版权独立）。

## 怎么放

```
assets/models3d/
├─ firefly.pmx                 ← 必须叫这个名字（或改 panel/js/model3d.js 的 MODEL_URL）
├─ tex/                        ← 贴图目录，保持 pmx 里写的相对路径
│  ├─ body.png
│  └─ ...
└─ toon/                       ← 有的模型带共享贴图
```

把整个模型包解压到这里，**保持原来的目录层级**，PMX 里记录的是相对路径，
少一层贴图就会变成纯白或黑块。

## 首次运行会做什么

1. `panel/js/model3d.js` 用 three.js 的 MMDLoader 读取 PMX
2. 自动按包围盒取景：**镜头对准胸口，画面只露出上半身到裙摆**
3. 每帧叠加程序化待机动作：
   - 呼吸（上半身轻微侧摆）
   - 摆头（头部正弦摆动）
   - 眨眼（`まばたき` morph）
4. 说话时用 TTS 的 `tts_start` / `tts_end` 事件驱动 `あ` morph 做口型

## 调参

构图不对就改 `panel/js/model3d.js` 顶部：

```js
const CAMERA = {
  fov: 28,
  bodySpan: 0.55,      // 越大 = 拉远（露出更多身体）
  lookAtRatio: 0.62,   // 0=脚底 1=头顶，镜头对准的高度
  offsetY: 0.02,       // 整体上下微调
};
```

## 已知限制

- **没有 .vmd 动作文件**，所以没有原版动画，只有程序化待机
- 口型只能用 `あ` 一个音素，看起来是"张嘴-闭嘴"而不是真正的发音
- 模型贴图若缺失，会显示纯白/纯黑
- three.js 从 CDN 加载，**第一次打开需要联网**；想离线请把 three.js 下载到 `assets/live2d-libs/` 并改 importmap

## 版权

PMX 模型通常有独立的使用条款（作者署名、禁止商用、禁止再分发等）。
上传仓库前请确认，或者把本目录加入 `.gitignore`（默认已经忽略 `.pmx`）。
