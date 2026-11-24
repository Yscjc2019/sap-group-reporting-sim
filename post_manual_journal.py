import sqlite3
import pandas as pd

DB_FILE = "sap_consolidation.db"

def post_manual_entry(period, unit_id, posting_level, doc_type, entries, remark):
    """
    模拟 SAP Fiori: Post Group Journal Entries (手工录入合并调整分录)
    entries 格式: [(fs_item, subitem, partner_unit, amount_lc), ...]
    """
    # 1. 核心内控防呆校验：单张凭证自身必须借贷平衡 (借正贷负之和为0)
    total_balance = sum(item[3] for item in entries)
    if abs(total_balance) >= 0.01:
        raise ValueError(f"❌ 过账被拒绝！手工凭证借贷不平，差额为: {total_balance:,.2f}")

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # 2. 幂等性清理：如果同一备注/类型的手工调整已存在，先清理避免重复运行叠加
    cursor.execute("""
        DELETE FROM fact_acdocu 
        WHERE fiscal_period = ? AND unit_id = ? AND posting_level = ? AND doc_type = ?
    """, (period, unit_id, posting_level, doc_type))

    # 3. 写入 ACDOCU 核心表
    records = []
    for fs_item, subitem, partner, amount_lc in entries:
        records.append((
            period, 'CG01', unit_id, partner, fs_item, subitem,
            posting_level, doc_type, amount_lc, 0.0, remark
        ))

    cursor.executemany("""
        INSERT INTO fact_acdocu 
        (fiscal_period, consol_group, unit_id, partner_unit, fs_item, subitem, posting_level, doc_type, amount_lc, amount_gc, audit_remark)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, records)

    conn.commit()
    print(f"✅ 成功过账手工调整凭证 | 实体: {unit_id} | 层级: PL {posting_level} | 凭证类型: {doc_type}")

    # 4. 重新运行该实体的全层级 (PL 00 + PL 01) 试算平衡校验
    df_check = pd.read_sql_query("""
        SELECT 
            posting_level,
            SUM(CASE WHEN amount_lc > 0 THEN amount_lc ELSE 0 END) AS debit,
            SUM(CASE WHEN amount_lc < 0 THEN amount_lc ELSE 0 END) AS credit,
            SUM(amount_lc) AS net_balance
        FROM fact_acdocu
        WHERE fiscal_period = ? AND unit_id = ? AND posting_level IN ('00', '01')
        GROUP BY posting_level
    """, conn, params=(period, unit_id))
    
    print("\n--- 调整前后层级明细对比 ---")
    print(df_check.to_string(index=False))
    
    total_net = df_check['net_balance'].sum()
    status = "🟢 绿灯 (调整后已完全平衡！)" if abs(total_net) < 0.01 else f"🔴 仍不平: {total_net}"
    print(f"\n最终状态 ({unit_id} 调整后单体): {status}")
    conn.close()

if __name__ == "__main__":
    # 针对 C4000 漏报的 200,000 元银行存款，补录一张 PL 01 调整凭证
    # 借：货币资金 (100100) +200,000
    # 贷：未分配利润 (310300) -200,000 (注：在标准会计中补记资产或调减未分配利润取决于查明原因，此处为模拟演示)
    c4000_adj_entries = [
        ('100100', '910', 'NONE',  200000.00),
        ('310300', '900', 'NONE', -200000.00)
    ]
    
    post_manual_entry(
        period="2026.01",
        unit_id="C4000",
        posting_level="01",
        doc_type="MAN_ADJ_01",
        entries=c4000_adj_entries,
        remark="集团合并会计补录C4000漏报基本户存款(保留PL00原始快照)"
    )
