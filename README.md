# SAP Group Reporting 合并财务报表沙盒模拟系统

本项目基于 **Python + SQLite + Excel (OpenPyXL/Pandas)**，以 **SAP S/4HANA Group Reporting** 的核心架构为蓝本，从零设计并实现了一套支持全生命周期财务合并处理、多维审计追踪（Audit Trail）及红绿灯状态门禁控制的合并财务报表模拟系统。

---

## 一、 核心设计理念（SAP Group Reporting 架构映射）

区别于传统独立 EPM 多维库（如 HFM）依赖外部 ETL 抽取试算平衡表的模式，本系统完整复刻了 SAP 新一代实时合并的核心特征：

1. **业财同源与凭证级粒度（`ACDOCA` -> `ACDOCU`）：** 底层单体凭证表（`src_s4_acdoca`）在业务发生时即记录贸易伙伴（`partner_unit`）与交易类型（`subitem`），支持直连聚合释放与异构 Excel 灵活上载并存。
2. **借正贷负符号体系（Sign Logic）：** 全库严格遵循借方为正（`+`）、贷方为负（`-`），任何维度组合下 `SUM(amount) == 0` 即代表试算平衡。
3. **非破坏性审计线索（Posting Level 分层机制）：** 系统绝不覆盖或修改已上报的原始数据，所有调整、外币折算差额、内部抵消与权益合并均通过追加不同 `posting_level` 和 `doc_type` 的新凭证实现，确保每笔合并数 100% 可追溯。

---

## 二、 数据库表结构与数据字典 (`sap_consolidation.db`)

### 1. 主数据与映射规则表

| 表名 | SAP 对应概念 | 核心字段与说明 |
| :--- | :--- | :--- |
| **`dim_unit`** | Consolidation Unit (合并单元) | `unit_id`(主键), `unit_name`, `local_curr`(本位币), `source_type`(`S4_DIRECT`/`EXCEL_TB`/`EXCEL_RAW`), `ownership_pct`(持股比例), `consol_method`(`FULL`全额/`EQUITY`权益法/`DIVESTED`已处置) |
| **`dim_fs_item`** | FS Item (合并报表项目) | `fs_item`(主键), `description`, `item_type`(`AST`/`LEQ`/`INC`/`EXP`), `ct_method`(`CLO`期末/`AVG`平均/`HIS`历史), `is_ic_relevant`(是否内部往来科目) |
| **`map_gl_to_fs`** | Mapping Rules (科目映射表) | `source_system`(`SAP_COA`/`C3000_COA`/`C4000_NAME`), `source_code`(源科目代码/名称), `fs_item`(目标合并项目), `default_subitem` |
| **`dim_fx_rate`** | Exchange Rates (汇率表) | `fiscal_period`, `from_curr`, `to_curr`, `rate_type`(`CLO`/`AVG`/`HIS`), `rate` |

### 2. 事实表与监控日志表

| 表名 | SAP 对应概念 | 核心字段与说明 |
| :--- | :--- | :--- |
| **`src_s4_acdoca`** | Universal Journal (单体内账凭证) | `doc_no`, `line_item`, `fiscal_period`, `unit_id`, `gl_account`, `partner_unit`, `subitem`, `amount_lc` |
| **`fact_acdocu`** | Consolidation Journal (合并核心表) | `fiscal_period`, `consol_group`, `unit_id`, `partner_unit`, `fs_item`, `subitem`, `posting_level`, `doc_type`, `amount_lc`, `amount_gc`, `audit_remark` |
| **`sys_task_log`** | Monitor Task Log (状态机日志) | `log_id`, `run_time`, `fiscal_period`, `step_code`, `step_name`, `status_light`(`GREEN`/`YELLOW`/`RED`), `message` |

### 3. 核心维度枚举定义
* **过账层级（`posting_level`）：**
  * `00`：单体上报数据（Data Release / Flexible Upload）
  * `01`：单体调整分录（Manual Journal Entry，保留 `00` 原始快照）
  * `20`：内部往来与内部交易自动抵消（ICMR Elimination & UPI）
  * `30`：权益合并、少数股东损益/权益、权益法调整及实体处置出表（COI）
* **子目/交易类型（`subitem`）：**
  * `900`：年初/期初余额（Opening Balance / Carryforward）
  * `910`：本期发生额（Current Period Movement）
  * `990`：合并范围退出/实体处置剥离（Total Divestiture / Scope Exit）

---

## 三、 模块清单与执行顺序说明

### 环境准备
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install pandas openpyxl
```

### 全生命周期脚本运行指南
1. **`init_db.py` — 数据库初始化：** 创建全部主数据表、事实表，插入 4 家初始实体（`C1000`母公司、`C2000`国内全资子公司、`C3000`美国美元子公司、`C4000`非全资子公司）及模拟总账凭证。
2. **`generate_excels.py` — 模拟外部子公司异构 Excel 填报：** 在 `uploads/` 目录生成标准英文 TB（`upload_C3000.xlsx`）与含无效表头、无独立关联方列的非标中文流水表（`upload_C4000.xlsx`）。
3. **`sprint1_data_monitor.py` — Data Monitor 采集与清洗引擎：** 执行 SAP 直连 SQL 聚合、标准 TB 映射、非标表格清洗（利用正则自动从中文备注识别贸易伙伴），并执行第一道单体试算平衡校验。
4. **`post_manual_journal.py` — 手工调整凭证引擎：** 模拟 Fiori 手工凭证过账（`PL 01`），强制校验单张凭证借贷平衡后写入 `fact_acdocu`。
5. **`sprint2_currency_translation.py` — 外币报表折算引擎：** 按 `CLO`、`AVG`、`HIS` 汇率将 `C3000`（USD）折算为集团币（CNY），并自动计算差额过账至 `310400`（其他综合收益-外币折算差额 CTA）。
6. **`sprint3_ic_elimination.py` — ICMR 内部交易自动抵消引擎：**
   * 内部往来对账：自动抵消匹配部分，5 万元容差内差异自动重分类至外部往来，超容差单边挂账（如 `144,000` 和 `100,000`）触发红灯拦截。
   * 内部购销与未实现毛利（UPI）：自动计算买方留存存货所含的 `25%` 内部毛利（`22,500.00`）并生成抵消凭证。
7. **`sprint4_coi_and_report.py` — COI 权益抵消与报表导出：** 抵消母公司长投与子公司实收资本，计算 `C4000`（持股 80%）的少数股东初始权益（`200,000`）与当期少数股东损益（`40,000`），导出多维工作底稿 Excel。
8. **`master_pipeline.py` — 一键总控流水线与状态门禁：** 串联 `Sprint 1 -> 4`，记录 `sys_task_log`，支持 `strict_ic_mode=True`（存在超容差内部往来时强制阻断关账）与 `False`（黄灯暂估放行）双模式。
9. **`sprint5_dynamic_expansion.py` — 主数据动态扩展与权益法合并：** 动态追加新报表项目 `611100`（投资收益）与联营企业 `C5000`（持股 30%，`EQUITY`），实现单行合并确认 `30,000` 投资收益。
10. **`sprint6_balance_carryforward.py` — 跨年度余额结转（`2026.01 -> 2027.01`）：** 资产负债按层级结转至 `Subitem 900`，上年全部损益自动滚入年初未分配利润（`-876,500`），并在次年年初自动冲回上年留存存货未实现毛利（`+22,500`）。
11. **`sprint7_divestiture.py` — 实体处置与合并范围退出：** 模拟 `2027.01` 出售子公司 `C2000`，通过 `Subitem 990` 全面剥离其资产负债余额，并将母公司单体处置收益（`200,000`）精准调整为集团合并处置收益（`42,500`）。

---

## 四、 审计追踪查看方式
* **底层数据表查看：** 在 VS Code 中安装 **SQLite Viewer** 扩展，打开 `sap_consolidation.db` 即可像使用 SAP `SE16N` 一样查阅 `fact_acdocu` 与 `sys_task_log`。
* **合并工作底稿查看：** 运行后生成的 `Consolidated_FS_*.xlsx` 包含两个工作表：`合并工作底稿(Audit_Trail)`（横向展示各单体、简单加总、`PL 20` 抵消、`PL 30` 抵消及合并最终数）与 `ACDOCU底层凭证流(明细)`。
