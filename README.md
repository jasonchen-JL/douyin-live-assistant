# 抖音直播助手

面向单人本机使用的评论采集与 AI 提词工作台：Python / FastAPI + React + SQLite。

## 功能

- Playwright CLI 读取直播网页评论，按平台消息标识去重并保存在本机。
- 按观众汇总已采历史，评论先展示、Markdown 提词异步补充。
- 手动重新分析、历史结果保留、在途新增评论处理。
- 模型账号池、限额及冷却，Key 保存到系统凭据库。
- 小知识文件快照直接注入，不使用向量数据库。

## Windows 快速开始

安装 Python 3.11+、Node.js、Edge 和 Bun。在项目根目录执行：

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
npm install -g @playwright/cli
cd web
bun install
bun run build
cd ..
```

先双击 `启动模拟演示.cmd`，打开本机 `http://127.0.0.1:8768`。演示使用合成数据和模拟模型，不连接真实直播间。

正式使用双击 `启动直播助手.cmd`，打开 `http://127.0.0.1:8765`。在设置中填写你有权使用的直播间地址、模型Base URL、Key与模型名；默认房间号是占位示例，默认API地址不可用。通过界面开始监听，按需要完成网页登录。模型调用可能产生费用。备份与迁移时自行保护本机数据库和系统凭据。

知识文件为 `docs/knowledge/company.md`，公开版仅含通用示例；更新知识不自动重跑历史任务。

## 架构与边界

浏览器评论 → 采集SQLite → 分析SQLite/后台任务 → 模型服务 → React工作台。网络请求与SQLite写事务分离，服务默认绑定127.0.0.1。

网页采集不是官方开放API，平台改版、断网和页面重载可能导致漏采。仅在获得授权并遵守平台规则的场景使用。本版本不提供语音转写、自动发言、多人权限或云部署。旧粗筛与评分代码保留，但默认流程不启用。

## 公开版本说明

这是从私有开发仓库导出的独立代码快照，不包含原提交历史、工作日志、业务知识、真实评论库、截图、浏览器配置资料或凭据。请勿将runtime、Cookie、Key或真实用户数据提交到Git。

## 本机验证

```powershell
.venv/Scripts/python -m pytest tests/test_workbench.py tests/test_key_pool.py tests/test_credentials.py
```

采用 MIT 许可；第三方依赖遵循各自许可证。
