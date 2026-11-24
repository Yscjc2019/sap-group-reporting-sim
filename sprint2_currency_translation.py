import sqlite3
import pandas as pd

DB_FILE = "sap_consolidation.db"
FISCAL_PERIOD = "2026.01"
CONSOL_GROUP = "CG01"
GROUP_CURR = "CNY"

class CurrencyTranslationEngine:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()

    def clear_existing_cta(self, period):
        """幂等性清理：重跑折算前清除之前自动生成的 CTA 差额凭证"""
        self.cursor.execute("""
            DELETE FROM fact_acdocu 
            WHERE fiscal_period = ? AND doc_type = 'CT_AUTO'
        """, (period,))

    def run_translation(self, period):
        self.clear_existing_cta(period)
        print(f"💱 开始执行外币报表折算任务 (期间: {period}, 集团本位币: {GROUP_CURR})...\n")

        # 1. 处理本位币等于集团币的实体 (C1000, C2000, C4000)：直接平推 amount_gc = amount_lc
        self.cursor.execute("""
            UPDATE fact_acdocu
            SET amount_gc = amount_lc
            WHERE fiscal_period = ? 
              AND unit_id IN (SELECT unit_id FROM dim_unit WHERE local_curr = ?)
        """, (period, GROUP_CURR))
        print(f"✅ 本位币为 {GROUP_CURR} 的实体已直接平推集团币金额 (共更新 {self.cursor.rowcount} 条)。")

        # 2. 找出所有外币实体 (如 C3000, local_curr = USD)
        foreign_units = self.cursor.execute("""
            SELECT unit_id, unit_name, local_curr 
            FROM dim_unit 
            WHERE local_curr != ?
        """, (GROUP_CURR,)).fetchall()

        for unit_id, unit_name, local_curr in foreign_units:
            # 加载该币种的汇率字典: {'CLO': 7.20, 'AVG': 7.15, 'HIS': 7.00}
            rates_df = pd.read_sql_query("""
                SELECT rate_type, rate FROM dim_fx_rate 
                WHERE fiscal_period = ? AND from_curr = ? AND to_curr = ?
            """, self.conn, params=(period, local_curr, GROUP_CURR))
            rate_map = dict(zip(rates_df['rate_type'], rates_df['rate']))

            # 读取该外币实体的所有待折算记录及对应的折算方法 (ct_method)
            records = self.cursor.execute("""
                SELECT f.id, f.fs_item, i.description, i.ct_method, f.amount_lc
                FROM fact_acdocu f
                JOIN dim_fs_item i ON f.fs_item = i.fs_item
                WHERE f.fiscal_period = ? AND f.unit_id = ?
            """, (period, unit_id)).fetchall()

            print(f"\n--- 正在折算外币实体: {unit_id} ({unit_name}) | 源币种: {local_curr} ---")
            for rec_id, fs_item, desc, ct_method, amount_lc in records:
                if ct_method not in rate_map:
                    raise ValueError(f"❌ 缺失汇率配置: {local_curr}->{GROUP_CURR} 类型 {ct_method}")
                
                fx_rate = rate_map[ct_method]
                amount_gc = round(amount_lc * fx_rate, 2)
                
                self.cursor.execute("""
                    UPDATE fact_acdocu SET amount_gc = ? WHERE id = ?
                """, (amount_gc, rec_id))
                print(f"   科目 {fs_item} ({desc:<10}) | 原币: {amount_lc:>10,.2f} | 方法: {ct_method} ({fx_rate}) | 集团币: {amount_gc:>12,.2f}")

            # 3. 核心：计算折算后的集团币借贷差额，自动生成 CTA (外币报表折算差额) 凭证
            net_gc_diff = self.cursor.execute("""
                SELECT ROUND(SUM(amount_gc), 2) 
                FROM fact_acdocu 
                WHERE fiscal_period = ? AND unit_id = ?
            """, (period, unit_id)).fetchone()[0]

            if abs(net_gc_diff) >= 0.01:
                cta_amount_gc = round(-net_gc_diff, 2) # 取反以轧平资产负债表
                self.cursor.execute("""
                    INSERT INTO fact_acdocu 
                    (fiscal_period, consol_group, unit_id, partner_unit, fs_item, subitem, posting_level, doc_type, amount_lc, amount_gc, audit_remark)
                    VALUES (?, ?, ?, 'NONE', '310400', '910', '00', 'CT_AUTO', 0.0, ?, ?)
                """, (
                    period, CONSOL_GROUP, unit_id, cta_amount_gc,
                    f"系统自动计算外币报表折算差额(CTA): {local_curr}->{GROUP_CURR}"
                ))
                print(f"✨ 自动生成 CTA 凭证: 借/贷 310400 (其他综合收益-CTA) | 原币: 0.00 | 集团币: {cta_amount_gc:>12,.2f}")

        self.conn.commit()

    def validate_group_currency_balance(self, period):
        """第二道防线：全集团所有实体折算后集团币 (CNY) 试算平衡校验"""
        print("\n" + "="*72)
        print(f"🚦 SAP Consolidation Monitor - 折算后集团币 ({GROUP_CURR}) 试算平衡校验 ({period})")
        print("="*72)
        
        query = """
            SELECT 
                u.unit_id,
                u.unit_name,
                u.local_curr,
                ROUND(SUM(CASE WHEN f.amount_gc > 0 THEN f.amount_gc ELSE 0 END), 2) AS gc_debit,
                ROUND(SUM(CASE WHEN f.amount_gc < 0 THEN f.amount_gc ELSE 0 END), 2) AS gc_credit,
                ROUND(SUM(f.amount_gc), 2) AS gc_diff
            FROM dim_unit u
            LEFT JOIN fact_acdocu f ON u.unit_id = f.unit_id AND f.fiscal_period = ?
            GROUP BY u.unit_id, u.unit_name, u.local_curr
        """
        df = pd.read_sql_query(query, self.conn, params=(period,))
        for _, row in df.iterrows():
            status = "🟢 绿灯 (集团币平衡)" if abs(row['gc_diff']) < 0.01 else f"🔴 红灯 (差额: {row['gc_diff']})"
            print(f"实体: {row['unit_id']} ({row['local_curr']}->{GROUP_CURR}) | 借方: {row['gc_debit']:>12,.2f} | 贷方: {row['gc_credit']:>12,.2f} | {status}")
        print("="*72 + "\n")
        self.conn.close()

if __name__ == "__main__":
    engine = CurrencyTranslationEngine(DB_FILE)
    engine.run_translation(FISCAL_PERIOD)
    engine.validate_group_currency_balance(FISCAL_PERIOD)
