import os
import glob
import time

# 导入各阶段核心模块
from init_db import init_database
from generate_excels import generate_c3000_excel, generate_c4000_excel, UPLOAD_DIR
from sprint1_data_monitor import DataMonitor
from post_manual_journal import post_manual_entry
from sprint2_currency_translation import CurrencyTranslationEngine
from sprint3_ic_elimination import ICEliminationEngine
from sprint4_coi_and_report import ConsolidationOfInvestmentsEngine
from sprint5_dynamic_expansion import DynamicExpansionManager
from sprint8_consolidated_cashflow import ConsolidatedCashFlowEngine
from sprint6_balance_carryforward import BalanceCarryforwardEngine
from sprint7_divestiture import DivestitureEngine

DB_FILE = "sap_consolidation.db"

def clean_workspace():
    """第 0 步：彻底清理历史生成的数据库与 Excel 报表，确保演练环境纯净"""
    print("="*80)
    print("🧹 [STAGE 0] 清理历史运行残留文件，重置纯净沙盒环境...")
    print("="*80)
    
    if os.path.exists(DB_FILE):
        os.remove(DB_FILE)
        print(f"   已移除旧数据库: {DB_FILE}")
        
    for xlsx_file in glob.glob("Consolidated_*.xlsx"):
        os.remove(xlsx_file)
        print(f"   已移除旧报表文件: {xlsx_file}")
        
    if not os.path.exists(UPLOAD_DIR):
        os.makedirs(UPLOAD_DIR)
    print("✅ 环境清理完毕！\n")

def run_end_to_end_simulation():
    start_time = time.time()
    clean_workspace()

    # ==========================================================
    # 第一幕：2026.01 基础初始化与多源异构数据采集
    # ==========================================================
    print("\n" + "🎬"*25)
    print("   第一幕：2026.01 集团初始化、异构数据采集与手工调整")
    print("🎬"*25 + "\n")
    
    init_database()
    generate_c3000_excel()
    generate_c4000_excel()
    
    # 执行 Data Monitor 采集与校验
    dm = DataMonitor(DB_FILE)
    dm.execute_all("2026.01")
    
    # 模拟集团合并会计录入 PL 01 手工调整凭证 (C4000 期初留存现金调整)
    c4000_adj = [
        ('100100', '910', 'NONE',  200000.00),
        ('310300', '900', 'NONE', -200000.00)
    ]
    post_manual_entry(
        period="2026.01", unit_id="C4000", posting_level="01", doc_type="MAN_ADJ_01",
        entries=c4000_adj, remark="集团合并会计补录C4000期初留存资金调整"
    )

    # ==========================================================
    # 第二幕：2026.01 外币折算、内部交易抵消与常规权益合并
    # ==========================================================
    print("\n" + "🎬"*25)
    print("   第二幕：2026.01 外币 CTA 折算、ICMR 内部抵消与 COI 合并")
    print("🎬"*25 + "\n")
    
    ct = CurrencyTranslationEngine(DB_FILE)
    ct.run_translation("2026.01")
    ct.validate_group_currency_balance("2026.01")
    
    ic = ICEliminationEngine(DB_FILE)
    ic.run_all("2026.01")
    
    coi = ConsolidationOfInvestmentsEngine(DB_FILE)
    coi.run_coi_elimination("2026.01")
    coi.export_consolidation_worksheet("2026.01", "Consolidated_01_2026.01_BASE.xlsx")

    # ==========================================================
    # 第三幕：2026.01 主数据动态扩展 (新增联营企业 C5000 & 投资收益科目)
    # ==========================================================
    print("\n" + "🎬"*25)
    print("   第三幕：2026.01 动态追加联营企业(权益法)并生成三大合并主表")
    print("🎬"*25 + "\n")
    
    exp_mgr = DynamicExpansionManager(DB_FILE)
    exp_mgr.expand_master_data_and_transactions()
    exp_mgr.run_equity_method_consolidation("2026.01")
    
    # 自动编制 2026.01 合并现金流量表并完成三大主表钩稽校验
    cf = ConsolidatedCashFlowEngine(DB_FILE)
    cf.generate_cash_flow_statement("2026.01")

    # ==========================================================
    # 第四幕：跨年结转 (2026.01 -> 2027.01) 与期初存货未实现毛利冲回
    # ==========================================================
    print("\n" + "🎬"*25)
    print("   第四幕：跨年度余额结转 (BCF) 与期初未实现存货毛利自动冲回")
    print("🎬"*25 + "\n")
    
    bcf = BalanceCarryforwardEngine(DB_FILE)
    bcf.run_bcf("2026.01", "2027.01")
    bcf.reverse_prior_year_upi("2027.01")
    bcf.validate_and_export("2027.01")

    # ==========================================================
    # 第五幕：2027.01 子公司处置与合并范围退出 (Divestiture - C2000)
    # ==========================================================
    print("\n" + "🎬"*25)
    print("   第五幕：2027.01 出售子公司 C2000、移出合并范围与重算处置损益")
    print("🎬"*25 + "\n")
    
    div_engine = DivestitureEngine(DB_FILE)
    div_engine.clear_existing_divestiture("2027.01")
    cost, gain = div_engine.record_parent_sale_entry("2027.01", "C2000", 1200000.00)
    div_engine.execute_deconsolidation("2027.01", "C2000", 1200000.00, cost, gain)
    div_engine.validate_and_export("2027.01", "C2000")

    # ==========================================================
    # 演练总结报告
    # ==========================================================
    elapsed = round(time.time() - start_time, 2)
    print("\n" + "="*80)
    print(f"🏆 全生命周期合并财务报表沙盒演练圆满完成！总耗时: {elapsed} 秒")
    print("="*80)
    print("📂 本次演练生成的全套审计底稿与报表文件清单：")
    for f in sorted(glob.glob("Consolidated_*.xlsx")):
        print(f"   📄 {f}")
    print(f"   🗄️  {DB_FILE} (可在 SQLite Viewer 中查看全量凭证流与状态日志)")
    print("="*80)

if __name__ == "__main__":
    run_end_to_end_simulation()
