# 测试启动与环境边界

## 统一入口

在仓库根目录执行：

```bash
./scripts/test.sh -q
```

脚本只补充源码工作树需要的三个路径：仓库根目录、`packages/autocut-core` 和
`packages/autocut-kernel/src`，不会修改安装环境，也不会把旧 Agent 状态图注入
`autocut_kernel`。

## Authority 契约测试

`tests/contracts/test_f1_authority_amendment_core.py` 校验的是独立的
`ac_auto_cut` 权威仓库，不是当前仓库的普通依赖。未提供仓库路径时，这组测试会
明确跳过，而不是伪造一个 authority 结果；需要执行时显式设置：

```bash
AUTOCUT_AUTHORITY_REPOSITORY=/absolute/path/to/ac_auto_cut \
  ./scripts/test.sh tests/contracts/test_f1_authority_amendment_core.py -q
```

该目录必须是 Git 仓库，并且其提交、契约版本和 authority lock 相匹配。路径、提交
或契约不匹配时应修复环境/锁定关系，不要通过修改测试绕过。

## 失败解释

- `autocut_core.agent`：旧导入路径。当前 Agent Native 状态图归属
  `auto_cut_bot.agent.state_graph`，应使用当前包内相对导入。
- `tools`：`tests/tools` 与根目录 `tools` 同名。测试包扩展自身搜索路径，入口脚本
  同时显式加入仓库根目录，保证工具模块解析稳定。
- 缺少 `AUTOCUT_AUTHORITY_REPOSITORY`：外部权威仓库未配置，不代表业务代码失败。

全量 pytest 还可能暴露与本次启动修复无关的历史测试基线失败；交付报告必须同时给出
定向测试、收集结果和全量测试的真实失败列表，不能把它们标成通过。
