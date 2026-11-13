# panel3d —— 3D 角色渲染器（babylon-mmd）

这个目录是**独立的小型构建项目**，只负责把 MMD 模型渲染出来，
产物是一个文件：`panel/js/mmd.bundle.js`，由控制台面板直接 `<script>` 加载。

## 为什么需要构建

babylon-mmd 官方只提供 npm 包（上千个 ESM 小文件），
浏览器原生 `import` 无法解析它内部的路径，必须打包成单文件。

## 一次性构建

```powershell
cd D:\AI\panel3d
npm install
npm run build
```

成功后会生成 `D:\AI\panel\js\mmd.bundle.js`（约 3 MB）。

之后**重启桌宠**，打开控制台对话页即可。

## 改 3D 代码后

```powershell
cd D:\AI\panel3d
npm run build
```

然后重启桌宠（或者关掉控制台再打开）。

想边改边看：

```powershell
npm run dev      # 监听 src/ 变化，自动重新构建
```

## 回退

不想用 babylon-mmd 时，**删掉 `panel/js/mmd.bundle.js`** 即可——
面板会自动退回 three.js 版本（`panel/js/model3d.js`），画面照常。

## 调参

`src/main.js` 顶部：

```js
const VIEW = {
  alpha: -Math.PI / 2,   // 镜头角度；看到后脑勺就改成 Math.PI / 2
  bodySpan: 0.62,        // 越小＝越大（越近）
  lookAtRatio: 0.62,     // 0 脚底 / 1 头顶
  offsetY: 0.04,         // 正数＝人往下
};
```

## 动作（VMD）

目前没有动作文件，待机是程序算的（摆头 + 呼吸 + 眨眼）。

要加载 MMD 动作，把 `.vmd` 放到 `assets/models3d/motions/`，
然后在 `src/main.js` 里用 `VmdLoader` 载入并绑定到模型上：

```js
import { VmdLoader } from "babylon-mmd/esm/Loader/vmdLoader";
const vmdLoader = new VmdLoader(scene);
const vmd = await vmdLoader.loadAsync("motion", "/assets/models3d/motions/idle.vmd");
// 然后 mmdRuntime 播放这个动画
```

> 注意：VMD 只能驱动**同骨架**的模型。不同作者的模型骨骼名不一致时，
> 会出现手臂错位或不动，需要重新绑定或使用「モーション移植」工具。

### 去哪找动作

| 站点 | 内容 |
|---|---|
| [ニコニ・コモンズ](https://commons.nicovideo.jp/) | 搜索「MMD モーション」，多数免费可商用需署名 |
| [BowlRoll](https://bowlroll.net/) | MMD 素材大本营，动作/相机/模型都有 |
| [MMD-BOX](https://mmdbox.web.fc2.com/) | 老牌素材站 |
| [萌娘百科 MMD 素材](https://zh.moegirl.org.cn/) | 中文导航 |

搜索关键词：`待機モーション`（待机）、`トークモーション`（说话）、
`アイドリング`、`ループ モーション`。

**下载后请先看作者的使用规约**（是否允许商用、是否需要署名、是否禁止再配布）。
