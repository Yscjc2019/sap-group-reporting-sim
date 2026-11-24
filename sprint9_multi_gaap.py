import sqlite3
import pandas as pd

DB_FILE = "sap_consolidation.db"
PERIOD = "2026.01"
CONSOL_GROUP = "CG01"
OUTPUT_EXCEL = f"Consolidated_MultiGAAP_CAS_vs_IFRS_{PERIOD}.xlsx"

class MultiGAAPConsolidationEngine:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()

    # ==========================================================
    # 步骤 1：动态升级表结构，引入 accounting_principle 维度与新科目
    # ==========================================================
    def upgrade_schema_for_multigaap(self):
        print("="*80)
        print("🌐 步骤 1：升级 ACDOCU 核心表结构，引入 accounting_principle (并行准则) 维度")
        print("="*80)

        # 检查 fact_acdocu 是否已存在 accounting_principle 列，不存在则动态 ALTER TABLE 追加
        cols = [info[1] for info in self.cursor.execute("PRAGMA table_info(fact_acdocu)").fetchall()]
        if 'accounting_principle' not in cols:
            self.cursor.execute("""
                ALTER TABLE fact_acdocu 
                ADD COLUMN accounting_principle TEXT DEFAULT 'COMMON'
            """)
            print("✅ 已在 fact_acdocu 成功追加字段: accounting_principle (默认值: 'COMMON')")
        else:
            print("ℹ️ 字段 accounting_principle 已存在，跳过建表结构变更。")

        # 确保历史存量数据均标记为 COMMON (CAS与IFRS共用底层)
        self.cursor.execute("UPDATE fact_acdocu SET accounting_principle = 'COMMON' WHERE accounting_principle IS NULL")

        # 追加 IFRS 差异所需的新报表项目: 170200 无形资产(研发资本化)
        self.cursor.execute("""
            INSERT OR REPLACE INTO dim_fs_item (fs_item, description, item_type, ct_method, is_ic_relevant)
            VALUES ('170200', '无形资产-研发资本化', 'AST', 'HIS', 0)
        """)
        self.conn.commit()
        print("✅ 已配置多准则映射项目: 170200 (无形资产-研发资本化)。\n")

    # ==========================================================
    # 步骤 2：生成 IFRS 准则专属增量调整分录 (PL 10, IFRS_DELTA)
    # ==========================================================
    def generate_ifrs_delta_adjustments(self, period):
        print("="*80)
        print("⚖️ 步骤 2：生成 IFRS 准则标准化增量调整凭证 (Posting Level 10 | IFRS_DELTA)")
        print("="*80)

        # 幂等性清理：清除历史生成的 IFRS_DELTA 调整记录
        self.cursor.execute("""
            DELETE FROM fact_acdocu 
            WHERE fiscal_period = ? AND accounting_principle = 'IFRS_DELTA'
        """, (period,))

        delta_entries = []

        # ----------------------------------------------------------
        # 差异场景 1：研发支出资本化差异 (IAS 38 vs CAS 6号)
        # C4000 发生研发支出 100,000，CAS 下全额计入管理费用，IFRS 下 60,000 资本化为无形资产
        # 注意：因 C4000 为 80% 持股子公司，利润增加 60,000 还会连带影响少数股东损益 20% (12,000)!
        # ----------------------------------------------------------
        rd_cap_amt = 60000.00
        nci_rd_adj = round(rd_cap_amt * 0.20, 2)  # 12,000.00

        print(f"📌 [IFRS 差异 1 - IAS 38 研发资本化]:")
        print(f"   C4000 调减管理费用(660200) -{rd_cap_amt:,.2f}，确认无形资产(170200) +{rd_cap_amt:,.2f}")
        print(f"   连带影响：C4000 净利润增加 60,000，自动补提 20% 少数股东损益(690100) +{nci_rd_adj:,.2f}")

        delta_entries.extend([
            # PL 10 准则标准化调整
            (period, CONSOL_GROUP, 'C4000', 'NONE', '170200', '910', '10', 'GAAP_ADJ', 0.0,  rd_cap_amt, 'IFRS_DELTA', 'IAS 38: 开发阶段支出满足条件资本化为无形资产'),
            (period, CONSOL_GROUP, 'C4000', 'NONE', '660200', '910', '10', 'GAAP_ADJ', 0.0, -rd_cap_amt, 'IFRS_DELTA', 'IAS 38: 冲减原CAS下费用化的研发支出'),
            # PL 30 连带少数股东权益与损益重算
            (period, CONSOL_GROUP, 'C4000', 'NONE', '690100', '910', '30', 'GAAP_NCI', 0.0,  nci_rd_adj, 'IFRS_DELTA', 'IFRS连带调整: 研发资本化增加的少数股东损益(20%)'),
            (period, CONSOL_GROUP, 'C4000', 'NONE', '320100', '910', '30', 'GAAP_NCI', 0.0, -nci_rd_adj, 'IFRS_DELTA', 'IFRS连带调整: 研发资本化增加的少数股东权益(20%)')
        ])

        # ----------------------------------------------------------
        # 差异场景 2：金融资产公允价值变动重分类 (IFRS 9 FVOCI vs CAS 损益)
        # 假设母公司 C1000 有一笔 50,000 收益在 CAS 下计入投资收益(611100)，在 IFRS 9 下重分类计入 OCI(310400)
        # ----------------------------------------------------------
        fvoci_amt = 50000.00
        print(f"\n📌 [IFRS 差异 2 - IFRS 9 金融资产重分类]:")
        print(f"   C1000 将 {fvoci_amt:,.2f} 从投资收益(611100) 重分类转入其他综合收益(310400)")

        delta_entries.extend([
            (period, CONSOL_GROUP, 'C1000', 'NONE', '611100', '910', '10', 'GAAP_ADJ', 0.0,  fvoci_amt, 'IFRS_DELTA', 'IFRS 9: 调减原计入当期损益的金融资产公允价值收益'),
            (period, CONSOL_GROUP, 'C1000', 'NONE', '310400', '910', '10', 'GAAP_ADJ', 0.0, -fvoci_amt, 'IFRS_DELTA', 'IFRS 9: 指定为FVOCI转入其他综合收益(OCI)')
        ])

        self.cursor.executemany("""
            INSERT INTO fact_acdocu 
            (fiscal_period, consol_group, unit_id, partner_unit, fs_item, subitem, posting_level, doc_type, amount_lc, amount_gc, accounting_principle, audit_remark)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, delta_entries)
        self.conn.commit()
        print("\n✅ 所有 IFRS_DELTA 增量调整凭证已写入 ACDOCU 核心表。\n")

    # ==========================================================
    # 步骤 3：并行生成 CAS vs IFRS 双准则对比合并报表与双轨平衡校验
    # ==========================================================
    def export_parallel_gaap_report(self, period, output_file):
        print("="*80)
        print(f"📊 步骤 3：并行组装中国准则 (CAS) 与国际准则 (IFRS) 对比合并报表")
        print("="*80)

        # 读取全量数据
        df_all = pd.read_sql_query("""
            SELECT 
                f.fs_item, i.description AS fs_desc, i.item_type,
                f.accounting_principle, f.posting_level, f.amount_gc
            FROM fact_acdocu f
            JOIN dim_fs_item i ON f.fs_item = i.fs_item
            WHERE f.fiscal_period = ?
        """, self.conn, params=(period,))

        # 1. 计算 CAS 准则合并数 (仅取 accounting_principle == 'COMMON')
        df_cas = df_all[df_all['accounting_principle'] == 'COMMON'].groupby(
            ['item_type', 'fs_item', 'fs_desc']
        )['amount_gc'].sum().rename('中国准则_CAS合并数')

        # 2. 计算 IFRS 增量调整数 (accounting_principle == 'IFRS_DELTA')
        df_delta = df_all[df_all['accounting_principle'] == 'IFRS_DELTA'].groupby(
            ['item_type', 'fs_item', 'fs_desc']
        )['amount_gc'].sum().rename('IFRS准则差异调整(Delta)')

        # 3. 合并组装双准则对比表
        report = pd.concat([df_cas, df_delta], axis=1).fillna(0.0).reset_index()
        report['国际准则_IFRS合并数'] = report['中国准则_CAS合并数'] + report['IFRS准则差异调整(Delta)']

        # 按报表要素标准顺序排序
        type_order = {'AST': 1, 'LEQ': 2, 'INC': 3, 'EXP': 4}
        report['sort_key'] = report['item_type'].map(type_order)
        report = report.sort_values(['sort_key', 'fs_item']).drop(columns=['sort_key'])

        # 添加底部试算平衡总校验行 (三列必须全部为 0.00)
        total_row = {
            'item_type': 'CHECK',
            'fs_item': 'TOTAL',
            'fs_desc': '双准则并行试算平衡校验 (借正贷负合计应为0)',
            '中国准则_CAS合并数': round(report['中国准则_CAS合并数'].sum(), 2),
            'IFRS准则差异调整(Delta)': round(report['IFRS准则差异调整(Delta)'].sum(), 2),
            '国际准则_IFRS合并数': round(report['国际准则_IFRS合并数'].sum(), 2)
        }
        report = pd.concat([report, pd.DataFrame([total_row])], ignore_index=True)

        print(report[['fs_item', 'fs_desc', '中国准则_CAS合并数', 'IFRS准则差异调整(Delta)', '国际准则_IFRS合并数']].to_string(index=False))

        # 双准则独立门禁校验
        cas_ok = abs(total_row['中国准则_CAS合并数']) < 0.01
        ifrs_ok = abs(total_row['国际准则_IFRS合并数']) < 0.01
        print("\n" + "="*80)
        print(f"🚦 [CAS 准则平衡校验]: {'🟢 绿灯 (0.00)' if cas_ok else '🔴 红灯'}")
        print(f"🚦 [IFRS 准则平衡校验]: {'🟢 绿灯 (0.00)' if ifrs_ok else '🔴 红灯'}")
        print("="*80)

        # 导出双准则对比 Excel 报告
        with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
            report.to_excel(writer, sheet_name='CAS_vs_IFRS并行合并报表', index=False)
            df_all[df_all['accounting_principle'] == 'IFRS_DELTA'].to_excel(writer, sheet_name='IFRS准则差异调整明细', index=False)

        print(f"\n✅ 双准则并行合并报表已导出至: {output_file}")
        self.conn.close()

if __name__ == "__main__":
    gaap_engine = MultiGAAPConsolidationEngine(DB_FILE)
    gaap_engine.upgrade_schema_for_multigaap()
    gaap_engine.generate_ifrs_delta_adjustments(PERIOD)
    gaap_engine.export_parallel_gaap_report(PERIOD, OUTPUT_EXCEL)
