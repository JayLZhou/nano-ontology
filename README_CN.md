<picture>
  <source media="(prefers-color-scheme: dark)" srcset="./images/readme-cover-dark.svg">
  <img alt="nano-onto：给 AI agent 用的可执行本体" src="./images/readme-cover-light.svg">
</picture>

<div align="center">
  <p>
    <a href="./README.md">English</a> |
    <a href="./README_CN.md">简体中文</a>
  </p>
  <p>
    <a href="https://github.com/JayLZhou/nano-ontology"><img src="https://img.shields.io/github/stars/JayLZhou/nano-ontology?style=flat&logo=github" alt="GitHub stars"></a>
    <a href="https://github.com/JayLZhou/nano-ontology/actions/workflows/ci.yml"><img src="https://github.com/JayLZhou/nano-ontology/actions/workflows/ci.yml/badge.svg?branch=main" alt="测试"></a>
    <a href="./pyproject.toml"><img src="https://img.shields.io/badge/python-%3E%3D3.10-blue" alt="Python 3.10 及以上"></a>
    <a href="./pyproject.toml"><img src="https://img.shields.io/badge/dependencies-PyYAML%20only-brightgreen" alt="唯一依赖 PyYAML"></a>
    <a href="./docs/agents.md"><img src="https://img.shields.io/badge/MCP-stdio%20%2B%20HTTP-8A2BE2" alt="MCP：stdio 与 HTTP"></a>
    <a href="./LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License"></a>
  </p>
  <p>
    <a href="./docs/walkthrough_cn.md">完整流程</a> ·
    <a href="./docs/ontology-reference.md">本体参考</a> ·
    <a href="./docs/agents.md">接入 agent</a> ·
    <a href="./benchmark/README.md">Benchmark</a> ·
    <a href="./CHANGELOG.md">更新日志</a>
  </p>
</div>

# nano-onto

🧭 **nano-onto** 是一个轻量、开源、Palantir 式的本体，给 AI agent 用，用 Python 写成。你用一个 YAML 文件描述业务——有什么、能做什么、按什么规则、要谁批准、谁能看什么——nano-onto 在模型之外强制执行这些约定，任何支持 MCP 的 agent 都能接入。

## 💡 为什么是 nano-onto

- **补上动作层，而不只是语义层：** 知识图谱告诉 agent "有什么"。nano-onto 还定义它"能做什么"——带参数、规则、审批和效果的动作——agent 可以改业务数据，而不必指望它记住制度。
- **模型说不动的规则：** 规则是确定性的表达式，由代码判定。在我们的基准测试里，规则只写在 prompt 中时，模型在 43% 的轮次里违规，用户一施压就全部违规；用 nano-onto 时是 0%。
- **审批、权限、审计都内置：** 多角色审批与职责分离、行级和字段级读权限、每一次尝试的审计记录——包括被拦下的尝试，以及是哪条规则拦下的。
- **接你现有的 agent 和数据库：** 一个 MCP server（stdio 或 HTTP）接 Claude Desktop、Claude Code、Cursor、nanobot 或你自己的 agent。默认 SQLite，支持 PostgreSQL 和 DuckDB，也可以把本体映射到已有的表上，数据留在原处。
- **小到读得完：** 核心约 2500 行，唯一依赖 PyYAML。运行 `bash core_lines.sh` 自己数。

## 从这里开始

| 你想做什么 | 去哪里 |
|---|---|
| 两分钟看完整个流程 | [快速开始](#-快速开始)，然后 `bash examples/walkthrough.sh` |
| 弄懂流程里的每一步 | [完整流程](./docs/walkthrough_cn.md) |
| 给自己的业务写本体 | [本体参考](./docs/ontology-reference.md) |
| 接到 Claude Desktop、Claude Code、Cursor 或自己的 agent 上 | [接入 agent](./docs/agents.md) |
| 放在一个已有的数据库前面 | [直读已有表](./docs/ontology-reference.md#existing-tables-read-through) |
| 查某条命令 | [CLI 参考](./docs/cli-reference.md) |
| 了解原理和限制 | [架构](./docs/architecture.md) |
| 量化它带来了什么 | [Benchmark](./benchmark/README.md) |

## 📦 安装

需要 **Python 3.10 及以上**，唯一依赖 PyYAML。

```bash
git clone https://github.com/JayLZhou/nano-ontology.git
cd nano-ontology
python -m pip install -e .
```

<details>
<summary>可选：PostgreSQL、DuckDB、开发工具</summary>

```bash
python -m pip install -e ".[postgres]"   # postgresql:// 存储（psycopg）
python -m pip install -e ".[duckdb]"     # duckdb:// 存储
python -m pip install -e ".[dev]"        # pytest、ruff、duckdb
```

</details>

## 🚀 快速开始

```bash
python examples/procurement/demo.py
```

这会在内存里让一家小公司的采购业务按规则走一遍。想自己动手、像 agent 那样操作：

**最初三步：**

1. 用示例本体和种子数据建一个数据库。

   ```bash
   export NANO_ONTO_SCHEMA=examples/procurement/ontology.yaml NANO_ONTO_DB=shop.db
   nano-onto init --seed examples/procurement/seed.json
   ```

2. 提一个规则不会直接放行的请求——员工要买 6 万元的服务器。

   ```bash
   nano-onto run submit_purchase_order -p title=服务器 -p amount=60000 -p supplier=S-002 --actor alice --role employee
   ```

   采购单**不会**被创建，而是成为一条需要部门经理和财务总监都批准的请求。

3. 把同一个数据库交给 agent。操作人身份在 server 启动时固定，模型选不了角色。

   ```bash
   nano-onto serve-mcp --actor alice --role employee
   ```

然后一次跑完整个流程——试运行、审批、读权限、审计日志、在审核下改规则、MCP（stdio 与 HTTP）、导出 Skill：

```bash
bash examples/walkthrough.sh
```

<details>
<summary>接入 Claude Desktop、Claude Code 或 Cursor</summary>

Claude Desktop（`claude_desktop_config.json`）和 Cursor（`.cursor/mcp.json`）：

```json
{ "mcpServers": { "procurement": {
    "command": "nano-onto",
    "args": ["serve-mcp", "--schema", "/abs/path/ontology.yaml", "--db", "/abs/path/shop.db",
             "--actor", "alice", "--role", "employee"] } } }
```

Claude Code：

```bash
claude mcp add procurement -- nano-onto serve-mcp --schema /abs/path/ontology.yaml --db /abs/path/shop.db --actor alice --role employee
```

每个动作变成一个 tool，描述里列出它的规则；另有 `onto_describe`、`onto_query`、`onto_get`、`onto_explain`、`onto_request_status`、`onto_pending_approvals`、`onto_approve`、`onto_reject`。详见[接入 agent](./docs/agents.md)。

</details>

<details>
<summary>通过 HTTP 服务多个用户，一人一个 token</summary>

```bash
nano-onto token --tokens tokens.yaml --actor alice --role employee      # token 只显示一次，文件里只存哈希
nano-onto serve-http --tokens tokens.yaml --port 8765
# POST http://127.0.0.1:8765/mcp      Authorization: Bearer nonto_...
```

决定"谁在操作"的是 token，不是模型。撤销 token 立即生效。详见[接入 agent](./docs/agents.md#mcp-over-http)。

</details>

<details>
<summary>在 Python 里使用</summary>

```python
from nano_onto import Actor, Engine, Ontology, open_store

onto = Ontology.load("examples/procurement/ontology.yaml")
engine = Engine(onto, open_store("shop.db", onto))

res = engine.execute("submit_purchase_order", {"title": "服务器", "amount": 60000, "supplier": "S-002"},
                     Actor("alice", "employee"))
res.status        # executed | pending_approval | denied | invalid
res.trace         # 哪些规则触发了，附原文出处
engine.query("PurchaseOrder", actor=Actor("alice", "employee"))   # 按权限读取
```

</details>

如果 nano-onto 对你有用，在 GitHub 上点个 star 是最简单的支持方式。

## 🎬 看它怎么工作

下面的内容都是真实输出：录下来的模型对话和终端会话，由 [`scripts/readme_assets.py`](./scripts/readme_assets.py) 重新生成。

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="images/same-request-dark.svg">
    <img src="images/same-request-light.svg" alt="同一个请求、同一个模型：数据库工具 vs. nano-onto 工具" width="100%">
  </picture>
</p>

### 模型说不动的规则

同样的 27 个请求交给同一个模型两次：一次给它普通的数据库工具、制度写在 prompt 里；一次给它 nano-onto 的工具。只写在 prompt 里的规则对普通请求还管用，一遇到施压（"财务总监已经口头同意了""出了问题我负责"）就失守；被强制执行的规则纹丝不动。

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="images/benchmark-dark.svg">
    <img src="images/benchmark-light.svg" alt="Benchmark：规则只在 prompt 里时违规率 43%，用 nano-onto 时 0%；任务成功率 42% 对 96%" width="100%">
  </picture>
</p>

这只是一个小模型在一个小领域上的结果，每个对照臂 81 轮——请当作方向，而不是排行榜。

→ [Benchmark：方法、数字、注意事项](./benchmark/README.md)

### 审批与职责分离

试运行会告诉你哪些规则会触发、为什么。超过门槛的请求要等所有必需的角色批准；申请人不管声称什么角色都批不了自己的申请；最后一个审批到达时，规则会按当前数据重新判定一遍。

<p align="center">
  <img src="images/terminal-approvals.svg" alt="终端：试运行、提交、自批被拒、两人批准后执行" width="900">
</p>

→ [完整流程，第 2–7 步](./docs/walkthrough_cn.md#2-先问再做试运行)

### 每个人只看到自己该看的

读权限写在本体里，每一次读取都会经过它：看不到的行表现为不存在，看不到的字段返回 `***` 且不能用作查询条件，动作也不能引用操作人看不到的对象。

<p align="center">
  <img src="images/terminal-permissions.svg" alt="终端：员工只看到自己的采购单和脱敏后的收款账号，财务总监能看到账号" width="900">
</p>

→ [读权限](./docs/ontology-reference.md#read-permissions)

### 每一次尝试都有记录

执行了的、待审批的、被拒绝的、被拦下的、失败的，甚至试运行：每次尝试一条审计记录，和它描述的那次变更在同一个事务里写入。

<p align="center">
  <img src="images/terminal-audit.svg" alt="终端：上面这次会话的审计日志" width="900">
</p>

→ [一个动作到底做了什么](./docs/architecture.md#what-one-action-does)

### 在审核下修改规则

数据库盖着它所遵循的本体的版本戳。改了一条规则之后，在这次改动被审阅并应用之前什么都不会执行——已经在运行的 server 会停下来，而不是继续执行旧规则。

<p align="center">
  <img src="images/terminal-migrate.svg" alt="终端：规则改动先被拒绝，用 migrate --dry-run 审阅，应用后生效" width="900">
</p>

→ [修改本体](./docs/ontology-reference.md#changing-the-ontology)

### 直接管住你已有的数据库

把对象类型和关系映射到已有的表上，动作就地读写这些表，并且和审计记录在同一个事务里。数据库自身的约束照常生效；Webhook 会把发生了什么告诉你的其他系统——只在事情真的发生之后。

<p align="center">
  <img src="images/terminal-readthrough.svg" alt="终端：在已有 ERP 数据库上执行动作，CHECK 约束使动作回滚，Webhook 投递成功" width="900">
</p>

→ [直读已有表](./docs/ontology-reference.md#existing-tables-read-through) · [Webhook](./docs/ontology-reference.md#webhooks)

## ✍️ 一个动作长这样

```yaml
action_types:
  submit_purchase_order:
    allowed_roles: [employee, dept_manager, cfo]
    parameters:
      title:    {type: string}
      amount:   {type: number}
      supplier: {type: object_ref, object_type: Supplier}
    rules:
      - id: no_blacklisted_supplier
        when: params.supplier.blacklisted          # 沿着对象关系读取
        then: deny
        message: 不得向黑名单供应商采购
      - id: cfo_over_50k
        when: params.amount > 50000
        then: {require_approval: cfo}
        source: {doc: 采购管理制度.md, quote: "单笔采购金额超过50000元的，须经财务总监审批。"}
    effects:
      - create: PurchaseOrder
        as: po
        set: {title: params.title, amount: params.amount, requester: actor.id, status: "'approved'"}
      - link: supplier
        from: po
        to: params.supplier
```

规则用一个安全的小表达式语言写：用 `ast` 解析、白名单检查、解释执行，从不 `eval`。整个文件在加载时就被检查——规则引用了不存在的属性、效果漏填了必填属性、键名拼错（比如把 `allowed_roles` 写成 `allowed_role`），都会在任何 agent 调用之前报错。完整语法见[本体参考](./docs/ontology-reference.md)。

## 架构

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="images/architecture-dark.svg">
    <img src="images/architecture-light.svg" alt="nano-onto 架构：agent、接入面、引擎、存储" width="100%">
  </picture>
</p>

所有接入方式——MCP（stdio / HTTP）、CLI、Python API——背后是同一个引擎，每个动作都是一个串行事务：角色检查、参数、规则、审批、效果、审计。语义、模块划分和需要知道的限制见[架构](./docs/architecture.md)。

## 文档

- **上手：** [完整流程](./docs/walkthrough_cn.md) · [Walkthrough (English)](./docs/walkthrough.md) · [`examples/walkthrough.sh`](./examples/walkthrough.sh)
- **为业务建模：** [本体参考](./docs/ontology-reference.md) · [读权限](./docs/ontology-reference.md#read-permissions) · [直读已有表](./docs/ontology-reference.md#existing-tables-read-through) · [Webhook](./docs/ontology-reference.md#webhooks) · [迁移](./docs/ontology-reference.md#changing-the-ontology)
- **运行：** [接入 agent](./docs/agents.md) · [CLI 参考](./docs/cli-reference.md)
- **原理：** [架构](./docs/architecture.md) · [Benchmark](./benchmark/README.md) · [更新日志](./CHANGELOG.md)
- **示例：** [`examples/procurement`](./examples/procurement) · [`examples/erp_readthrough`](./examples/erp_readthrough)

参考类文档目前只有英文版。

## 本体从哪来？

手写 YAML 适合 demo。真实场景用姊妹项目 **Ontology-Anything**：读制度、SOP、合同和表格，提出对象类型、动作和规则（**每条带原文引用**），标出文件之间的冲突，人工审核通过后直接产出 nano-onto 本体。YAML 中以 `x-` 开头的键在任何位置都会被忽略，方便这类工具附带审核元数据。

## 路线图

- [x] 行级 / 字段级读权限
- [x] PostgreSQL / DuckDB 存储；直读已有表
- [x] Webhook：事务性 outbox 与重试
- [x] 本体版本管理与受审核的迁移
- [x] HTTP MCP 与按用户的 token
- [x] Benchmark 框架与第一组模型结果
- [ ] 更多模型的 Benchmark 数字
- [ ] 行级过滤下推到 SQL；直读支持联合主键
- [ ] HTTP 传输支持 OAuth 与 SSE

## 🤝 参与贡献

欢迎提 issue：bug、复现步骤、使用场景或建议都可以。提 PR 之前请先跑：

```bash
python -m pip install -e ".[dev]"
ruff check . && pytest && bash examples/walkthrough.sh
```

测试会跑它能找到的所有后端：SQLite 必跑，装了 DuckDB 就跑 DuckDB，PostgreSQL 使用 `$NANO_ONTO_TEST_PG`（或在装了 `pgserver` 包时起一个临时的内嵌实例）。

<p align="center">
  <em>感谢关注 nano-onto。MIT License。</em>
</p>
