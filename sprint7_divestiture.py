import sqlite3
import pandas as pd

DB_FILE = "sap_consolidation.db"
PERIOD = "2027.01"
CONSOL_GROUP = "CG01"
DIVESTED_UNIT = "C2000"
SALE_PROCEEDS = 1200000.00  # 母公司以 120万元 出售 C2000 100%股权

class DivestitureEngine:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()

    def clear_existing_divestiture(self, period):
        """幂等性清理：重跑处置前清除历史生成的处置分录"""
        self.cursor.execute("""
            DELETE FROM fact_acdocu 
            WHERE fiscal_period = ? AND doc_type IN ('DIV_PARENT', 'COI_DIV_EXIT', 'IC_DECONSOL')
        """, (period,))

    # ==========================================================
    # 步骤 1：模拟母公司 C1000 单体账务记录出售子公司交易 (PL 00)
    # ==========================================================
    def record_parent_sale_entry(self, period, sub_id, proceeds):
        print("="*78)
        print(f"💼 步骤 1：母公司 (C1000) 单体确认出售 [{sub_id}] 股权交易 (对价: {proceeds:,.2f})")
        print("="*78)

        cost_basis, = self.cursor.execute("""
            SELECT COALESCE(SUM(amount_gc), 0) FROM fact_acdocu
            WHERE fiscal_period = ? AND unit_id = 'C1000' AND partner_unit = ? 
              AND fs_item = '151100' AND posting_level = '00'
        """, (period, sub_id)).fetchone()

        parent_gain = round(proceeds - cost_basis, 2)
        print(f"   母公司账面长投成本: {cost_basis:,.2f} | 出售对价: {proceeds:,.2f} | 单体投资收益: {parent_gain:,.2f}")

        entries = [
            (period, CONSOL_GROUP, 'C1000', 'NONE', '100100', '910', '00', 'DIV_PARENT', proceeds, proceeds, f"收到出售{sub_id}股权款"),
            (period, CONSOL_GROUP, 'C1000', sub_id, '151100', '910', '00', 'DIV_PARENT', -cost_basis, -cost_basis, f"结转出售{sub_id}长投成本"),
            (period, CONSOL_GROUP, 'C1000', sub_id, '611100', '910', '00', 'DIV_PARENT', -parent_gain, -parent_gain, f"单体确认出售{sub_id}投资收益")
        ]
        self.cursor.executemany("""
            INSERT INTO fact_acdocu 
            (fiscal_period, consol_group, unit_id, partner_unit, fs_item, subitem, posting_level, doc_type, amount_lc, amount_gc, audit_remark)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, entries)
        return cost_basis, parent_gain

    # ==========================================================
    # 步骤 2：SAP COI 合并范围退出 (Scope Exit, Subitem 990) 与重算合并处置损益
    # ==========================================================
    def execute_deconsolidation(self, period, sub_id, proceeds, cost_basis, parent_gain):
        print("\n" + "="*78)
        print(f"🚪 步骤 2：SAP COI 执行 [{sub_id}] 移出合并范围 (Scope Exit - Subitem 990)")
        print("="*78)

        self.cursor.execute("""
            UPDATE dim_unit SET consol_method = 'DIVESTED' WHERE unit_id = ?
        """, (sub_id,))
        print(f"✅ 主数据状态更新: dim_unit 中 [{sub_id}] 已标记为 DIVESTED (退出合并范围)。")

        # 1. 冲回母公司期末处置后不应再保留的期初历史长投抵消分录
        self.cursor.execute("""
            INSERT INTO fact_acdocu 
            (fiscal_period, consol_group, unit_id, partner_unit, fs_item, subitem, posting_level, doc_type, amount_lc, amount_gc, audit_remark)
            VALUES (?, ?, 'C1000', ?, '151100', '990', '30', 'COI_DIV_EXIT', 0.0, ?, ?)
        """, (period, CONSOL_GROUP, sub_id, cost_basis, f"处置退出: 冲回对{sub_id}历史长投抵消分录"))

        # 2. 全面清零被处置子公司 (C2000) 在合并工作底稿上的所有科目残留余额
        sub_balances = self.cursor.execute("""
            SELECT fs_item, partner_unit, ROUND(SUM(amount_gc), 2) AS net_bal
            FROM fact_acdocu
            WHERE fiscal_period = ? AND unit_id = ?
            GROUP BY fs_item, partner_unit
            HAVING ABS(SUM(amount_gc)) >= 0.01
        """, (period, sub_id)).fetchall()

        print(f"\n--- 正在生成 Subitem 990 退出冲销分录 (清零 {sub_id} 全部合并底稿余额) ---")
        strip_total = 0.0
        for fs_item, partner, net_bal in sub_balances:
            self.cursor.execute("""
                INSERT INTO fact_acdocu 
                (fiscal_period, consol_group, unit_id, partner_unit, fs_item, subitem, posting_level, doc_type, amount_lc, amount_gc, audit_remark)
                VALUES (?, ?, ?, ?, ?, '990', '30', 'COI_DIV_EXIT', 0.0, ?, ?)
            """, (period, CONSOL_GROUP, sub_id, partner, fs_item, -net_bal, f"处置退出(Subitem 990): 剥离{sub_id}期末科目余额"))
            strip_total = round(strip_total - net_bal, 2)
            print(f"   剥离科目 {fs_item} (伙伴: {partner}) | 原余额: {net_bal:>11,.2f} -> 退出冲销: {-net_bal:>11,.2f}")

        # 3. 严格依据复式平衡恒等式，自动推导合并层面投资收益调整额
        # 退出凭证中: 冲回长投抵消(+cost_basis) + 剥离子公司净余额(strip_total) + 投资收益调整(gain_adjustment) == 0
        gain_adjustment = round(-(cost_basis + strip_total), 2)
        consol_div_gain = round(parent_gain - gain_adjustment, 2)

        print("\n--- 合并层面投资处置损益重算逻辑 ---")
        print(f"   母公司单体确认处置收益: {parent_gain:,.2f}")
        print(f"   合并层面重算真实处置收益: {consol_div_gain:,.2f}")
        print(f"   自动生成投资收益调整分录: 借 611100 投资收益 +{gain_adjustment:,.2f}")

        self.cursor.execute("""
            INSERT INTO fact_acdocu 
            (fiscal_period, consol_group, unit_id, partner_unit, fs_item, subitem, posting_level, doc_type, amount_lc, amount_gc, audit_remark)
            VALUES (?, ?, 'C1000', ?, '611100', '990', '30', 'COI_DIV_EXIT', 0.0, ?, ?)
        """, (period, CONSOL_GROUP, sub_id, gain_adjustment, f"处置退出: 调整母公司单体投资收益为合并处置收益({consol_div_gain:,.2f})"))

        # 4. 还原 C1000 对已退出公司 C2000 的残留内部应收款为外部应收款
        c1000_ic_ar, = self.cursor.execute("""
            SELECT COALESCE(ROUND(SUM(amount_gc), 2), 0) FROM fact_acdocu
            WHERE fiscal_period = ? AND unit_id = 'C1000' AND partner_unit = ? AND fs_item = '112299'
        """, (period, sub_id)).fetchone()

        if abs(c1000_ic_ar) >= 0.01:
            self.cursor.execute("""
                INSERT INTO fact_acdocu 
                (fiscal_period, consol_group, unit_id, partner_unit, fs_item, subitem, posting_level, doc_type, amount_lc, amount_gc, audit_remark)
                VALUES (?, ?, 'C1000', ?, '112299', '990', '30', 'IC_DECONSOL', 0.0, ?, ?)
            """, (period, CONSOL_GROUP, sub_id, -c1000_ic_ar, f"出表重分类: 冲减原对{sub_id}内部应收"))
            self.cursor.execute("""
                INSERT INTO fact_acdocu 
                (fiscal_period, consol_group, unit_id, partner_unit, fs_item, subitem, posting_level, doc_type, amount_lc, amount_gc, audit_remark)
                VALUES (?, ?, 'C1000', 'NONE', '112200', '990', '30', 'IC_DECONSOL', 0.0, ?, ?)
            """, (period, CONSOL_GROUP, c1000_ic_ar, f"出表重分类: 原对{sub_id}应收转为外部应收"))
            print(f"   🔄 [关联方出表重分类] 原 C1000 对 {sub_id} 内部应收 {c1000_ic_ar:,.2f} 已转为外部应收(112200)。")

        self.conn.commit()

    # ==========================================================
    # 步骤 3：校验处置后全集团借贷总平衡与子公司的“零余额”退出验证
    # ==========================================================
    def validate_and_export(self, period, sub_id):
        print("\n" + "="*78)
        print(f"🚦 步骤 3：SAP Consolidation Monitor - 实体处置后平衡与退出验证 ({period})")
        print("="*78)

        sub_residual, = self.cursor.execute("""
            SELECT COALESCE(ROUND(SUM(ABS(bal)), 2), 0)
            FROM (
                SELECT fs_item, SUM(amount_gc) AS bal
                FROM fact_acdocu WHERE fiscal_period = ? AND unit_id = ?
                GROUP BY fs_item
            )
        """, (period, sub_id)).fetchone()
        
        if sub_residual >= 0.01:
            print(f"1. 已处置实体 [{sub_id}] 合并余额清零检查: 🔴 红灯 (残留: {sub_residual})")
        else:
            print(f"1. 已处置实体 [{sub_id}] 合并余额清零检查: 🟢 绿灯 (全部资产负债权益已彻底出表归零)")

        grand_diff, = self.cursor.execute("""
            SELECT COALESCE(ROUND(SUM(amount_gc), 2), 0) FROM fact_acdocu WHERE fiscal_period = ?
        """, (period,)).fetchone()
        
        if abs(grand_diff) >= 0.01:
            print(f"2. 处置后全集团试算平衡总校验: 🔴 红灯 (不平差额: {grand_diff})")
        else:
            print("2. 处置后全集团试算平衡总校验: 🟢 绿灯 (差额: 0.00)")

        from sprint4_coi_and_report import ConsolidationOfInvestmentsEngine
        exporter = ConsolidationOfInvestmentsEngine(DB_FILE)
        exporter.export_consolidation_worksheet(period, f"Consolidated_FS_{period}_AFTER_DIVESTITURE.xlsx")
        self.conn.close()

if __name__ == "__main__":
    div_engine = DivestitureEngine(DB_FILE)
    div_engine.clear_existing_divestiture(PERIOD)
    cost, gain = div_engine.record_parent_sale_entry(PERIOD, DIVESTED_UNIT, SALE_PROCEEDS)
    div_engine.execute_deconsolidation(PERIOD, DIVESTED_UNIT, SALE_PROCEEDS, cost, gain)
    div_engine.validate_and_export(PERIOD, DIVESTED_UNIT)
