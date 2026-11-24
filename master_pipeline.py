import sqlite3
import datetime
import pandas as pd

# 导入前面四个 Sprint 编写的核心引擎类
from sprint1_data_monitor import DataMonitor
from sprint2_currency_translation import CurrencyTranslationEngine
from sprint3_ic_elimination import ICEliminationEngine
from sprint4_coi_and_report import ConsolidationOfInvestmentsEngine

DB_FILE = "sap_consolidation.db"
FISCAL_PERIOD = "2026.01"
CONSOL_GROUP = "CG01"

class ConsolidationMasterPipeline:
    def __init__(self, db_path, period, strict_ic_mode=True):
        self.db_path = db_path
        self.period = period
        self.strict_ic_mode = strict_ic_mode  # True: 存在超容差内部往来差异时强制阻断关账
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()
        self._init_task_log_table()

    def _init_task_log_table(self):
        """创建 SAP Monitor 任务状态监控日志表"""
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS sys_task_log (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_time TEXT NOT NULL,
                fiscal_period TEXT NOT NULL,
                step_code TEXT NOT NULL,
                step_name TEXT NOT NULL,
                status_light TEXT NOT NULL,  -- GREEN, YELLOW, RED
                message TEXT NOT NULL
            )
        """)
        self.conn.commit()

    def log_status(self, step_code, step_name, light, message):
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.cursor.execute("""
            INSERT INTO sys_task_log (run_time, fiscal_period, step_code, step_name, status_light, message)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (now_str, self.period, step_code, step_name, light, message))
        self.conn.commit()
        
        icon = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}.get(light, "⚪")
        print(f"{icon} [{step_code}] {step_name} -> {light} | {message}")

    # ==========================================================
    # 门禁 1：执行 Data Monitor 并校验所有实体单体平衡 (PL 00 + 01)
    # ==========================================================
    def step1_data_collection_and_gate1(self):
        print("\n" + "="*80)
        print("🚀 STEP 1: 启动 SAP Data Monitor (多源采集、清洗与单体平衡校验)")
        print("="*80)
        
        dm = DataMonitor(self.db_path)
        dm.execute_all(self.period)

        # 检查所有实体在 PL 00 + PL 01 下的本位币借贷净额
        df_check = pd.read_sql_query("""
            SELECT unit_id, ROUND(SUM(amount_lc), 2) AS net_lc
            FROM fact_acdocu
            WHERE fiscal_period = ? AND posting_level IN ('00', '01')
            GROUP BY unit_id
        """, self.conn, params=(self.period,))

        unbalanced = df_check[df_check['net_lc'].abs() >= 0.01]
        if len(unbalanced) > 0:
            err_msg = f"以下实体单体本位币借贷不平，强制阻断后续合并: {unbalanced.to_dict('records')}"
            self.log_status("GATE_01", "单体试算平衡门禁", "RED", err_msg)
            raise RuntimeError(err_msg)
        
        self.log_status("GATE_01", "单体试算平衡门禁", "GREEN", "全集团所有实体单体本位币试算平衡通过。")

    # ==========================================================
    # 门禁 2：执行外币报表折算并校验集团币平衡
    # ==========================================================
    def step2_currency_translation_and_gate2(self):
        print("\n" + "="*80)
        print("🚀 STEP 2: 启动 Currency Translation (外币折算与自动 CTA)")
        print("="*80)

        ct = CurrencyTranslationEngine(self.db_path)
        ct.run_translation(self.period)
        ct.validate_group_currency_balance(self.period)

        df_check = pd.read_sql_query("""
            SELECT unit_id, ROUND(SUM(amount_gc), 2) AS net_gc
            FROM fact_acdocu
            WHERE fiscal_period = ? AND posting_level IN ('00', '01')
            GROUP BY unit_id
        """, self.conn, params=(self.period,))

        unbalanced = df_check[df_check['net_gc'].abs() >= 0.01]
        if len(unbalanced) > 0:
            err_msg = f"以下实体折算后集团币(CNY)借贷不平，强制阻断: {unbalanced.to_dict('records')}"
            self.log_status("GATE_02", "集团币折算平衡门禁", "RED", err_msg)
            raise RuntimeError(err_msg)

        self.log_status("GATE_02", "集团币折算平衡门禁", "GREEN", "全集团折算后集团币(CNY)平衡通过，CTA差额已自动轧平。")

    # ==========================================================
    # 门禁 3：执行内部往来与交易抵消，并检查超容差残留风险
    # ==========================================================
    def step3_ic_elimination_and_gate3(self):
        print("\n" + "="*80)
        print("🚀 STEP 3: 启动 ICMR 内部交易自动抵消 (PL 20) 与容差门禁")
        print("="*80)

        ic = ICEliminationEngine(self.db_path)
        ic.run_all(self.period)

        # 1. 校验 PL 20 自身凭证是否借贷平衡
        net_pl20, = self.cursor.execute("""
            SELECT COALESCE(ROUND(SUM(amount_gc), 2), 0) FROM fact_acdocu
            WHERE fiscal_period = ? AND posting_level = '20'
        """, (self.period,)).fetchone()

        if abs(net_pl20) >= 0.01:
            err_msg = f"PL 20 内部抵消凭证借贷不平 (差额: {net_pl20})，强制阻断！"
            self.log_status("GATE_03A", "PL20凭证平衡门禁", "RED", err_msg)
            raise RuntimeError(err_msg)

        # 2. 检查经过 PL 20 抵消后，集团合并层面是否仍残留未抵消的内部往来余额 (112299 / 220299)
        residual_ic, = self.cursor.execute("""
            SELECT COALESCE(ROUND(SUM(ABS(amount_gc)), 2), 0)
            FROM (
                SELECT fs_item, SUM(amount_gc) AS amount_gc
                FROM fact_acdocu
                WHERE fiscal_period = ? AND fs_item IN ('112299', '220299')
                GROUP BY fs_item
            )
        """, (self.period,)).fetchone()

        if residual_ic >= 0.01:
            msg = f"检测到超容差未抵消内部往来残留余额: {residual_ic:,.2f} CNY (存在单边挂账)"
            if self.strict_ic_mode:
                self.log_status("GATE_03B", "内部往来清零门禁(严格模式)", "RED", msg + " -> 严格模式已强制阻断关账！")
                raise RuntimeError("关账阻断：" + msg)
            else:
                self.log_status("GATE_03B", "内部往来清零门禁(容忍模式)", "YELLOW", msg + " -> 已开启容忍放行，残留额将作为未达账项列报。")
        else:
            self.log_status("GATE_03B", "内部往来清零门禁", "GREEN", "集团内部应收/应付已 100% 清零。")

    # ==========================================================
    # 门禁 4：执行权益抵消 (PL 30)、全局总平衡校验并导出关账报表
    # ==========================================================
    def step4_coi_and_final_close(self):
        print("\n" + "="*80)
        print("🚀 STEP 4: 启动 COI 权益抵消 (PL 30) 与最终关账总校验")
        print("="*80)

        coi = ConsolidationOfInvestmentsEngine(self.db_path)
        coi.run_coi_elimination(self.period)

        # 全集团全层级 (PL 00 + 01 + 20 + 30) 终极试算平衡校验
        grand_total_diff, = self.cursor.execute("""
            SELECT COALESCE(ROUND(SUM(amount_gc), 2), 0)
            FROM fact_acdocu
            WHERE fiscal_period = ?
        """, (self.period,)).fetchone()

        if abs(grand_total_diff) >= 0.01:
            err_msg = f"终极校验失败！全集团合并总账借贷不平，差额: {grand_total_diff}"
            self.log_status("GATE_04", "全集团终极平衡门禁", "RED", err_msg)
            raise RuntimeError(err_msg)

        mode_tag = "FINAL_CLOSED" if self.strict_ic_mode else "PROVISIONAL_WITH_IC_DIFF"
        output_file = f"Consolidated_FS_{self.period}_{mode_tag}.xlsx"
        coi.export_consolidation_worksheet(self.period, output_file)
        
        self.log_status("GATE_04", "全集团终极平衡与报表发布", "GREEN", f"期间 {self.period} 合并关账完成！正式报表已生成: {output_file}")

    def execute_pipeline(self):
        print(f"🎬 开始执行集团合并自动化总控流水线 | 期间: {self.period} | 严格内部往来门禁: {self.strict_ic_mode}")
        try:
            self.step1_data_collection_and_gate1()
            self.step2_currency_translation_and_gate2()
            self.step3_ic_elimination_and_gate3()
            self.step4_coi_and_final_close()
            print("\n🏆 恭喜！全流程合并流水线执行完毕，所有门禁检查均已通过！")
        except RuntimeError as e:
            print("\n" + "!"*80)
            print(f"🛑 流水线执行中止 (SAP Consolidation Monitor 状态锁定)")
            print(f"阻断原因: {e}")
            print("💡 提示: 请在解决单边挂账差异后重跑，或将 strict_ic_mode 设为 False 以生成带未达账项的暂估合并报表。")
            print("!"*80 + "\n")
        finally:
            self.conn.close()

if __name__ == "__main__":
    # 第一次运行：体验 strict_ic_mode=True (严格模式) 下，系统如何在 Gate 3 精准拦截单边挂账并阻断关账！
    # 体验完拦截后，你可以把下面的 True 改为 False 再运行一次，观察黄灯放行并生成带标签的完整报表。
    pipeline = ConsolidationMasterPipeline(DB_FILE, FISCAL_PERIOD, strict_ic_mode=True)
    pipeline.execute_pipeline()
