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

## 环境及检查

使用 Python 3.11.2 标准库，无需网络或额外安装。在项目目录可执行 `python3 -m unittest discover -s tests -v` 核对输入文件、表头与记录形状。各来源字段及记录数量见 `data-contract.json`。目前提供资料读取检查，不生成业务分析结果。
