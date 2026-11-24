# 完整流程

这份文档把一个本体从头到尾走一遍：校验、建库、通过动作改数据、审批、按权限读取、审计、在审核下修改规则，用三种方式交给 agent，最后在浏览器里打开。

全部可以直接运行。一条命令在一个临时数据库上跑完整个流程：

```bash
python -m pip install -e .
bash examples/walkthrough.sh
```

下面按脚本的步骤逐一说明，输出都是真实运行结果。示例是一家小公司的采购系统（[`examples/procurement/ontology.yaml`](../examples/procurement/ontology.yaml)）：三个角色（员工 `employee`、部门经理 `dept_manager`、财务总监 `cfo`），两种对象（供应商 `Supplier`、采购单 `PurchaseOrder`），三个动作（提交采购单、取消采购单、拉黑供应商）。

想自己敲命令的话，先告诉 CLI 本体文件和数据库在哪：

```bash
export NANO_ONTO_SCHEMA=examples/procurement/ontology.yaml NANO_ONTO_DB=shop.db
```

[English](./walkthrough.md)

## 1. 校验并建库

```console
$ nano-onto validate
ok: procurement v0.1.0 -- 2 object types, 1 link types, 3 actions, 7 rules
$ nano-onto init --seed examples/procurement/seed.json
initialised shop.db: Supplier=3, PurchaseOrder=2, links=2
```

`validate` 是故意从严的：规则引用了不存在的属性、效果漏填了必填属性、角色不存在、键名拼错——都在这一步报错，而不是等 agent 调用时才出问题。`init` 建库、导入种子数据，并给数据库盖上"当前遵循哪个本体"的版本戳（第 10 步会用到）。

## 2. 先问再做：试运行

```console
$ nano-onto explain submit_purchase_order -p title=Servers -p amount=60000 -p supplier=S-002 --actor alice --role employee
[WAIT] submit_purchase_order: pending_approval (dry run)
  - needs approval by dept_manager (Department manager): Order exceeds 10,000
  - needs approval by cfo (CFO): Order exceeds 50,000
  [ ] no_blacklisted_supplier: params.supplier.blacklisted
  [ ] positive_amount: params.amount <= 0
  [x] dept_manager_over_10k: params.amount > 10000 -> needs approval by dept_manager
  [x] cfo_over_50k: params.amount > 50000 -> needs approval by cfo
  [ ] warn_rating_c: params.supplier.rating == 'C'
```

试运行不改任何数据，只告诉你每条规则是否触发。agent 通过 `onto_explain` 工具得到同样的结果。试运行也会记入审计日志，因为它判定规则时会读到调用者本来看不到的数据。

## 3. 一个被允许的动作

```console
$ nano-onto run submit_purchase_order -p title=Stationery -p amount=8000 -p supplier=S-001 --actor alice --role employee
[OK] submit_purchase_order: executed
  created po: PO-0003
```

没有规则触发，于是执行效果：创建采购单并关联供应商。角色检查、参数转换、规则判定、效果和审计记录在同一个串行事务里完成。

## 4. 一个被拒绝的动作

```console
$ nano-onto run submit_purchase_order -p title=Chips -p amount=4800 -p supplier=S-003 --actor alice --role employee
[DENIED] submit_purchase_order: denied
  - denied: Blacklisted suppliers cannot be used
  - warning: This supplier is rated C; expect delivery risk
```

`S-003` 在黑名单里。规则 `params.supplier.blacklisted` 沿着引用读到供应商对象，拒绝了这个动作。数据没有任何变化，命令退出码为 2。

## 5. 一个进入审批的动作

```console
$ nano-onto run submit_purchase_order -p title=Servers -p amount=60000 -p supplier=S-002 --actor alice --role employee
[WAIT] submit_purchase_order: pending_approval
  - needs approval by dept_manager (Department manager): Order exceeds 10,000
  - needs approval by cfo (CFO): Order exceeds 50,000
  request: REQ-298D1CC6  waiting for: cfo, dept_manager
```

这是最关键的一步：**采购单此时并不存在。** 存下来的是一条审批请求，里面有参数、申请人和需要签字的角色。agent 如果在这里回答"已完成"就是错的——工具返回的状态明确写着 `pending_approval`。

## 6. 职责分离

```console
$ nano-onto approve REQ-298D1CC6 --actor alice --role cfo
error: requesters cannot approve their own requests (separation of duties)
```

Alice 不能批准自己的申请，不管她声称自己是什么角色。这次尝试会以 `refused` 记入审计日志。

## 7. 审批通过并执行

```console
$ nano-onto approve REQ-298D1CC6 --actor bob --role dept_manager
[WAIT] submit_purchase_order: pending_approval
  - approval recorded; still waiting for: cfo
  request: REQ-298D1CC6  waiting for: cfo
$ nano-onto approve REQ-298D1CC6 --actor carol --role cfo
[OK] submit_purchase_order: executed
  created po: PO-0004
$ nano-onto request REQ-298D1CC6 --json
REQ-298D1CC6 -> executed approved by {'cfo': 'carol', 'dept_manager': 'bob'}
```

最后一个审批到达时，引擎不是简单地"重放"请求，而是以原申请人的身份、**按当前数据把每条规则重新判定一遍**。如果供应商在此期间被拉黑，请求会被关闭为 `denied`；如果现在适用更严的规则，新的审批角色会被加进来；如果动作已经根本无法执行，请求会被关闭为 `failed`，而不是一直挂着。申请人用 `nano-onto request <id>`（或 `onto_request_status` 工具）查看结果。

## 8. 读权限

采购本体规定：员工只能看到自己的采购单，只有财务总监能看到供应商的收款账号：

```yaml
PurchaseOrder:
  visible_when: obj.requester == actor.id or actor.role != 'employee'   # 行级
Supplier:
  properties:
    bank_account: {type: string, readable_by: [cfo]}                    # 字段级
```

```console
$ nano-onto query PurchaseOrder --actor alice --role employee --json
alice sees ['PO-0001', 'PO-0003', 'PO-0004']
$ nano-onto query PurchaseOrder --actor carol --role cfo --json
carol sees ['PO-0001', 'PO-0002', 'PO-0003', 'PO-0004']
$ nano-onto get Supplier S-001 --actor alice --role employee
type: Supplier
id: S-001
properties:
  bank_account: '***'
  blacklisted: false
  name: Northwind Office Supply
  rating: A
  supplier_id: S-001
links: {}
$ nano-onto run cancel_purchase_order -p po=PO-0002 --actor alice --role employee
[INVALID] cancel_purchase_order: invalid
  - po: PurchaseOrder 'PO-0002' not found
```

`PO-0002` 是 Dave 的。Alice 在查询里看不到它，她的**动作**也引用不了它：看不到的对象和不存在的对象返回完全一样的结果，所以没法用动作去试探"某个东西存不存在"。出于同样的原因，被脱敏的字段也不能用作查询条件。

## 9. 审计日志

```console
$ nano-onto audit
   1  2026-11-24T20:25:40  alice (employee)       explain  submit_purchase_order      pending_approval
   2  2026-11-24T20:25:40  alice (employee)       execute  submit_purchase_order      executed
   3  2026-11-24T20:25:40  alice (employee)       execute  submit_purchase_order      denied
   4  2026-11-24T20:25:40  alice (employee)       execute  submit_purchase_order      pending_approval REQ-298D1CC6
   5  2026-11-24T20:25:40  alice (cfo)            approve  submit_purchase_order      refused          REQ-298D1CC6
   6  2026-11-24T20:25:40  bob (dept_manager)     approve  submit_purchase_order      recorded         REQ-298D1CC6
   7  2026-11-24T20:25:41  carol (cfo)            approve  submit_purchase_order      recorded         REQ-298D1CC6
   8  2026-11-24T20:25:41  carol (cfo)            approve  submit_purchase_order      executed         REQ-298D1CC6
   9  2026-11-24T20:25:41  alice (employee)       execute  cancel_purchase_order      invalid
```

每次尝试一行，包括试运行（1）、被拒绝的下单（3）、被拦下的自批（5）、引用自己看不到的采购单（9）。`nano-onto audit --verbose`（或 `--json`）会带上参数、提示信息、创建/修改了什么，以及当时生效的本体版本。

## 10. 修改一条规则

把 `ontology.yaml` 里部门经理审批的门槛从 10000 改成 5000，并把 `version` 改成 `0.2.0`。数据库盖的是旧本体的戳，所以在这次改动被审阅之前，什么都不会执行：

```console
$ nano-onto run submit_purchase_order -p title=Printers -p amount=7000 -p supplier=S-001 --actor alice --role employee
error: this database follows ontology procurement@0.1.0, but this process holds procurement@0.2.0 with different content. If the ontology file changed, review and apply it with `nano-onto migrate`; if the database was migrated, restart this process.
$ nano-onto migrate --dry-run
procurement@0.1.0 -> procurement@0.2.0
schema changes:
  ~ action_types.submit_purchase_order.rules.dept_manager_over_10k.when: "params.amount > 10000" -> "params.amount > 5000"
  ~ version: "0.1.0" -> "0.2.0"
dry run: nothing was applied.
$ nano-onto migrate --by carol
procurement@0.1.0 -> procurement@0.2.0
schema changes:
  ~ action_types.submit_purchase_order.rules.dept_manager_over_10k.when: "params.amount > 10000" -> "params.amount > 5000"
  ~ version: "0.1.0" -> "0.2.0"
applied.
$ nano-onto run submit_purchase_order -p title=Printers -p amount=7000 -p supplier=S-001 --actor alice --role employee
[WAIT] submit_purchase_order: pending_approval
  - needs approval by dept_manager (Department manager): Order exceeds 10,000
  request: REQ-AC609D91  waiting for: dept_manager
$ nano-onto history --json
1 initialised procurement@0.1.0
2 migrated procurement@0.2.0
```

迁移不只是换个戳。类型或属性变化时，它会回填默认值、按 `renamed_from` 处理改名、转换类型、删除多余的值，并关闭已经没有意义的待审批请求；做不到安全处理的情况会作为问题报告，并且什么都不改。版本检查在每次动作和读取时都会做，所以一个长期运行的 server 会在别的进程迁移数据库的那一刻停下来，而不是继续执行旧规则。详见[修改本体](./ontology-reference.md#changing-the-ontology)。

## 11. 交给 agent：MCP（stdio）

```bash
nano-onto serve-mcp --actor alice --role employee
```

这是一个零依赖、走标准输入输出的 MCP server。每个动作是一个 tool，描述里列出它的规则；读取走 `onto_query` 和 `onto_get`，按 Alice 的权限返回。脚本手动给它发了三行 JSON-RPC：

```console
server: nano-onto-procurement
alice calls blacklist_supplier -> denied ["role 'employee' may not run blacklist_supplier; allowed: cfo"]
```

操作人身份在 server 启动时就固定了，模型在对话里说什么都改变不了"它在替谁操作"。Claude Desktop、Claude Code、Cursor 的配置见[接入 agent](./agents.md)。

## 12. 多用户：MCP（HTTP）

```bash
TOKEN=$(nano-onto token --tokens tokens.yaml --actor carol --role cfo | tail -1)
nano-onto serve-http --tokens tokens.yaml --port 18765
```

```console
no token   -> HTTP 401
carol/cfo  -> executed
```

一个进程服务多个用户：每个请求带一个 bearer token，由 token 决定操作人。文件里只存 token 的哈希；`nano-onto token --revoke` 立即生效，不用重启；默认只监听本机。

## 13. 导出为 Skill

```console
$ nano-onto export-skill --out skill
wrote skill/SKILL.md
```

生成的 `SKILL.md` 教支持 Skills 的 agent 只通过 `nano-onto run` 改数据，里面列出每个动作、参数和规则。Skill 方式下 `--actor/--role` 由 agent 自己传，所以如果不能让 agent 自选身份，请用 MCP。

## 14. 给人用的控制台

```bash
nano-onto console --demo alice:employee --demo carol:cfo --auditor cfo --port 18787
```

```console
alice's audit view -> own entries: 8
carol's audit view -> all entries: 13
alice sees bank account -> ***
```

Web 控制台就是同一个引擎前面加了一个页面。脚本向它的 API 问了三件事：Alice 只能看到自己的审计记录，Carol（审计角色）能看到全部，收款账号对 Alice 是脱敏的——和她的 agent 看到的完全一样。在浏览器里打开这个地址就能使用，详见[控制台](./console.md)。

## 接下来

- 写自己的本体：[本体参考](./ontology-reference.md)
- 数据留在原处：[直读已有表](./ontology-reference.md#existing-tables-read-through)，以及 `python examples/erp_readthrough/demo.py`
- 动作提交后通知其他系统：[Webhook](./ontology-reference.md#webhooks)
- 接入真实客户端：[接入 agent](./agents.md)
- 在浏览器里用：[控制台](./console.md)
- 所有命令和参数：[CLI 参考](./cli-reference.md)
- 到底保证什么、不保证什么：[架构](./architecture.md)
