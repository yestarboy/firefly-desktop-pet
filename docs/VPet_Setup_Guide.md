# VPet Live2D 桌宠设置指南

## 方案一：使用 VPet（推荐）

### 1. 下载 VPet
访问：https://github.com/LorisYounger/VPet/releases
下载最新版本的 `VPet.zip`

### 2. 解压并运行
解压后运行 `VPet.exe`

### 3. 导入 Firefly 模型
VPet 支持 Live2D Cubism 模型，但需要特定的文件结构。

**问题**：VPet 主要支持自己的模型格式，对标准 Live2D 模型的支持有限。

---

## 方案二：使用 Wallpaper Engine（最简单）

### 1. 购买 Wallpaper Engine
Steam 商店：约 ¥18
https://store.steampowered.com/app/431960/Wallpaper_Engine/

### 2. 导入 Live2D 模型
- 打开 Wallpaper Engine
- 创建新壁纸 → 选择 Live2D
- 导入你的 Firefly.model3.json
- 设置为桌面宠物模式

**优点**：
- 完美支持 Live2D Cubism 4.0
- 性能优秀
- 可以调整大小、位置
- 支持鼠标交互

---

## 方案三：使用 DesktopSprite（免费）

### 1. 下载 DesktopSprite
GitHub: https://github.com/Hawkbat/DesktopSprite

### 2. 配置
将 Firefly-desktop 文件夹放入 DesktopSprite 的 models 目录

---

## 方案四：使用 Live2DViewerEX（推荐，免费）

### 1. 下载 Live2DViewerEX
Steam 免费下载：
https://store.steampowered.com/app/616720/Live2DViewerEX/

### 2. 导入模型
- 打开 Live2DViewerEX
- 点击"导入模型"
- 选择 Firefly.model3.json
- 设置为桌面模式

**优点**：
- 完全免费
- 完美支持 Live2D Cubism 3/4
- 支持桌面宠物模式
- 可以调整大小、透明度
- 支持鼠标交互和动作触发

---

## 推荐方案

**最推荐：Live2DViewerEX**
- 免费
- Steam 平台，安装方便
- 完美支持你的 Firefly 模型
- 功能完整

**次推荐：Wallpaper Engine**
- 需要付费（¥18）
- 功能最强大
- 性能最好

---

## 如果坚持用 Python

可以使用更轻量的方案：

### 使用 pygame + Live2D SDK
但这需要：
1. 编译 Live2D Cubism SDK for Native
2. 使用 Python ctypes 调用
3. 实现渲染循环

**复杂度很高，不推荐。**

---

## 总结

对于你的需求（Firefly Live2D 桌宠），我强烈推荐：

1. **Live2DViewerEX**（Steam 免费）- 最简单最快
2. **Wallpaper Engine**（Steam ¥18）- 功能最强
3. 如果必须用 Python，当前的 PyQt6 方案已经是最优解了

你想选择哪个方案？
