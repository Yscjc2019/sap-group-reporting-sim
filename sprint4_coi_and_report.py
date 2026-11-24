import sqlite3
import pandas as pd

DB_FILE = "sap_consolidation.db"
FISCAL_PERIOD = "2026.01"
CONSOL_GROUP = "CG01"
OUTPUT_EXCEL = f"Consolidated_Financial_Statements_{FISCAL_PERIOD}.xlsx"

class ConsolidationOfInvestmentsEngine:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()

    def clear_existing_pl30(self, period):
        """幂等性清理：清除历史生成的 Posting Level 30 权益抵消分录"""
        self.cursor.execute("""
            DELETE FROM fact_acdocu 
            WHERE fiscal_period = ? AND posting_level = '30'
        """, (period,))

    def post_pl30_line(self, period, unit_id, partner_unit, fs_item, subitem, doc_type, amount_gc, remark):
        """写入一条 Posting Level 30 权益与合并范围抵消分录"""
        self.cursor.execute("""
            INSERT INTO fact_acdocu 
            (fiscal_period, consol_group, unit_id, partner_unit, fs_item, subitem, posting_level, doc_type, amount_lc, amount_gc, audit_remark)
            VALUES (?, ?, ?, ?, ?, ?, '30', ?, 0.0, ?, ?)
        """, (period, CONSOL_GROUP, unit_id, partner_unit, fs_item, subitem, doc_type, round(amount_gc, 2), remark))

    # ==========================================================
    # 模块 1：长期股权投资 vs 子公司所有者权益抵消 & 少数股东权益 (COI)
    # ==========================================================
    def run_coi_elimination(self, period):
        self.clear_existing_pl30(period)
        print("="*75)
        print("🏛️ 任务 1：SAP COI 权益抵消 (PL 30) 与少数股东损益/权益计算")
        print("="*75)

        subs = self.cursor.execute("""
            SELECT unit_id, unit_name, ownership_pct 
            FROM dim_unit 
            WHERE unit_id != 'C1000' AND consol_method = 'FULL'
        """).fetchall()

        for sub_id, sub_name, own_pct in subs:
            nci_pct = round(1.0 - own_pct, 4)
            
            # 1. 查询母公司 C1000 对该子公司的长期股权投资 (151100)
            inv_amt, = self.cursor.execute("""
                SELECT COALESCE(SUM(amount_gc), 0) FROM fact_acdocu
                WHERE fiscal_period = ? AND unit_id = 'C1000' AND partner_unit = ? AND fs_item = '151100'
            """, (period, sub_id)).fetchone()

            # 2. 查询该子公司的实收资本 (300100, 贷方为负)
            cap_amt, = self.cursor.execute("""
                SELECT COALESCE(SUM(amount_gc), 0) FROM fact_acdocu
                WHERE fiscal_period = ? AND unit_id = ? AND fs_item = '300100'
            """, (period, sub_id)).fetchone()

            if abs(inv_amt) > 0:
                print(f"\n📌 子公司 [{sub_id} - {sub_name}] | 母公司持股: {own_pct*100:.0f}% | 少数股东: {nci_pct*100:.0f}%")
                print(f"   母公司长投(借方): {inv_amt:,.2f} | 子公司实收资本(贷方): {cap_amt:,.2f}")

                self.post_pl30_line(period, sub_id, 'C1000', '300100', '900', 'COI_EQ', -cap_amt, f"抵消{sub_id}实收资本")
                self.post_pl30_line(period, 'C1000', sub_id, '151100', '900', 'COI_EQ', -inv_amt, f"抵消对{sub_id}长期股权投资")

                nci_cap = round(cap_amt * nci_pct, 2)
                if abs(nci_cap) > 0:
                    self.post_pl30_line(period, sub_id, 'NONE', '320100', '900', 'COI_NCI_EQ', nci_cap, f"确认{sub_id}少数股东初始权益({nci_pct*100:.0f}%)")

                goodwill = round(inv_amt + cap_amt * own_pct, 2)
                if abs(goodwill) >= 0.01:
                    self.post_pl30_line(period, sub_id, 'C1000', '170100', '900', 'COI_GW', goodwill, f"确认收购{sub_id}产生的商誉")
                    print(f"   ✨ 确认合并商誉: {goodwill:,.2f}")
                else:
                    print(f"   🟢 权益与长投全额对等抵消 (商誉: 0.00 | 少数股东初始权益: {-nci_cap:,.2f})")

            # 3. 计算非全资子公司当期损益中的少数股东损益 (NCI Net Income)
            if nci_pct > 0:
                net_income_credit, = self.cursor.execute("""
                    SELECT COALESCE(SUM(f.amount_gc), 0)
                    FROM fact_acdocu f
                    JOIN dim_fs_item i ON f.fs_item = i.fs_item
                    WHERE f.fiscal_period = ? AND f.unit_id = ? AND i.item_type IN ('INC', 'EXP')
                """, (period, sub_id)).fetchone()

                nci_pl_debit = round(-net_income_credit * nci_pct, 2)
                if abs(nci_pl_debit) >= 0.01:
                    self.post_pl30_line(period, sub_id, 'NONE', '690100', '910', 'COI_NCI_PL',  nci_pl_debit, f"归属{sub_id}少数股东损益({nci_pct*100:.0f}%)")
                    self.post_pl30_line(period, sub_id, 'NONE', '320100', '910', 'COI_NCI_PL', -nci_pl_debit, f"转入{sub_id}少数股东权益({nci_pct*100:.0f}%)")
                    print(f"   💰 当期净利润: {-net_income_credit:,.2f} -> 确认少数股东损益: {nci_pl_debit:,.2f} (同步增加少数股东权益)")

        self.conn.commit()

        # 第四道防线：校验 PL 30 自身借贷平衡
        net_pl30, = self.cursor.execute("""
            SELECT COALESCE(ROUND(SUM(amount_gc), 2), 0) FROM fact_acdocu
            WHERE fiscal_period = ? AND posting_level = '30'
        """, (period,)).fetchone()
        
        if abs(net_pl30) >= 0.01:
            status = f"🔴 红灯 (不平: {net_pl30})"
        else:
            status = "🟢 绿灯 (PL 30 权益抵消完全平衡)"
            
        print("\n" + "="*75)
        print(f"🚦 SAP Consolidation Monitor - PL 30 权益抵消层级平衡校验: {status}")
        print("="*75 + "\n")

    # ==========================================================
    # 模块 2：模拟 SAP Analysis for Office 导出多维合并工作底稿 Excel
    # ==========================================================
    def export_consolidation_worksheet(self, period, output_file):
        print(f"📊 正在生成集团合并财务报表工作底稿: {output_file} ...")

        df_all = pd.read_sql_query("""
            SELECT 
                f.id, f.fiscal_period, f.unit_id, f.partner_unit,
                f.fs_item, i.description AS fs_desc, i.item_type,
                f.subitem, f.posting_level, f.doc_type,
                f.amount_lc, f.amount_gc, f.audit_remark
            FROM fact_acdocu f
            JOIN dim_fs_item i ON f.fs_item = i.fs_item
            WHERE f.fiscal_period = ?
        """, self.conn, params=(period,))

        df_entities = df_all[df_all['posting_level'].isin(['00', '01'])].pivot_table(
            index=['item_type', 'fs_item', 'fs_desc'],
            columns='unit_id',
            values='amount_gc',
            aggfunc='sum',
            fill_value=0.0
        )

        df_elims = df_all[df_all['posting_level'].isin(['20', '30'])].pivot_table(
            index=['item_type', 'fs_item', 'fs_desc'],
            columns='posting_level',
            values='amount_gc',
            aggfunc='sum',
            fill_value=0.0
        ).rename(columns={'20': '内部交易抵消(PL20)', '30': '权益及少数股东抵消(PL30)'})

        worksheet = df_entities.join(df_elims, how='outer').fillna(0.0)

        entity_cols = [c for c in ['C1000', 'C2000', 'C3000', 'C4000'] if c in worksheet.columns]
        elim_cols = [c for c in ['内部交易抵消(PL20)', '权益及少数股东抵消(PL30)'] if c in worksheet.columns]
        
        worksheet['单体简单加总'] = worksheet[entity_cols].sum(axis=1)
        worksheet['集团最终合并数(CNY)'] = worksheet['单体简单加总'] + worksheet[elim_cols].sum(axis=1)

        final_cols = entity_cols + ['单体简单加总'] + elim_cols + ['集团最终合并数(CNY)']
        worksheet = worksheet[final_cols].reset_index()

        # 按财务报表标准顺序排序 (AST -> LEQ -> INC -> EXP)
        type_order = {'AST': 1, 'LEQ': 2, 'INC': 3, 'EXP': 4}
        worksheet['sort_key'] = worksheet['item_type'].map(type_order)
        worksheet = worksheet.sort_values(['sort_key', 'fs_item']).drop(columns=['sort_key'])

        # 添加底部总平衡校验行
        total_row = {'item_type': 'CHECK', 'fs_item': 'TOTAL', 'fs_desc': '试算平衡总校验 (借正贷负合计应为0)'}
        for col in final_cols:
            total_row[col] = round(worksheet[col].sum(), 2)
        worksheet = pd.concat([worksheet, pd.DataFrame([total_row])], ignore_index=True)

        with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
            worksheet.to_excel(writer, sheet_name='合并工作底稿(Audit_Trail)', index=False)
            df_all.to_excel(writer, sheet_name='ACDOCU底层凭证流(明细)', index=False)

        print(f"✅ 合并报表底稿导出成功！请打开项目根目录下的 [{output_file}] 查看完整报表。")
        print("\n--- 合并工作底稿核心摘要预览 ---")
        print(worksheet[['fs_item', 'fs_desc', '单体简单加总'] + elim_cols + ['集团最终合并数(CNY)']].to_string(index=False))
        self.conn.close()

if __name__ == "__main__":
    coi = ConsolidationOfInvestmentsEngine(DB_FILE)
    coi.run_coi_elimination(FISCAL_PERIOD)
    coi.export_consolidation_worksheet(FISCAL_PERIOD, OUTPUT_EXCEL)