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

使用 Python 3.11.2 标准库，无需网络或额外安装。在项目目录可执行 `python3 -m unittest discover -s tests -v` 核对输入文件、表头与记录形状。各来源字段及记录数量见 `data-contract.json`。目前提供资料读取检查，不生成业务分析结果。
