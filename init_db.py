import sqlite3
import os

DB_FILE = "sap_consolidation.db"

def init_database():
    # 如果已存在旧库则删除，方便反复重置测试
    if os.path.exists(DB_FILE):
        os.remove(DB_FILE)
        print(f"已清理旧数据库: {DB_FILE}")

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # ==========================================
    # 1. 创建主数据与规则表 (Master Data & Mapping)
    # ==========================================
    
    # 1.1 合并单元主数据 (对应 SAP Consolidation Unit)
    cursor.execute("""
    CREATE TABLE dim_unit (
        unit_id TEXT PRIMARY KEY,
        unit_name TEXT NOT NULL,
        local_curr TEXT NOT NULL,
        source_type TEXT NOT NULL,      -- S4_DIRECT(直连凭证), EXCEL_TB(标准TB), EXCEL_RAW(非标明细)
        ownership_pct REAL NOT NULL,    -- 持股比例 (1.0 = 100%)
        consol_method TEXT NOT NULL     -- FULL(全额合并), EQUITY(权益法)
    )
    """)

    # 1.2 合并报表项目主数据 (对应 SAP FS Item)
    cursor.execute("""
    CREATE TABLE dim_fs_item (
        fs_item TEXT PRIMARY KEY,
        description TEXT NOT NULL,
        item_type TEXT NOT NULL,        -- AST(资产), LEQ(负债及权益), INC(收入), EXP(费用)
        ct_method TEXT NOT NULL,        -- 外币折算方法: CLO(期末), AVG(平均), HIS(历史)
        is_ic_relevant INTEGER NOT NULL -- 是否内部交易相关: 1(是), 0(否)
    )
    """)

    # 1.3 总账科目到合并项目映射表 (对应 SAP Mapping Rules)
    cursor.execute("""
    CREATE TABLE map_gl_to_fs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_system TEXT NOT NULL,    -- 源系统标识: SAP_COA, C3000_COA, C4000_NAME
        source_code TEXT NOT NULL,      -- 源系统科目代码或名称
        fs_item TEXT NOT NULL,          -- 目标合并项目
        default_subitem TEXT NOT NULL,  -- 默认子目/交易类型 (如 910=本期发生)
        FOREIGN KEY (fs_item) REFERENCES dim_fs_item(fs_item)
    )
    """)

    # 1.4 外币汇率表 (对应 SAP Exchange Rates)
    cursor.execute("""
    CREATE TABLE dim_fx_rate (
        fiscal_period TEXT NOT NULL,
        from_curr TEXT NOT NULL,
        to_curr TEXT NOT NULL,
        rate_type TEXT NOT NULL,        -- CLO(期末), AVG(平均), HIS(历史)
        rate REAL NOT NULL,
        PRIMARY KEY (fiscal_period, from_curr, to_curr, rate_type)
    )
    """)

    # ==========================================
    # 2. 创建数据源表与核心合并事实表
    # ==========================================

    # 2.1 模拟 SAP S/4HANA 单体通用日记账 (ACDOCA) - 供 C1000 和 C2000 使用
    cursor.execute("""
    CREATE TABLE src_s4_acdoca (
        doc_no TEXT NOT NULL,
        line_item INTEGER NOT NULL,
        fiscal_period TEXT NOT NULL,
        unit_id TEXT NOT NULL,
        gl_account TEXT NOT NULL,
        partner_unit TEXT DEFAULT 'NONE', -- 贸易伙伴 (Trading Partner)
        subitem TEXT DEFAULT '910',       -- 交易类型 (900期初, 910本期增加)
        amount_lc REAL NOT NULL,          -- 本位币金额 (借正贷负)
        description TEXT,
        PRIMARY KEY (doc_no, line_item, unit_id)
    )
    """)

    # 2.2 核心合并结果表 (模拟 SAP Group Reporting 的 ACDOCU)
    cursor.execute("""
    CREATE TABLE fact_acdocu (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fiscal_period TEXT NOT NULL,
        consol_group TEXT NOT NULL,
        unit_id TEXT NOT NULL,
        partner_unit TEXT DEFAULT 'NONE',
        fs_item TEXT NOT NULL,
        subitem TEXT NOT NULL,
        posting_level TEXT NOT NULL,      -- 00:单体上报, 01:单体调整, 10:标准化, 20:内部抵消, 30:权益抵消
        doc_type TEXT NOT NULL,           -- 凭证类型 (如 REL_S4, UPL_XL, CT_AUTO, IC_ELIM)
        amount_lc REAL NOT NULL,          -- 本位币金额
        amount_gc REAL DEFAULT 0.0,       -- 集团币金额 (CNY)
        audit_remark TEXT                 -- 审计线索备注
    )
    """)

    # ==========================================
    # 3. 插入初始化主数据与业务测试分录
    # ==========================================

    # 3.1 插入合并单元
    units = [
        ('C1000', '集团母公司(北京)', 'CNY', 'S4_DIRECT', 1.0, 'FULL'),
        ('C2000', '国内销售子公司(上海)', 'CNY', 'S4_DIRECT', 1.0, 'FULL'),
        ('C3000', '海外贸易子公司(纽约)', 'USD', 'EXCEL_TB', 1.0, 'FULL'),
        ('C4000', '新收购制造子公司(深圳)', 'CNY', 'EXCEL_RAW', 0.8, 'FULL')
    ]
    cursor.executemany("INSERT INTO dim_unit VALUES (?, ?, ?, ?, ?, ?)", units)

    # 3.2 插入精简版集团合并报表项目 (FS Items)
    fs_items = [
        # 资产 (AST)
        ('100100', '货币资金', 'AST', 'CLO', 0),
        ('112200', '应收账款-外部', 'AST', 'CLO', 0),
        ('112299', '应收账款-内部关联方', 'AST', 'CLO', 1),
        ('140500', '库存商品', 'AST', 'CLO', 0),
        ('151100', '长期股权投资', 'AST', 'HIS', 1),
        ('170100', '商誉', 'AST', 'HIS', 0),
        # 负债与所有者权益 (LEQ)
        ('220200', '应付账款-外部', 'LEQ', 'CLO', 0),
        ('220299', '应付账款-内部关联方', 'LEQ', 'CLO', 1),
        ('300100', '实收资本', 'LEQ', 'HIS', 1),
        ('310300', '未分配利润', 'LEQ', 'HIS', 0),
        ('310400', '其他综合收益-外币折算差额(CTA)', 'LEQ', 'HIS', 0),
        ('320100', '少数股东权益', 'LEQ', 'HIS', 0),
        # 损益 (INC / EXP)
        ('600100', '营业收入-外部', 'INC', 'AVG', 0),
        ('600199', '营业收入-内部关联方', 'INC', 'AVG', 1),
        ('640100', '营业成本-外部', 'EXP', 'AVG', 0),
        ('640199', '营业成本-内部关联方', 'EXP', 'AVG', 1),
        ('660200', '管理费用', 'EXP', 'AVG', 0),
        ('690100', '少数股东损益', 'EXP', 'AVG', 0)
    ]
    cursor.executemany("INSERT INTO dim_fs_item VALUES (?, ?, ?, ?, ?)", fs_items)

    # 3.3 插入多源科目映射规则 (SAP标准科目 / C3000英文科目 / C4000中文非标名称)
    mappings = [
        # SAP_COA (C1000, C2000 使用)
        ('SAP_COA', '10020100', '100100', '910'), # 银行存款 -> 货币资金
        ('SAP_COA', '11220100', '112200', '910'), # 应收外部 -> 应收账款-外部
        ('SAP_COA', '11220200', '112299', '910'), # 应收内部 -> 应收账款-内部
        ('SAP_COA', '14050100', '140500', '910'), # 库存商品 -> 库存商品
        ('SAP_COA', '15110100', '151100', '900'), # 长期股权投资 -> 长期股权投资
        ('SAP_COA', '22020100', '220200', '910'), # 应付外部 -> 应付账款-外部
        ('SAP_COA', '22020200', '220299', '910'), # 应付内部 -> 应付账款-内部
        ('SAP_COA', '40010100', '300100', '900'), # 实收资本 -> 实收资本
        ('SAP_COA', '60010100', '600100', '910'), # 主营业务收入-外部 -> 营业收入-外部
        ('SAP_COA', '60010200', '600199', '910'), # 主营业务收入-内部 -> 营业收入-内部
        ('SAP_COA', '64010100', '640100', '910'), # 主营业务成本-外部 -> 营业成本-外部
        ('SAP_COA', '64010200', '640199', '910'), # 主营业务成本-内部 -> 营业成本-内部
        ('SAP_COA', '66020100', '660200', '910'), # 管理费用 -> 管理费用
        
        # C3000_COA (海外子公司英文科目表映射)
        ('C3000_COA', 'CASH_USD', '100100', '910'),
        ('C3000_COA', 'AR_EXT',   '112200', '910'),
        ('C3000_COA', 'AR_IC',    '112299', '910'),
        ('C3000_COA', 'AP_EXT',   '220200', '910'),
        ('C3000_COA', 'CAPITAL',  '300100', '900'),
        ('C3000_COA', 'REV_EXT',  '600100', '910'),
        ('C3000_COA', 'OPEX',     '660200', '910'),

        # C4000_NAME (非标子公司中文科目名称映射)
        ('C4000_NAME', '银行存款',     '100100', '910'),
        ('C4000_NAME', '应收客户款',   '112200', '910'),
        ('C4000_NAME', '内部往来应收', '112299', '910'),
        ('C4000_NAME', '应付供应商',   '220200', '910'),
        ('C4000_NAME', '注册资本',     '300100', '900'),
        ('C4000_NAME', '产品销售收入', '600100', '910'),
        ('C4000_NAME', '日常办公费用', '660200', '910')
    ]
    cursor.executemany("INSERT INTO map_gl_to_fs (source_system, source_code, fs_item, default_subitem) VALUES (?, ?, ?, ?)", mappings)

    # 3.4 插入 2026.01 汇率数据 (USD -> CNY)
    rates = [
        ('2026.01', 'USD', 'CNY', 'CLO', 7.20), # 期末汇率
        ('2026.01', 'USD', 'CNY', 'AVG', 7.15), # 平均汇率
        ('2026.01', 'USD', 'CNY', 'HIS', 7.00)  # 历史汇率(投资时点)
    ]
    cursor.executemany("INSERT INTO dim_fx_rate VALUES (?, ?, ?, ?, ?)", rates)

    # 3.5 插入 SAP 单体凭证 (src_s4_acdoca) - 借正贷负，每张凭证严格借贷平衡
    acdoca_entries = [
        # --- C1000 (母公司) 业务分录 ---
        # 凭证1: 股东投入实收资本 500万
        ('DOC1001', 1, '2026.01', 'C1000', '10020100', 'NONE', '910', 5000000.0, '收到股东出资'),
        ('DOC1001', 2, '2026.01', 'C1000', '40010100', 'NONE', '900', -5000000.0, '确认实收资本'),
        # 凭证2: 投资 C2000 (100%持股, 100万) 和 C4000 (80%持股, 80万)
        ('DOC1002', 1, '2026.01', 'C1000', '15110100', 'C2000', '900', 1000000.0, '对C2000长期股权投资'),
        ('DOC1002', 2, '2026.01', 'C1000', '15110100', 'C4000', '900', 800000.0, '对C4000长期股权投资'),
        ('DOC1002', 3, '2026.01', 'C1000', '10020100', 'NONE',  '910', -1800000.0, '支付投资款'),
        # 凭证3: 向 C2000 销售商品 20万 (成本15万)，形成内部应收和内部收入
        ('DOC1003', 1, '2026.01', 'C1000', '11220200', 'C2000', '910', 200000.0, '应收C2000货款'),
        ('DOC1003', 2, '2026.01', 'C1000', '60010200', 'C2000', '910', -200000.0, '确认对C2000内部收入'),
        ('DOC1003', 3, '2026.01', 'C1000', '64010200', 'C2000', '910', 150000.0, '结转对C2000内部成本'),
        ('DOC1003', 4, '2026.01', 'C1000', '14050100', 'NONE',  '910', -150000.0, '发出库存商品'),

        # --- C2000 (国内子公司) 业务分录 ---
        # 凭证1: 收到母公司 C1000 投资款 100万
        ('DOC2001', 1, '2026.01', 'C2000', '10020100', 'NONE',  '910', 1000000.0, '收到C1000投资款'),
        ('DOC2001', 2, '2026.01', 'C2000', '40010100', 'C1000', '900', -1000000.0, '确认实收资本'),
        # 凭证2: 从 C1000 采购商品，但故意只入账 18万 (还有2万在途未入账，制造内部对账差异！)
        ('DOC2002', 1, '2026.01', 'C2000', '14050100', 'NONE',  '910', 180000.0, '采购入库商品'),
        ('DOC2002', 2, '2026.01', 'C2000', '22020200', 'C1000', '910', -180000.0, '应付C1000货款'),
        # 凭证3: 将从 C1000 采购的商品对外销售了一半 (成本9万，售价12万)
        ('DOC2003', 1, '2026.01', 'C2000', '11220100', 'NONE', '910', 120000.0, '外部客户应收款'),
        ('DOC2003', 2, '2026.01', 'C2000', '60010100', 'NONE', '910', -120000.0, '对外销售收入'),
        ('DOC2003', 3, '2026.01', 'C2000', '64010100', 'NONE', '910', 90000.0, '对外销售成本'),
        ('DOC2003', 4, '2026.01', 'C2000', '14050100', 'NONE', '910', -90000.0, '结转外部销售库存')
    ]
    cursor.executemany("INSERT INTO src_s4_acdoca VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", acdoca_entries)

    conn.commit()
    conn.close()
    print("✅ 数据库初始化成功！所有主数据与 SAP 单体测试凭证已写入。")

if __name__ == "__main__":
    init_database()
