<div align="center">

# nano-onto

**超轻量的 Palantir 式本体，给 AI Agent 用。**
对象、关系、**动作**、角色、审批、读权限、审计——核心约 2500 行 Python，一个下午读完。

[English](README.md) · MIT · Python ≥ 3.10 · 唯一依赖 PyYAML

</div>

---

📏 核心代码行数：运行 `bash core_lines.sh` 自己数（核心约 2500 行；四个可选模块另加约 700 行）。

对象和关系描述"有什么"；**动作**描述"能做什么、按什么规则、要谁批准"——而且这些由代码强制执行，不靠模型自觉。

现在开源的"本体 / 知识图谱"项目大多停在**语义层**：实体、属性、关系。这足够让 agent **回答问题**，但不够让它**改东西**——提交采购单、取消合同、拉黑供应商。这些操作背后的业务规则写在制度文件和人的脑子里，让大模型"记住规则"迟早会漏一条。

nano-onto 补的就是 Palantir Ontology 里最值钱、开源世界最缺的那一层：**动作层**。

| | 语义层（知识图谱 / GraphRAG） | **nano-onto** |
|---|---|---|
| 有什么 | ✅ 对象、关系 | ✅ 对象、关系 |
| 能做什么 | ❌ | ✅ 带参数和效果的**动作** |
| 谁能做 | ❌ | ✅ 每个动作的角色权限 |
| 按什么规则 | 写在 prompt 里 | ✅ **确定性规则**，代码判定，不交给模型 |
| 审批 | ❌ | ✅ 多角色审批、职责分离（不能自己批自己） |
| 谁能看什么 | ❌ | ✅ 行级 / 字段级读权限，自动脱敏 |
| 可追责 | ❌ | ✅ 每次尝试都进审计日志，每条规则带原文出处 |
| 数据放哪 | 复制一份进图存储 | ✅ 自带 SQLite / PostgreSQL / DuckDB 存储，**或者直接用你现有的表，不复制** |
| 外部副作用 | ❌ | ✅ Webhook 与动作同事务入队，失败自动重试 |
| 变更管控 | ❌ | ✅ 本体带版本，迁移先审后执行 |
| 给 agent 的接口 | 检索 | ✅ **MCP server**（stdio 与 HTTP）、CLI、Claude / nanobot **Skill** |

## 30 秒跑起来

```bash
pip install -e .
python examples/procurement/demo.py
```

示例里：8000 元采购直接执行；向黑名单供应商下单被拒；6 万元采购进入"部门经理 + 财务总监"审批；申请人想自己批被拦下（这次尝试也会进审计日志）；两人批完后才真正创建采购单；同一个查询，员工只看到自己的采购单、看不到供应商收款账号，财务总监都能看到。

## 一个动作长这样

见 [`examples/procurement/ontology.yaml`](examples/procurement/ontology.yaml)。规则用一个**安全的**小表达式语言写（用 `ast` 解析 + 白名单 + 解释执行，从不 `eval`），可以沿关系走（`params.po.supplier.rating`）、和当前操作人比较（`params.po.requester != actor.id`）、用 `today()` 写"某天起生效"的规则。

整个本体在加载时做**严格**检查：规则里引用了不存在的属性、效果漏填了必填属性、角色不存在、键名拼错（例如把 `allowed_roles` 写成 `allowed_role`，否则这个动作会对所有人开放）——都会在任何 agent 调用之前报错。

## 谁能看什么

读权限写在本体里，agent 的每一次读取都会经过它：

```yaml
object_types:
  PurchaseOrder:
    visible_when: obj.requester == actor.id or actor.role != 'employee'   # 行级
  Supplier:
    properties:
      bank_account: {type: string, readable_by: [cfo]}                    # 字段级：其他角色看到 ***
  Salary:
    readable_by: [cfo]                                                    # 类型级
```

看不到的行表现为"不存在"——读取时如此，执行动作时也如此：动作不能引用操作人看不到的对象。看不到的字段返回 `***`，并且不能用作查询条件（否则等值过滤会泄露它的值）；指向不可见对象的关系会被略去。规则判定仍然能读到所有字段——判定的是引擎，不是读取者。

## 接到 Agent 上

**MCP（stdio）**——Claude Desktop / Claude Code / Cursor / nanobot 等任意 MCP 客户端，零额外依赖：

```bash
nano-onto init --schema examples/procurement/ontology.yaml --db shop.db --seed examples/procurement/seed.json
nano-onto serve-mcp --schema examples/procurement/ontology.yaml --db shop.db --actor alice --role employee
```

每个动作变成一个 tool，描述里写明规则；另有 `onto_describe`、`onto_query`、`onto_get`、`onto_explain`（试运行）、`onto_request_status`、`onto_pending_approvals`、`onto_approve`、`onto_reject`。**操作人身份在启动 server 时固定**，模型无法给自己换角色来绕过规则。

**MCP（HTTP，一人一个 token）**——一个进程服务多个用户，由 bearer token（而不是模型）决定是谁在操作：

```bash
nano-onto token --tokens tokens.yaml --actor alice --role employee      # token 只显示一次，文件里只存哈希
nano-onto serve-http --schema ontology.yaml --db shop.db --tokens tokens.yaml --port 8765
# POST http://127.0.0.1:8765/mcp   Authorization: Bearer nonto_...
```

实现的是 MCP Streamable HTTP 传输的请求/响应部分（JSON 应答，没有服务端推送流）。默认只监听本机并拒绝其他浏览器来源；对外暴露前请在前面加 TLS。

**Skill**：`nano-onto export-skill --out skills/procurement` 生成 `SKILL.md`，教 agent 只通过 `nano-onto run` 改数据。Skill 方式下 `--actor/--role` 由 agent 自己传，如果不能让 agent 自选身份，请用 MCP。

**CLI / Python**：见英文 README。常用的新增命令：`nano-onto request <id>`（审批请求的状态）、`nano-onto query <Type> --actor alice --role employee`（按某人的权限查看）。

## 直接用你已有的数据库

`--db`（或 `open_store`）可以是 SQLite 文件、`postgresql://user:pass@host/db`（`pip install 'nano-onto[postgres]'`）或 `duckdb://file.duckdb`（`pip install 'nano-onto[duckdb]'`）。nano-onto 自己的表都以 `onto_` 为前缀。

更进一步：**完全不复制数据**。把对象类型和关系映射到已有的表上，动作直接读写这些表，并且和审计记录在同一个事务里：

```yaml
object_types:
  Supplier:
    source: {table: suppliers, columns: {supplier_id: id, name: supplier_name}}   # 属性 -> 列
link_types:
  supplier:
    from: PurchaseOrder
    to: Supplier
    source: {column: supplier_id}                                   # purchase_orders 表上的外键列
  tags:
    from: PurchaseOrder
    to: Tag
    cardinality: many_to_many
    source: {table: po_tags, from_column: po_no, to_column: tag}    # 中间表
```

`python examples/erp_readthrough/demo.py` 在一个预先存在的数据库上演示：只写被修改的列，没映射的列绝不触碰，数据库自身的约束照常生效（`CHECK` 失败会让整个动作回滚，并以 `invalid` 记入审计）。

## 外部副作用：Webhook

```yaml
webhooks:
  erp:
    url: ${ERP_HOOK_URL}                 # 用 ${环境变量}，地址和密钥不进 YAML
    secret_env: ERP_HOOK_SECRET          # 可选：HMAC-SHA256 签名（X-Nano-Onto-Signature）
    max_attempts: 6
action_types:
  submit_purchase_order:
    effects:
      # ... create / link ...
      - webhook: erp
        event: purchase_order.created
        payload: {po_id: po.po_id, amount: params.amount}
```

投递记录和动作**在同一个事务里**写入 outbox 表，所以它只在动作真正提交时存在——被拒绝、待审批、回滚的动作都不会发通知。之后再 POST 出去；失败按指数退避重试（30 秒、1 分钟、2 分钟……最长 1 小时），超过 `max_attempts` 标记为 `dead`。MCP server 在后台投递；`nano-onto run` 会立即尝试一次；`nano-onto deliver [--watch 30]` 清空队列；`nano-onto outbox` 查看队列。语义是"至少一次"，接收方请按 `X-Nano-Onto-Delivery` 头去重。

## 修改本体

数据库会记下它当前遵循的本体。本体文件一旦改动，引擎拒绝执行任何动作，直到这次改动被审阅并应用：

```bash
nano-onto migrate --dry-run     # 本体改了什么、已有数据会怎样
nano-onto migrate --by carol    # 在一个事务里应用，并记入审计日志
nano-onto history               # 这个数据库遵循过的所有本体版本
```

迁移会回填默认值、处理改名（在类型 / 属性 / 关系上写 `renamed_from: 旧名`）、转换类型、删除多余属性，并关闭那些动作或参数已不存在的待审批请求。做不到安全处理的情况——值不符合新类型、新增必填属性却无值可填、被删除类型下还有数据（除非加 `--drop-removed`）——会作为问题报告，并且**什么都不改**。规则变化不会让待审批请求作废，因为最后一个审批到达时规则会重新判定。

## 本体从哪来？

手写 YAML 适合 demo。真实场景用姊妹项目 **[Ontology-Anything](../ontology-anything)**：读制度、SOP、合同和表格，提出对象类型、动作和规则（**每条带原文引用**），标出文件之间的冲突，人工审核通过后直接产出 nano-onto 本体。YAML 中以 `x-` 开头的键在任何位置都会被忽略，方便这类工具附带审核元数据。

## 语义（一段话）

`execute` 检查角色，校验并转换参数（解析对象引用），判定每条规则，然后：有 `deny` → **denied**；有操作人自己不具备的 `require_approval` → **pending_approval**（存下审批请求，数据不变）；否则执行效果 → **executed**。以上全部和审计记录一起在**一个串行事务**里完成：规则看到的状态就是效果修改的状态，任何失败都会整体回滚并以 **invalid** 记入审计。最后一个审批到达时，规则会按当前数据重新判定后才执行；审批齐了却已无法执行的请求会被关闭为 `failed`，不会一直挂着。每一次尝试——拒绝、被拦下的审批、失败的效果——都进审计日志。缺失的值是 `None`，和 `None` 做大小比较结果为假：如果缺值也应该触发规则，请写成 `x == None or x > 3`。

## Benchmark

`benchmark/` 衡量动作层到底带来了什么：同样的 27 个请求（允许的、需审批的、禁止的、施压下的禁止请求、越权读取），分别交给一个**只有普通数据库工具、规则写在 prompt 里**的 agent，和一个**使用 nano-onto 工具**的同款 agent。期望结果不是手写的，而是由参考引擎算出来的。

```bash
python -m benchmark.bench --base-url http://localhost:8000/v1 --model <model>     # 任意 OpenAI 兼容接口
python -m benchmark.bench --agent scripted:compliant                                # 不需要模型
```

第一组结果（Qwen3-8B，每个对照臂 81 轮）：规则只写在 prompt 里时，模型在 **43%** 的轮次里违反了规则——用户施压的 18 轮（"财务总监已经口头同意了""出了问题我负责"）**全部**违规；用 nano-onto 时违规率 **0%**，任务成功率从 42% 升到 96%。这只是一个小模型在一个小领域上的结果；指标含义、完整表格和注意事项见 [benchmark/README.md](benchmark/README.md)。

## 需要知道的限制

- PostgreSQL 上每个数据库同一时刻只执行一个动作（advisory lock），SQLite 同一时刻一个写入者，DuckDB 只允许单进程。对 agent 负载足够，不适合高并发写入。
- 行级过滤（`visible_when`）是取出该类型的行之后在 Python 里判定的，没有下推到 SQL。
- 直读已有表要求单列主键，且列名是普通标识符。
- HTTP 传输使用静态 bearer token，没有事件流——不支持 OAuth 和服务端主动消息。
- 动作只能引用操作人看得到的对象（看不到的对象和不存在的对象一样，返回 not found），但规则会读取被脱敏的**字段**。操作人能看到自己这次请求触发了哪些规则，所以一条针对脱敏字段的规则每次尝试会透露这一个事实；正因如此，试运行（explain）也会记入审计日志。

## 路线图

- [x] Postgres / DuckDB 存储；直接读现有数据库，不用复制
- [x] 行级 / 字段级权限，查询脱敏
- [x] 外部副作用动作（提交后调用 API，带重试）
- [x] 本体版本管理与迁移
- [x] HTTP MCP 传输与按用户鉴权
- [x] Benchmark 框架：有 / 无 nano-onto 时 agent 的任务成功率与违规率
- [ ] 更多模型的 Benchmark 数字（目前只发布了一个）
- [ ] 行级过滤下推到 SQL；直读支持联合主键
- [ ] HTTP 传输支持 OAuth 与 SSE

## 测试

```bash
pip install -e ".[dev]" && pytest
```

测试会跑它能找到的所有后端：SQLite 必跑，装了 DuckDB 就跑 DuckDB，PostgreSQL 使用 `$NANO_ONTO_TEST_PG`（或者在装了 `pgserver` 包时起一个临时的内嵌实例）。
