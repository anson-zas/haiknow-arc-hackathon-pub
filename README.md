# HAiKnow Agent

基于 Codex SDK 的 Web 应用开发 Agent，读取需求 YAML，在指定目录生成应用或对已有项目进行增量修改，供参考实现思路。

## 工作流程

1. 解析需求与依赖，按 ROOT 子模块依次实现。
2. 保留已有代码和数据库；空目录使用通用 React / Express 模板初始化。
3. 执行构建、启动、应用测试及需求覆盖检查，将失败反馈给编码模型修复。
4. 记录运行进度、需求追踪和 Git 检查点。

## 主要目录

- `main.py`、`core/`：入口、需求解析、增量开发与验证流程。
- `agents/`：模型协议适配、视觉请求路由和异常诊断。
- `competition/`、`runtime-policy.yaml`：运行配置和需求检查策略。
- `template/`：应用模板与通用组件。
- `skills/`、`arcbench-agent-runtime/`：进度、需求追踪和检查点工具。
- `HAIKNOW-LITE.md`：编码模型使用的运行指令。
- `vendor/landrun/`：Linux 进程写入隔离工具。

## 运行

需要支持 Landlock 的 Linux（amd64 / arm64）、Python 3.12、Node.js 20.19.3、npm 和可运行的 Playwright / Chromium。

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt

cp .env.example .env
# 编辑 .env，填写 OPENAI_API_KEY、OPENAI_BASE_URL 和 MODEL。
set -a
. ./.env
set +a

python main.py /work/requirements --output-dir /work/output --type web
```

需求目录须包含根节点为 `ROOT` 的 `requirements.yaml`。输入、输出与 Agent 源码使用独立目录；增量开发时，将已有应用放入输出目录。

模型服务需支持对应协议，选择规则见 `competition/coding-transport.json`。默认最多修复 8 轮，请求空闲检测为 600 秒，入口另有 3 小时防卡死保护。参考图解析默认关闭。

运行辅助库与 skills 源自 ARC-Bench 参考实现；Landrun 的许可证见 [vendor/landrun/LICENSE](vendor/landrun/LICENSE)。

项目自有代码采用 [MIT License](LICENSE)。第三方组件保留原有版权与许可声明。
