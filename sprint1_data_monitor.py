import sqlite3
import pandas as pd
import re
import os

DB_FILE = "sap_consolidation.db"
FISCAL_PERIOD = "2026.01"
CONSOL_GROUP = "CG01"

class DataMonitor:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()

    def clear_existing_pl00(self, unit_id, period):
        """幂等性清理：重跑采集前清除该实体当期 Posting Level 00 的旧数据"""
        self.cursor.execute("""
            DELETE FROM fact_acdocu 
            WHERE unit_id = ? AND fiscal_period = ? AND posting_level = '00'
        """, (unit_id, period))

    def get_mapping_dict(self, source_system):
        """加载指定源系统的科目映射规则字典"""
        df_map = pd.read_sql_query(
            "SELECT source_code, fs_item, default_subitem FROM map_gl_to_fs WHERE source_system = ?",
            self.conn, params=(source_system,)
        )
        # 返回格式: {'10020100': ('100100', '910'), ...}
        return {row['source_code']: (row['fs_item'], row['default_subitem']) for _, row in df_map.iterrows()}

    # ==========================================================
    # 引擎 1：SAP S/4HANA 直连数据释放 (Release Universal Journal)
    # ==========================================================
    def release_s4_direct(self, unit_id, period):
        self.clear_existing_pl00(unit_id, period)
        
        # 模拟 SAP 底层 SQL 聚合：从明细表 src_s4_acdoca 关联映射表，按合并维度汇总
        query = """
            SELECT 
                a.fiscal_period,
                ? AS consol_group,
                a.unit_id,
                a.partner_unit,
                m.fs_item,
                a.subitem,
                '00' AS posting_level,
                'REL_S4' AS doc_type,
                SUM(a.amount_lc) AS amount_lc,
                0.0 AS amount_gc,
                'SAP S/4HANA 实时通用日记账释放' AS audit_remark
            FROM src_s4_acdoca a
            JOIN map_gl_to_fs m ON a.gl_account = m.source_code AND m.source_system = 'SAP_COA'
            WHERE a.unit_id = ? AND a.fiscal_period = ?
            GROUP BY a.fiscal_period, a.unit_id, a.partner_unit, m.fs_item, a.subitem
        """
        df = pd.read_sql_query(query, self.conn, params=(CONSOL_GROUP, unit_id, period))
        df.to_sql('fact_acdocu', self.conn, if_exists='append', index=False)
        print(f"🔄 [{unit_id}] S/4HANA 直连数据释放完成，写入 {len(df)} 条汇总记录。")

    # ==========================================================
    # 引擎 2：标准 Excel 试算平衡表灵活上载 (Flexible Upload)
    # ==========================================================
    def upload_excel_tb(self, unit_id, period, file_path):
        self.clear_existing_pl00(unit_id, period)
        mapping = self.get_mapping_dict('C3000_COA')
        
        df_raw = pd.read_excel(file_path)
        records = []
        for _, row in df_raw.iterrows():
            src_code = str(row['Account_Code']).strip()
            if src_code not in mapping:
                raise ValueError(f"❌ [{unit_id}] 发现未映射的外部科目代码: {src_code}")
            
            fs_item, subitem = mapping[src_code]
            # 借正贷负转换
            amount_lc = float(row['Debit']) - float(row['Credit'])
            partner = str(row['Partner_Code']).strip() if pd.notna(row['Partner_Code']) else 'NONE'
            
            records.append((
                period, CONSOL_GROUP, unit_id, partner, fs_item, subitem,
                '00', 'UPL_XL_TB', amount_lc, 0.0, f"标准TB上载: {row['Account_Desc']}"
            ))

        self.cursor.executemany("""
            INSERT INTO fact_acdocu 
            (fiscal_period, consol_group, unit_id, partner_unit, fs_item, subitem, posting_level, doc_type, amount_lc, amount_gc, audit_remark)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, records)
        print(f"📥 [{unit_id}] 标准英文 TB 上载完成，写入 {len(records)} 条记录。")

    # ==========================================================
    # 引擎 3：非标中文 Excel 脏数据清洗与智能识别 (ETL + Regex)
    # ==========================================================
    def upload_excel_raw(self, unit_id, period, file_path):
        self.clear_existing_pl00(unit_id, period)
        mapping = self.get_mapping_dict('C4000_NAME')
        
        # 1. 跳过前两行无效标题 (header=2 表示第3行是列名)
        df_raw = pd.read_excel(file_path, header=2)
        records = []

        for _, row in df_raw.iterrows():
            acc_name = str(row['科目名称']).strip()
            
            # 2. 过滤底部“合计”行或空行
            if acc_name in ['合计', '总计', 'nan', '']:
                continue
            if acc_name not in mapping:
                raise ValueError(f"❌ [{unit_id}] 发现未映射的中文科目名称: '{acc_name}'")

            fs_item, subitem = mapping[acc_name]
            amount_lc = float(row['期末金额(借正贷负)'])
            remark = str(row['摘要说明']) if pd.notna(row['摘要说明']) else ''

            # 3. 核心清洗逻辑：利用正则从中文摘要中自动抓取贸易伙伴 (如 C1000, C2000)
            # 注意：实收资本/注册资本虽然备注里有C1000，但如果是非内部往来科目，仅在内部相关科目上打Partner标签
            is_ic = self.cursor.execute("SELECT is_ic_relevant FROM dim_fs_item WHERE fs_item = ?", (fs_item,)).fetchone()[0]
            partner = 'NONE'
            if is_ic == 1:
                match = re.search(r'(C\d{4})', remark)
                if match:
                    partner = match.group(1)

            records.append((
                period, CONSOL_GROUP, unit_id, partner, fs_item, subitem,
                '00', 'UPL_XL_RAW', amount_lc, 0.0, f"非标清洗上载 | 原摘要: {remark}"
            ))

        self.cursor.executemany("""
            INSERT INTO fact_acdocu 
            (fiscal_period, consol_group, unit_id, partner_unit, fs_item, subitem, posting_level, doc_type, amount_lc, amount_gc, audit_remark)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, records)
        print(f"🧹 [{unit_id}] 非标中文表格清洗并上载完成，写入 {len(records)} 条记录。")

    # ==========================================================
    # 核心稽核：SAP Data Monitor 试算平衡校验 (Validation)
    # ==========================================================
    def run_validation_check(self, period):
        print("\n" + "="*65)
        print(f"🚦 SAP Data Monitor - 单体上报数据试算平衡校验 ({period})")
        print("="*65)
        
        query = """
            SELECT 
                u.unit_id,
                u.unit_name,
                u.local_curr,
                ROUND(SUM(CASE WHEN f.amount_lc > 0 THEN f.amount_lc ELSE 0 END), 2) AS total_debit,
                ROUND(SUM(CASE WHEN f.amount_lc < 0 THEN f.amount_lc ELSE 0 END), 2) AS total_credit,
                ROUND(SUM(f.amount_lc), 2) AS balance_diff
            FROM dim_unit u
            LEFT JOIN fact_acdocu f ON u.unit_id = f.unit_id AND f.fiscal_period = ? AND f.posting_level = '00'
            GROUP BY u.unit_id, u.unit_name, u.local_curr
        """
        df_val = pd.read_sql_query(query, self.conn, params=(period,))
        
        for _, row in df_val.iterrows():
            status = "🟢 绿灯 (平衡通过)" if abs(row['balance_diff']) < 0.01 else f"🔴 红灯 (不平差额: {row['balance_diff']})"
            print(f"实体: {row['unit_id']} ({row['unit_name']}) | 币种: {row['local_curr']}")
            print(f"   借方合计: {row['total_debit']:>12,.2f} | 贷方合计: {row['total_credit']:>12,.2f} | 状态: {status}")
        print("="*65 + "\n")

    def execute_all(self, period):
        units = self.cursor.execute("SELECT unit_id, source_type FROM dim_unit").fetchall()
        for unit_id, source_type in units:
            if source_type == 'S4_DIRECT':
                self.release_s4_direct(unit_id, period)
            elif source_type == 'EXCEL_TB':
                self.upload_excel_tb(unit_id, period, f"uploads/upload_{unit_id}.xlsx")
            elif source_type == 'EXCEL_RAW':
                self.upload_excel_raw(unit_id, period, f"uploads/upload_{unit_id}.xlsx")
        
        self.conn.commit()
        self.run_validation_check(period)
        self.conn.close()

if __name__ == "__main__":
    monitor = DataMonitor(DB_FILE)
    monitor.execute_all(FISCAL_PERIOD)
