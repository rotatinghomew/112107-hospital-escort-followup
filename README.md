# 陪诊申请、执行记录与康复衔接分析

以下资料为合成记录，不含实际个人信息。文件以 UTF-8 编码，普通字段含日期或本地时间（东八区），分号表示同一字段的多值。空值表示在该记录中未提供，不自动当零。表内的凭据编号用于核对记录归属。

## 数据来源

- `data/bookings.csv`：预约及释放范围，等待上限为分钟；support=team 需双人。
- `data/households.csv`：来往地和后续预约；requested_at 为排队起点。
- `data/rides.csv`：开始、结束是员工可工作的时段，不是固定出发时间。
- `data/routes.csv`：往返方向共用的耗时区间（分钟），没有表项的地点对尚未验证。
- `data/handover.csv`：第三方授权及承接变更；同 id 以通知时点可见的最后一条为准。
- `data/commitments.csv`：既有已确认服务明细，用途中同一 trip 表示同一对外行程。
- `data/appointment_notices.csv`：医院取消通知和有缺口的交接凭据。
- `data/closing_days.csv`：站点移交的日档目录；`cutoff` 是历史派班截点，`complete` 指该日受理、供给和后续登记资料是否齐备，不保证每单均有签收。
- `data/intake_register.csv`：受理台留存的办理结论，不等同于实际发生的陪诊。`known_at` 是上报时刻。
- `data/trip_receipts.csv`：现场凭据发生时间和回填时间分开；完成交接需有对应地点与签收。
- `data/extra_shift_quotes.csv`：额外班次的报价，星期按周一为 1，服务时段为当地时间；费用为每个适用服务日的总价，单位分。

## 环境及检查

使用 Python 3.11.2 标准库，无需网络或额外安装。在项目目录可执行 `python3 -m unittest discover -s tests -v` 核对输入文件、表头与记录形状。各来源字段及记录数量见 `data-contract.json`。

## 协调与重放工具

代码位于 `care_desk/`，只使用标准库：

```bash
python3 -m unittest discover -s tests -v
python3 -m care_desk report --save-run
python3 -m care_desk history
# 或
python3 analyze.py
```

命令会生成：

- `reports/quote-comparison.json`：完整工作日重放、报价增量、费用、未接原因和审计条件；
- `reports/coordination-plan.json`：2026-06-10 需求在 2026-06-08T13:05 可见资料下的整段预留结果；
- `reports/outcomes-current.json` / `outcomes-archive-20260611.json`：全量回执视图和归档时点视图；
- `reports/report.md`：可给协调员/采购同事阅读的汇总；
- `reports/history/`：带源 CSV SHA-256、cutoff 和重放条件的运行快照。

换一个工作周时，可建立新的数据目录（同表头放入新报价、回执、日档等），执行：

```bash
python3 -m care_desk report --data <新数据目录> --out reports-week-XX --save-run
```

历史重放只读 `data/`，不会回写原行程或受理记录。

## 已固化的业务判定

1. **一户一日一需求**：按 `(home, date)` 合并替代预约；选中的预约必须能和家庭地点、医院释放窗口、后续交接点/回家路线组成原子整段安排。
2. **最差情况可兑现**：到达窗口用路线时长区间求可出发区间；释放段使用 `release_from + max_wait` 限制最长等待，并用 `release_until + max_route_minutes` 检查后续交接时限和班组下班时间，不用平均耗时。
3. **双人及员工冲突**：`support=team` 的每个行程环节需要同一时段有两名不同员工；已确认承诺作为 busy interval，不能分给其他家庭。
4. **授权接棒**：同一授权 id 按 `received_at` 的可见最后状态判断；只有 `approved`、地点匹配且有效期覆盖释放/交接范围时才省略返程资源。`uncertain` 等意向状态不释放返程。
5. **优先级**：新请求按 `(min(expires), household.requested_at, home)` 排序；先尝试形成完整方案，不能形成完整方案时不占用部分资源。
6. **取消与待核**：可见取消只标失效预约/承诺范围，其余有效承诺仍受保护；已到医院但在归档时点无有效末段签收的记录保持 `missing_proof_pending`，不重派去程。迟报签收保留实际发生时间和登记时间。
7. **无后见之明重放**：每个归档日只使用 closing cutoff 前已可知的受理、授权、取消通知和已确认承诺；实际离院/到达、事后批准、cutoff 后通知和回执不进入排班。
8. **增班收益**：仅使用 `complete=yes` 且受理/供给凭据齐备、单元和星期均匹配报价的工作日。新增数只计 baseline 未覆盖且报价后能形成完整行程的户次，避免把替换原班误算成增单。

## 本数据的重放结论

两个可比重放日是 `2026-05-27` 和 `2026-06-03`（均为周三、北区、完整日档）。未完整的 `2026-06-04`、`2026-06-10` 排除。

- `E1`（08:00–09:30，1 班）：0 净增。它不覆盖释放/返程瓶颈。
- `E2`（09:50–11:50，1 班）：0 净增。两个未接需求都是 team 返程；原有 late worker 已被 Y1/Y2 占用，只加一人仍凑不齐同时到位的两人。即使让 E2 替换原承诺，也只是挪动原订单，仍无第二人接新户。
- `E3`（09:50–11:50，2 班）：每个可比日净增 1 户，共 2 户（H5/P2、H7/P4），类别均为 `outbound_return_home` team 完整往返。每日报价 30,000 分；两个重放日累计 60,000 分，重放样本中每新增户成本 30,000 分。

因此，在这些 6 月报价仍处于 `valid_until` 之前时，只有 `E3` 显示净新增完整行程；若在 2026-11-25 之后讨论，三份报价均已过有效期，应用新工作周报价重跑后再决定，不能直接沿用旧报价采购。

