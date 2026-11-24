import sqlite3
import pandas as pd

DB_FILE = "sap_consolidation.db"
FISCAL_PERIOD = "2026.01"
CONSOL_GROUP = "CG01"

class DynamicExpansionManager:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()

    # ==========================================================
    # 阶段 1：动态扩展主数据 (追加新科目 + 新增权益法联营企业)
    # ==========================================================
    def expand_master_data_and_transactions(self):
        print("="*75)
        print("🛠️ 阶段 1：动态扩展集团主数据 (追加新科目 611100 & 新联营企业 C5000)")
        print("="*75)

        # 1. 追加新合并报表项目: 611100 投资收益 (INC类, 平均汇率折算, 非内部往来)
        self.cursor.execute("""
            INSERT OR REPLACE INTO dim_fs_item (fs_item, description, item_type, ct_method, is_ic_relevant)
            VALUES ('611100', '投资收益(权益法)', 'INC', 'AVG', 0)
        """)
        print("✅ 已在 dim_fs_item 追加新报表项目: 611100 - 投资收益(权益法)")

        # 2. 追加 SAP 总账科目映射规则
        self.cursor.execute("""
            DELETE FROM map_gl_to_fs WHERE source_system = 'SAP_COA' AND source_code = '61110100'
        """)
        self.cursor.execute("""
            INSERT INTO map_gl_to_fs (source_system, source_code, fs_item, default_subitem)
            VALUES ('SAP_COA', '61110100', '611100', '910')
        """)
        print("✅ 已在 map_gl_to_fs 追加总账科目映射: 61110100 -> 611100")

        # 3. 新增联营企业 C5000 (持股 30%, 合并方法为 EQUITY 权益法)
        self.cursor.execute("""
            INSERT OR REPLACE INTO dim_unit (unit_id, unit_name, local_curr, source_type, ownership_pct, consol_method)
            VALUES ('C5000', '联营科技公司(杭州)', 'CNY', 'S4_DIRECT', 0.30, 'EQUITY')
        """)
        print("✅ 已在 dim_unit 新增合并单元: C5000 (持股 30% | 合并方法: EQUITY)")

        # 4. 插入模拟业务凭证 (先清理防重复)
        self.cursor.execute("DELETE FROM src_s4_acdoca WHERE doc_no IN ('DOC1004', 'DOC5001', 'DOC5002')")
        
        new_entries = [
            # 母公司 C1000 对联营企业 C5000 投资 30万 (占股 30%)
            ('DOC1004', 1, '2026.01', 'C1000', '15110100', 'C5000', '900',  300000.0, '对联营企业C5000长期股权投资'),
            ('DOC1004', 2, '2026.01', 'C1000', '10020100', 'NONE',  '910', -300000.0, '支付C5000投资款'),
            
            # 联营企业 C5000 自身单体账务: 注册资本 100万, 当期实现收入 25万, 费用 15万 (净利润 10万)
            ('DOC5001', 1, '2026.01', 'C5000', '10020100', 'NONE',  '910',  1000000.0, '收到股东出资款'),
            ('DOC5001', 2, '2026.01', 'C5000', '40010100', 'NONE',  '900', -1000000.0, '确认实收资本'),
            ('DOC5002', 1, '2026.01', 'C5000', '11220100', 'NONE',  '910',   250000.0, '技术服务收入应收款'),
            ('DOC5002', 2, '2026.01', 'C5000', '60010100', 'NONE',  '910',  -250000.0, '确认主营业务收入'),
            ('DOC5002', 3, '2026.01', 'C5000', '66020100', 'NONE',  '910',   150000.0, '研发及管理费用'),
            ('DOC5002', 4, '2026.01', 'C5000', '10020100', 'NONE',  '910',  -150000.0, '支付费用款项')
        ]
        self.cursor.executemany("INSERT INTO src_s4_acdoca VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", new_entries)
        self.conn.commit()
        print("✅ 已写入 C1000 投资凭证及 C5000 单体业务凭证 (借贷严格平衡)。\n")

    # ==========================================================
    # 阶段 2：权益法单行合并计算引擎 (Equity Method Pickup)
    # ==========================================================
    def run_equity_method_consolidation(self, period):
        print("="*75)
        print("⚖️ 阶段 2：执行权益法 (EQUITY) 单行合并计算与借贷平衡校验")
        print("="*75)

        # 1. 重新释放 C1000 (因为新增了 DOC1004 投资分录)
        self.cursor.execute("DELETE FROM fact_acdocu WHERE fiscal_period = ? AND unit_id = 'C1000' AND posting_level = '00'", (period,))
        self.cursor.execute("""
            INSERT INTO fact_acdocu (fiscal_period, consol_group, unit_id, partner_unit, fs_item, subitem, posting_level, doc_type, amount_lc, amount_gc, audit_remark)
            SELECT 
                a.fiscal_period, ?, a.unit_id, a.partner_unit, m.fs_item, a.subitem,
                '00', 'REL_S4', SUM(a.amount_lc), SUM(a.amount_lc), 'SAP S/4HANA 实时通用日记账释放(含新增投资)'
            FROM src_s4_acdoca a
            JOIN map_gl_to_fs m ON a.gl_account = m.source_code AND m.source_system = 'SAP_COA'
            WHERE a.unit_id = 'C1000' AND a.fiscal_period = ?
            GROUP BY a.fiscal_period, a.unit_id, a.partner_unit, m.fs_item, a.subitem
        """, (CONSOL_GROUP, period))

        # 2. 清理历史权益法调整分录 (doc_type = 'EQ_PICKUP')
        self.cursor.execute("DELETE FROM fact_acdocu WHERE fiscal_period = ? AND doc_type = 'EQ_PICKUP'", (period,))

        # 3. 扫描所有合并方法为 'EQUITY' 的联营/合营企业
        eq_units = self.cursor.execute("""
            SELECT unit_id, unit_name, ownership_pct 
            FROM dim_unit 
            WHERE consol_method = 'EQUITY'
        """).fetchall()

        for eq_id, eq_name, own_pct in eq_units:
            # 直接从联营企业单体账簿计算当期净利润 (注意：不将联营企业明细行写入 fact_acdocu!)
            # 收入在源表为负，费用为正；净利润(贷方为负) = SUM(INC + EXP)
            net_income_credit, = self.cursor.execute("""
                SELECT COALESCE(SUM(a.amount_lc), 0)
                FROM src_s4_acdoca a
                JOIN map_gl_to_fs m ON a.gl_account = m.source_code AND m.source_system = 'SAP_COA'
                JOIN dim_fs_item i ON m.fs_item = i.fs_item
                WHERE a.fiscal_period = ? AND a.unit_id = ? AND i.item_type IN ('INC', 'EXP')
            """, (period, eq_id)).fetchone()

            net_profit = -net_income_credit # 转为正数表示盈利
            pickup_amt = round(net_profit * own_pct, 2)

            print(f"📌 联营企业 [{eq_id} - {eq_name}] | 持股比例: {own_pct*100:.0f}%")
            print(f"   单体当期净利润: {net_profit:,.2f} CNY -> 集团按持股比例享有投资收益: {pickup_amt:,.2f} CNY")
            print(f"   ℹ️ 系统规则检查: 合并方法为 EQUITY，跳过资产/负债/收入/费用逐行并入，执行单行合并。")

            if abs(pickup_amt) >= 0.01:
                # 在母公司 C1000 名下生成 PL 30 权益法调整凭证:
                # 借: 长期股权投资 (151100) +30,000
                # 贷: 投资收益-权益法 (611100) -30,000
                self.cursor.execute("""
                    INSERT INTO fact_acdocu 
                    (fiscal_period, consol_group, unit_id, partner_unit, fs_item, subitem, posting_level, doc_type, amount_lc, amount_gc, audit_remark)
                    VALUES (?, ?, 'C1000', ?, '151100', '910', '30', 'EQ_PICKUP', 0.0, ?, ?)
                """, (period, CONSOL_GROUP, eq_id, pickup_amt, f"权益法确认享有{eq_id}净损益({own_pct*100:.0f}%)"))

                self.cursor.execute("""
                    INSERT INTO fact_acdocu 
                    (fiscal_period, consol_group, unit_id, partner_unit, fs_item, subitem, posting_level, doc_type, amount_lc, amount_gc, audit_remark)
                    VALUES (?, ?, 'C1000', ?, '611100', '910', '30', 'EQ_PICKUP', 0.0, ?, ?)
                """, (period, CONSOL_GROUP, eq_id, -pickup_amt, f"权益法确认对{eq_id}投资收益({own_pct*100:.0f}%)"))

        self.conn.commit()

        # ==========================================================
        # 阶段 3：主数据扩展后的全集团多维度平衡与完整性测试
        # ==========================================================
        print("\n" + "="*75)
        print("🚦 阶段 3：主数据扩展后全集团试算平衡与会计恒等式测试")
        print("="*75)

        # 测试 1：所有总账科目映射完整性检查 (防止新增科目漏配映射)
        unmapped_count, = self.cursor.execute("""
            SELECT COUNT(*) FROM src_s4_acdoca a
            LEFT JOIN map_gl_to_fs m ON a.gl_account = m.source_code AND m.source_system = 'SAP_COA'
            WHERE m.fs_item IS NULL
        """).fetchone()
        print(f"1. 科目映射完整性校验: {'🟢 绿灯 (0 个未映射科目)' if unmapped_count == 0 else f'🔴 红灯 (发现 {unmapped_count} 条未映射记录)'}")

        # 测试 2：全集团合并底层表 (fact_acdocu) 借贷总平衡校验
        grand_diff, = self.cursor.execute("""
            SELECT COALESCE(ROUND(SUM(amount_gc), 2), 0) FROM fact_acdocu WHERE fiscal_period = ?
        """, (period,)).fetchone()
        print(f"2. 全集团借贷总平衡校验: {'🟢 绿灯 (差额: 0.00)' if abs(grand_diff) < 0.01 else f'🔴 红灯 (不平差额: {grand_diff})'}")

        # 测试 3：四大报表要素平衡校验 (资产 + 负债权益 + 收入 + 费用 == 0)
        df_elements = pd.read_sql_query("""
            SELECT i.item_type, ROUND(SUM(f.amount_gc), 2) AS total_gc
            FROM fact_acdocu f
            JOIN dim_fs_item i ON f.fs_item = i.fs_item
            WHERE f.fiscal_period = ?
            GROUP BY i.item_type
        """, self.conn, params=(period,))
        print("\n--- 扩展后集团四大财务报表要素汇总 (借正贷负) ---")
        print(df_elements.to_string(index=False))

        # 导出最新完整合并底稿验证新科目 611100 的列报效果
        from sprint4_coi_and_report import ConsolidationOfInvestmentsEngine
        exporter = ConsolidationOfInvestmentsEngine(DB_FILE)
        exporter.export_consolidation_worksheet(period, f"Consolidated_FS_{period}_EXPANDED.xlsx")
        self.conn.close()

if __name__ == "__main__":
    mgr = DynamicExpansionManager(DB_FILE)
    mgr.expand_master_data_and_transactions()
    mgr.run_equity_method_consolidation(FISCAL_PERIOD)
