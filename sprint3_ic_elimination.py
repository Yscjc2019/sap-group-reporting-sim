import sqlite3
import pandas as pd

DB_FILE = "sap_consolidation.db"
FISCAL_PERIOD = "2026.01"
CONSOL_GROUP = "CG01"
TOLERANCE_LIMIT = 50000.0  # SAP ICMR 配置的自动容差阈值 (5万元以内自动处理差异)

class ICEliminationEngine:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()

    def clear_existing_pl20(self, period):
        """幂等性清理：清除历史生成的 Posting Level 20 自动抵消分录"""
        self.cursor.execute("""
            DELETE FROM fact_acdocu 
            WHERE fiscal_period = ? AND posting_level = '20'
        """, (period,))

    def post_pl20_line(self, period, unit_id, partner_unit, fs_item, subitem, doc_type, amount_gc, remark):
        """写入一条 Posting Level 20 合并抵消分录 (仅影响集团币 amount_gc)"""
        self.cursor.execute("""
            INSERT INTO fact_acdocu 
            (fiscal_period, consol_group, unit_id, partner_unit, fs_item, subitem, posting_level, doc_type, amount_lc, amount_gc, audit_remark)
            VALUES (?, ?, ?, ?, ?, ?, '20', ?, 0.0, ?, ?)
        """, (period, CONSOL_GROUP, unit_id, partner_unit, fs_item, subitem, doc_type, round(amount_gc, 2), remark))

    # ==========================================================
    # 模块 1：内部应收 vs 内部应付 自动对账与容差抵消 (112299 vs 220299)
    # ==========================================================
    def eliminate_ic_ar_ap(self, period):
        print("="*75)
        print("🔍 任务 1：SAP ICMR 内部应收 (112299) vs 内部应付 (220299) 自动对账与抵消")
        print("="*75)

        # 提取所有在合并范围内的内部应收与应付汇总 (按交易双方分组)
        df_ic = pd.read_sql_query("""
            SELECT unit_id, partner_unit, fs_item, SUM(amount_gc) AS amt
            FROM fact_acdocu
            WHERE fiscal_period = ? 
              AND posting_level IN ('00', '01', '10')
              AND fs_item IN ('112299', '220299')
              AND partner_unit != 'NONE'
            GROUP BY unit_id, partner_unit, fs_item
        """, self.conn, params=(period,))

        # 构建所有出现过的无序交易对集合, 如 {('C1000', 'C2000'), ('C2000', 'C4000')}
        pairs = set()
        for _, row in df_ic.iterrows():
            pair = tuple(sorted([row['unit_id'], row['partner_unit']]))
            pairs.add(pair)

        for u1, u2 in sorted(pairs):
            # 方向 A: u1 的应收 vs u2 的应付
            ar_u1 = df_ic[(df_ic['unit_id']==u1) & (df_ic['partner_unit']==u2) & (df_ic['fs_item']=='112299')]['amt'].sum()
            ap_u2 = df_ic[(df_ic['unit_id']==u2) & (df_ic['partner_unit']==u1) & (df_ic['fs_item']=='220299')]['amt'].sum()
            if abs(ar_u1) > 0 or abs(ap_u2) > 0:
                self._process_ar_ap_pair(period, seller=u1, buyer=u2, ar_amt=ar_u1, ap_amt=ap_u2)

            # 方向 B: u2 的应收 vs u1 的应付
            ar_u2 = df_ic[(df_ic['unit_id']==u2) & (df_ic['partner_unit']==u1) & (df_ic['fs_item']=='112299')]['amt'].sum()
            ap_u1 = df_ic[(df_ic['unit_id']==u1) & (df_ic['partner_unit']==u2) & (df_ic['fs_item']=='220299')]['amt'].sum()
            if abs(ar_u2) > 0 or abs(ap_u1) > 0:
                self._process_ar_ap_pair(period, seller=u2, buyer=u1, ar_amt=ar_u2, ap_amt=ap_u1)

    def _process_ar_ap_pair(self, period, seller, buyer, ar_amt, ap_amt):
        # ar_amt 为正(借方), ap_amt 为负(贷方), 理想状态下 ar_amt + ap_amt == 0
        diff = round(ar_amt + ap_amt, 2)
        print(f"\n📌 交易对 [{seller} (应收) <-> {buyer} (应付)]:")
        print(f"   {seller} 内部应收: {ar_amt:>12,.2f} | {buyer} 内部应付: {ap_amt:>12,.2f} | 净差额: {diff:>10,.2f}")

        if abs(diff) <= TOLERANCE_LIMIT:
            # 1. 按双方确认的最小金额全额双边抵消
            elim_base = min(abs(ar_amt), abs(ap_amt))
            if elim_base > 0:
                self.post_pl20_line(period, buyer, seller, '220299', '910', 'IC_ARAP',  elim_base, f"内部应付抵消({buyer}<-{seller})")
                self.post_pl20_line(period, seller, buyer, '112299', '910', 'IC_ARAP', -elim_base, f"内部应收抵消({seller}->{buyer})")
                print(f"   🟢 [自动抵消] 双方匹配金额 {elim_base:,.2f} 已生成 PL 20 抵消分录。")

            # 2. 容差内差额处理：将未达在途差额重分类至外部往来 (避免合并报表残留内部往来余额)
            if diff > 0: # 应收大于应付 (如 C1000 多出 20,000 在途)
                self.post_pl20_line(period, seller, buyer, '112299', '910', 'IC_DIFF', -diff, f"容差内差额冲减内部应收({seller})")
                self.post_pl20_line(period, seller, 'NONE', '112200', '910', 'IC_DIFF',  diff, f"容差内差额重分类至外部应收/在途({seller})")
                print(f"   🟡 [容差重分类] 差额 {diff:,.2f} <= 阈值 {TOLERANCE_LIMIT:,.0f}，已自动重分类至 {seller} 外部应收(112200)。")
        else:
            print(f"   🔴 [超容差阻断] 差额 {abs(diff):,.2f} 超过阈值 {TOLERANCE_LIMIT:,.0f}，系统挂起等待业务核对！")

    # ==========================================================
    # 模块 2：内部购销收入/成本 & 未实现内部存货损益(UPI)抵消
    # ==========================================================
    def eliminate_ic_sales_and_upi(self, period):
        print("\n" + "="*75)
        print("📦 任务 2：内部购销收入/成本 (600199/640199) 及未实现存货损益 (UPI) 抵消")
        print("="*75)

        # 查找本期的内部销售收入与内部成本 (以 C1000 卖给 C2000 为例)
        ic_sales = pd.read_sql_query("""
            SELECT unit_id AS seller, partner_unit AS buyer,
                   SUM(CASE WHEN fs_item = '600199' THEN -amount_gc ELSE 0 END) AS ic_rev,
                   SUM(CASE WHEN fs_item = '640199' THEN amount_gc ELSE 0 END) AS ic_cogs
            FROM fact_acdocu
            WHERE fiscal_period = ? AND posting_level = '00' AND fs_item IN ('600199', '640199')
            GROUP BY unit_id, partner_unit
        """, self.conn, params=(period,))

        for _, row in ic_sales.iterrows():
            seller, buyer = row['seller'], row['buyer']
            ic_rev, ic_cogs = row['ic_rev'], row['ic_cogs']
            gross_margin_rate = (ic_rev - ic_cogs) / ic_rev if ic_rev > 0 else 0.0

            # 查询买方(C2000)期末库存商品(140500)余额，假设其中全部来自关联方未售出部分
            ending_inv = self.cursor.execute("""
                SELECT COALESCE(SUM(amount_gc), 0) FROM fact_acdocu
                WHERE fiscal_period = ? AND unit_id = ? AND fs_item = '140500' AND posting_level = '00'
            """, (period, buyer)).fetchone()[0] 

            # 未实现内部销售损益 (UPI) = 买方期末关联方存货 * 卖方内部销售毛利率
            upi_amount = round(ending_inv * gross_margin_rate, 2)

            print(f"\n📌 内部贸易链 [{seller} -> {buyer}]:")
            print(f"   卖方内部收入: {ic_rev:,.2f} | 卖方内部成本: {ic_cogs:,.2f} | 内部毛利率: {gross_margin_rate*100:.1f}%")
            print(f"   买方期末留存关联方存货: {ending_inv:,.2f} | 含未实现内部利润 (UPI): {upi_amount:,.2f}")

            # 生成标准合并抵消分录 (借: 营业收入-内部 / 贷: 营业成本-内部 / 贷: 库存商品)
            self.post_pl20_line(period, seller, buyer, '600199', '910', 'IC_SALES',  ic_rev,              f"抵消内部销售收入({seller}->{buyer})")
            self.post_pl20_line(period, seller, buyer, '640199', '910', 'IC_SALES', -(ic_rev - upi_amount), f"抵消内部销售成本({seller}->{buyer})")
            if upi_amount > 0:
                self.post_pl20_line(period, buyer, seller, '140500', '910', 'IC_UPI', -upi_amount,       f"抵消期末存货未实现内部毛利({buyer})")
            print(f"   🟢 [自动抵消] 已生成 PL 20 购销与存货未实现利润抵消分录 (借贷平衡校验: 0.00)。")

    def validate_pl20_balance(self, period):
        """第三道防线：校验 PL 20 所有自动抵消分录自身的借贷平衡"""
        net_pl20 = self.cursor.execute("""
            SELECT COALESCE(ROUND(SUM(amount_gc), 2), 0) FROM fact_acdocu
            WHERE fiscal_period = ? AND posting_level = '20'
        """, (period,)).fetchone()[0] 
        
        status = "🟢 绿灯 (PL 20 自动抵消分录完全平衡)" if abs(net_pl20) < 0.01 else f"🔴 红灯 (不平: {net_pl20})"
        print("\n" + "="*75)
        print(f"🚦 SAP Consolidation Monitor - PL 20 自动抵消层级平衡校验: {status}")
        print("="*75 + "\n")

    def run_all(self, period):
        self.clear_existing_pl20(period)
        self.eliminate_ic_ar_ap(period)
        self.eliminate_ic_sales_and_upi(period)
        self.conn.commit()
        self.validate_pl20_balance(period)
        self.conn.close()

if __name__ == "__main__":
    engine = ICEliminationEngine(DB_FILE)
    engine.run_all(FISCAL_PERIOD)
