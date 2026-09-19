# Operant 白底深绿图标

2026-09-19，Codex 按 User 最终选择落地白底深绿版本，取代此前绿底方案。

- 白底 `#FFFFFF`，标志深绿 `#123D2C`；圆环与右上角楔形同色。
- 保留开口 O 轮廓，去掉渐变、薄荷色点缀与材质效果；生成稿转为可缩放 SVG。
- 单一图形源：`clients/gui/public/app-icon.svg`，favicon 使用相同几何；桌面 PNG/ICNS 由 Tauri CLI 从 SVG 生成。
- 网页与 PWA 继续使用原资源地址；Service Worker 缓存版本升级为 v4，使新图标随新版壳缓存安装。
- 桌面打包显式指定 PNG 与 ICNS；已安装应用需要重新构建后安装才能换图标。

本次独立分支基于 B2-7 `f88df735781365ce662b43b684a119c105fb1342`，治理来自主目录 `AGENTS.md`（workflow-20260915.1）。只改图标资源、缓存版本与图标打包声明，不改变 Core、协议、模型或业务行为。

验收范围：图标视觉回读，GUI 构建及现有客户端测试，PNG/ICNS 格式与尺寸，diff 检查。无需重跑后端或真实模型链路。本次不代表已安装桌面应用的实机验收。

实际结果：GUI `npm run build`（含 TypeScript 与 bundle 预算检查）通过，`npm test` 121 项通过；512px 与 32px PNG 已视觉回读，PNG 带透明边缘；macOS `iconutil` 成功解析 ICNS；favicon 与主 SVG 字节一致，`git diff --check` 通过。未重构建或安装桌面应用，未合并、推送或发布。
