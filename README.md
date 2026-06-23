# 人体体态数据采集系统

基于奥比中光Gemini 336L深度相机的自动化人体体态数据采集系统。

## 功能特性

- 距离检测：实时检测采集者与相机的距离（目标1米）
- 多模态数据采集：RGB彩色图、深度图、3D点云（PLY格式）
- 语音控制：支持"开始采集"、"停止采集"等语音指令
- 语音播报：操作引导和状态反馈的语音提示
- 实时预览：GUI中显示彩色和深度画面
- 小米风格界面：简约、大气、科技感的用户界面

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
cd frontend && npm install
```

### 2. 下载语音模型

下载 `vosk-model-small-cn-0.22` 到 `models/` 目录。

### 3. 一键启动

```bash
go.bat
```

## 使用方法

### 启动脚本

| 脚本 | 功能 |
|------|------|
| `go.bat` | 一键启动前后端（推荐） |
| `run.bat` | 仅启动后端 |
| `run_frontend.bat` | 仅启动前端 |

### 访问地址

- 后端: ws://localhost:8765
- 前端: http://localhost:3000

### 语音指令

| 指令 | 动作 |
|------|------|
| "开始采集" | 开始数据采集 |
| "停止" | 停止当前采集 |
| "下一个" | 准备下一次采集 |
| "完成" | 结束采集会话 |

## 数据存储

数据存储在 `data/sessions/` 目录下：

- RGB图像: PNG格式
- 深度数据: NPZ格式
- 点云数据: PLY格式

## 故障排除

- 端口占用: `taskkill /F /IM python.exe`
- 前端错误: `cd frontend && npm install`

## 许可证

Apache License 2.0
