# Firefly 桌面宠物

一个跑在 Windows 上的 Live2D 桌宠：**流式对话 + 语音合成 + 语音输入 + 长期记忆**，
全部由一个本地控制台可视化管理。

![status](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)
![python](https://img.shields.io/badge/python-3.10%2B-blue)

---

## 特性

| 模块 | 说明 |
|---|---|
| **对话** | 线上 LLM（DeepSeek，OpenAI 兼容）或本地 Ollama，SSE 流式输出，首个字约 0.9s |
| **语音合成** | 接入 GPT-SoVITS，可换成任意音色；逐句流水线合成，说完一句就开口 |
| **语音输入** | 全局热键「按住说话」，或全程麦点一下录一句；funasr paraformer 识别，含热词与纠错 |
| **动作表情** | LLM 用 `（动作名）` 驱动 Live2D 动作，只播放模型里真实存在的动作 |
| **长期记忆** | SQLite 存储，按出现次数分级留存（1 天 / 7 天 / 30 天），自动去重与过期 |
| **控制台** | 对话 / 设置 / 日志三个页签，改配置、换音色、看实时日志都不用改代码 |

---

## 目录结构

```
Firefly-Desktop-Pet/
├─ live2d_pet.py            入口：窗口、托盘、QWebChannel 桥
├─ live2d_pet.html          桌宠页面（Live2D + 悬浮球 + 气泡）
├─ launch.py                静默启动器（TTS / STT / 桌宠）
├─ start_all.vbs            一键启动（无终端窗口）
├─ install.bat              依赖安装
├─ requirements.txt
├─ firefly_config.example.json   配置模板（复制成 firefly_config.json 使用）
│
├─ src/firefly_app/
│  ├─ paths.py              所有路径的唯一来源
│  ├─ backend/
│  │  ├─ llm.py             模型接入（DeepSeek / Ollama，流式）
│  │  ├─ voice.py           TTS：合成队列、逐句播放、参考音色
│  │  ├─ memory.py          长期记忆（SQLite）
│  │  ├─ actions.py         （动作名）解析器
│  │  ├─ sentences.py       流式逐句切分
│  │  ├─ hotkey.py          全局键盘钩子（按住说话）
│  │  └─ ui_bus.py          事件广播（桌宠窗口 + 控制台面板）
│  └─ frontend/
│     ├─ live2d_server.py   本地静态服务器
│     └─ settings_window.py 控制台窗口 + Web 桥
│
├─ panel/                   控制台前端（纯 HTML/CSS/JS，无构建）
│  ├─ index.html
│  ├─ css/                  base / layout / settings / chat / logs
│  └─ js/                   util / tabs / sidenav / settings / chat / logs / main
│
├─ tools/
│  ├─ stt_server.py         语音识别服务（由 GPT-SoVITS 运行时启动）
│  ├─ memory_cli.py         记忆库命令行
│  ├─ tts_probe.py          TTS 参数 A/B 体检
│  └─ hotkey_test.py        全局热键自检
│
├─ assets/
│  ├─ models/Firefly-desktop/   Live2D 模型
│  ├─ live2d-libs/              pixi / live2d 运行时
│  └─ icons/                    托盘与窗口图标
│
├─ data/
│  ├─ voices/*.wav          参考音色（文件名 = 台词）
│  └─ memory.db             记忆库（自动生成，不入库）
└─ logs/                    运行日志（自动生成，不入库）
```

---

## 快速开始

```powershell
# 1. 安装依赖（会自动建 .venv）
install.bat

# 2. 复制配置并填入你的 API Key
copy firefly_config.example.json firefly_config.json

# 3. 启动
start_all.vbs
```

首次启动会弹出托盘图标，右键可打开控制台。
详细用法见 **[USAGE.md](USAGE.md)**。

---

## 可选组件

桌宠本体可以独立运行，此时：

- TTS 降级为 Windows 系统语音（`lightweight_tts.py`）
- 语音输入不可用

想要完整体验，需要额外安装：

### GPT-SoVITS（语音合成，必需才有角色音色）

下载整合包，解压到：

```
v4/GPT-SoVITS-v4-20250422fix/GPT-SoVITS-v4-20250422-nvidia50/
```

该目录下应有 `runtime/python.exe` 和 `api_v2.py`。
`launch.py` 会自动检测，不存在就跳过。

### funasr（语音识别）

`tools/stt_server.py` 需要 `funasr + sounddevice + numpy`，建议直接复用
GPT-SoVITS 自带的 runtime（它通常已经装好）。启动器默认就用它跑。

模型首次使用会从 ModelScope 下载（约 1 GB），之后离线可用。

---

## 二次开发

### 改配置项

1. `src/firefly_app/backend/llm.py` 的 `DEFAULTS` 加字段
2. `src/firefly_app/frontend/settings_window.py` 的 `EDITABLE_KEYS` 加键
3. `panel/index.html` 加控件，`panel/js/settings.js` 的 `FIELDS` 和 `collect()` 同步

保存后立即生效，不用重启。

### 加一个控制台页面

1. `panel/index.html` 的 `<nav>` 加 `<button class="tab" data-tab="xxx">`
2. 加 `<section class="tab-page" id="page-xxx" hidden>`
3. 新建 `panel/js/xxx.js`（照 `chat.js` 的 IIFE 模块骨架）
4. `panel/js/main.js` 里 `XxxPanel.bind(bridge)`，并在 `__onTabChange` 里启停轮询

### 加一个后端能力

在 `src/firefly_app/backend/` 新建模块，然后在
`src/firefly_app/frontend/settings_window.py` 的 `_Bridge` 上加 `@pyqtSlot`。

跨线程刷新界面一律走 `ui_bus.broadcast()`，不要直接碰 GUI 对象。

### 加一个动作

1. 确认 `assets/models/Firefly-desktop/` 里**真的有**对应的 `.motion3.json`
2. 加进 `src/firefly_app/backend/actions.py` 的 `MOTION_VOCAB`
3. 同步 `firefly_config.json` 的 `system_prompt` 里的动作列表

> 模型 JSON 里登记的动作不一定有文件，加之前先核对目录。

---

## 已知限制

- 仅支持 Windows（用到了 Win32 API：全局键盘钩子、AppUserModelID、winsound）
- Live2D 模型包需自备，且动作文件可能不全
- 语音识别为中文普通话模型，不带标点

---

## 致谢

这个项目站在很多人的肩膀上：GPT-SoVITS、pixi-live2d-display、FunASR，以及
提供模型与语音素材的创作者。完整名单与授权说明见 **[CREDITS.md](CREDITS.md)**。

> 如果你要公开分发这个仓库，请先阅读 CREDITS.md 里的素材授权说明。

---

## 许可

代码部分可自由修改使用。Live2D 模型、语音素材的版权归各自权利人所有，
请勿随本仓库分发。
