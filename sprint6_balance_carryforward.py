import sqlite3
import pandas as pd

DB_FILE = "sap_consolidation.db"
SRC_PERIOD = "2026.01"
DST_PERIOD = "2027.01"
CONSOL_GROUP = "CG01"

class BalanceCarryforwardEngine:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()

    def clear_target_period(self, dst_period):
        """幂等性清理：重跑跨年结转前清除目标年度(2027.01)的旧结转记录"""
        self.cursor.execute("DELETE FROM fact_acdocu WHERE fiscal_period = ?", (dst_period,))

    # ==========================================================
    # 任务 1：执行 SAP Balance Carryforward (资产负债结转 + 损益滚入未分配利润)
    # ==========================================================
    def run_bcf(self, src_period, dst_period):
        self.clear_target_period(dst_period)
        print("="*78)
        print(f"⏭️ 任务 1：执行跨年度余额结转 ({src_period} 期末 -> {dst_period} 期初 Subitem 900)")
        print("="*78)

        # 1. 资产负债类科目 (AST, LEQ) 按实体、伙伴、科目、过账层级原样结转至 Subitem 900
        self.cursor.execute("""
            INSERT INTO fact_acdocu 
            (fiscal_period, consol_group, unit_id, partner_unit, fs_item, subitem, posting_level, doc_type, amount_lc, amount_gc, audit_remark)
            SELECT 
                ?, f.consol_group, f.unit_id, f.partner_unit, f.fs_item,
                '900' AS subitem,
                f.posting_level,
                'BCF_BS' AS doc_type,
                ROUND(SUM(f.amount_lc), 2),
                ROUND(SUM(f.amount_gc), 2),
                '跨年结转: 资产负债科目结转至次年期初(Subitem 900)'
            FROM fact_acdocu f
            JOIN dim_fs_item i ON f.fs_item = i.fs_item
            WHERE f.fiscal_period = ? AND i.item_type IN ('AST', 'LEQ')
            GROUP BY f.consol_group, f.unit_id, f.partner_unit, f.fs_item, f.posting_level
            HAVING ABS(SUM(f.amount_gc)) >= 0.01 OR ABS(SUM(f.amount_lc)) >= 0.01
        """, (dst_period, src_period))
        print(f"✅ 资产负债类科目 (AST/LEQ) 结转完成，写入 {self.cursor.rowcount} 条期初记录。")

        # 2. 损益类科目 (INC, EXP) 全部清零，按 (unit_id, posting_level) 汇总滚入 310300 (未分配利润)
        self.cursor.execute("""
            INSERT INTO fact_acdocu 
            (fiscal_period, consol_group, unit_id, partner_unit, fs_item, subitem, posting_level, doc_type, amount_lc, amount_gc, audit_remark)
            SELECT 
                ?, f.consol_group, f.unit_id, 'NONE' AS partner_unit,
                '310300' AS fs_item,
                '900' AS subitem,
                f.posting_level,
                'BCF_PL' AS doc_type,
                ROUND(SUM(f.amount_lc), 2),
                ROUND(SUM(f.amount_gc), 2),
                '跨年结转: 上年损益(INC/EXP)自动结转至年初未分配利润'
            FROM fact_acdocu f
            JOIN dim_fs_item i ON f.fs_item = i.fs_item
            WHERE f.fiscal_period = ? AND i.item_type IN ('INC', 'EXP')
            GROUP BY f.consol_group, f.unit_id, f.posting_level
            HAVING ABS(SUM(f.amount_gc)) >= 0.01 OR ABS(SUM(f.amount_lc)) >= 0.01
        """, (dst_period, src_period))
        print(f"✅ 损益类科目 (INC/EXP) 结转完成，已按过账层级滚入 310300 年初未分配利润 (共 {self.cursor.rowcount} 条)。")

        self.conn.commit()

    # ==========================================================
    # 任务 2：期初存货未实现内部毛利自动冲回 (Prior-Year UPI Reversal)
    # ==========================================================
    def reverse_prior_year_upi(self, dst_period):
        print("\n" + "="*78)
        print(f"🔄 任务 2：检测期初存货未实现内部毛利 (UPI) 并生成 {dst_period} 自动冲回凭证")
        print("="*78)

        # 查找 2027.01 期初 (Subitem 900) 中，PL 20 层级下库存商品 (140500) 的历史抵消余额
        upi_rows = self.cursor.execute("""
            SELECT unit_id, partner_unit, ROUND(SUM(amount_gc), 2) AS carried_upi
            FROM fact_acdocu
            WHERE fiscal_period = ? AND posting_level = '20' AND subitem = '900' AND fs_item = '140500'
            GROUP BY unit_id, partner_unit
            HAVING ABS(SUM(amount_gc)) >= 0.01
        """, (dst_period,)).fetchall()

        for buyer_id, seller_id, carried_upi in upi_rows:
            # carried_upi 在期初为贷方负数 (-22,500.00)，冲回金额为正数 (+22,500.00)
            rev_amt = round(-carried_upi, 2)
            print(f"📌 发现期初未实现存货毛利抵消余额: 买方 [{buyer_id}] <- 卖方 [{seller_id}] | 期初抵消额: {carried_upi:,.2f}")
            print(f"   ✨ 自动生成 {dst_period} 本期 (Subitem 910) 冲回分录: 借 140500 库存商品 +{rev_amt:,.2f} / 贷 640199 营业成本 -{rev_amt:,.2f}")

            # 借: 库存商品 (140500) +22,500 (恢复集团层面存货账面价值，随本期对外销售结转成本)
            self.cursor.execute("""
                INSERT INTO fact_acdocu 
                (fiscal_period, consol_group, unit_id, partner_unit, fs_item, subitem, posting_level, doc_type, amount_lc, amount_gc, audit_remark)
                VALUES (?, ?, ?, ?, '140500', '910', '20', 'IC_UPI_REV', 0.0, ?, ?)
            """, (dst_period, CONSOL_GROUP, buyer_id, seller_id, rev_amt, f"次年年初自动冲回上年留存存货未实现毛利({buyer_id})"))

            # 贷: 营业成本-内部关联方 (640199) -22,500 (冲减次年营业成本，实现跨期毛利归属)
            self.cursor.execute("""
                INSERT INTO fact_acdocu 
                (fiscal_period, consol_group, unit_id, partner_unit, fs_item, subitem, posting_level, doc_type, amount_lc, amount_gc, audit_remark)
                VALUES (?, ?, ?, ?, '640199', '910', '20', 'IC_UPI_REV', 0.0, ?, ?)
            """, (dst_period, CONSOL_GROUP, buyer_id, seller_id, -rev_amt, f"次年年初冲回上年未实现毛利调减本期成本({buyer_id})"))

        self.conn.commit()

    # ==========================================================
    # 任务 3：校验 2027.01 跨年结转后全层级试算平衡并导出期初工作底稿
    # ==========================================================
    def validate_and_export(self, dst_period):
        print("\n" + "="*78)
        print(f"🚦 任务 3：SAP Consolidation Monitor - {dst_period} 跨年结转与冲回平衡校验")
        print("="*78)

        df_check = pd.read_sql_query("""
            SELECT posting_level, subitem, ROUND(SUM(amount_gc), 2) AS net_gc
            FROM fact_acdocu
            WHERE fiscal_period = ?
            GROUP BY posting_level, subitem
        """, self.conn, params=(dst_period,))
        print(df_check.to_string(index=False))

        grand_diff, = self.cursor.execute("""
            SELECT COALESCE(ROUND(SUM(amount_gc), 2), 0) FROM fact_acdocu WHERE fiscal_period = ?
        """, (dst_period,)).fetchone()

        if abs(grand_diff) >= 0.01:
            print(f"\n🔴 红灯警报！{dst_period} 结转后借贷不平，差额: {grand_diff}")
        else:
            print(f"\n🟢 绿灯通过！{dst_period} 跨年结转及期初存货毛利冲回后全集团借贷严格平衡 (0.00)。")

        from sprint4_coi_and_report import ConsolidationOfInvestmentsEngine
        exporter = ConsolidationOfInvestmentsEngine(DB_FILE)
        exporter.export_consolidation_worksheet(dst_period, f"Consolidated_FS_{dst_period}_BCF_OPENING.xlsx")
        self.conn.close()

if __name__ == "__main__":
    bcf = BalanceCarryforwardEngine(DB_FILE)
    bcf.run_bcf(SRC_PERIOD, DST_PERIOD)
    bcf.reverse_prior_year_upi(DST_PERIOD)
    bcf.validate_and_export(DST_PERIOD)
