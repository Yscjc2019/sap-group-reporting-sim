import os
import pandas as pd
import openpyxl

UPLOAD_DIR = "uploads"

def generate_c3000_excel():
    """
    生成 C3000 (海外子公司 - 纽约) 的标准英文试算平衡表 (Trial Balance)
    特点：格式规范，分借贷两列，单位 USD，需要汇率折算和科目映射
    """
    data = [
        {"Account_Code": "CASH_USD", "Account_Desc": "Cash in Bank - Chase",   "Partner_Code": "NONE",  "Debit": 150000.00, "Credit": 0.00},
        {"Account_Code": "AR_EXT",   "Account_Desc": "Accounts Receivable",    "Partner_Code": "NONE",  "Debit": 80000.00,  "Credit": 0.00},
        {"Account_Code": "AR_IC",    "Account_Desc": "Intercompany AR - C1000","Partner_Code": "C1000", "Debit": 20000.00,  "Credit": 0.00},
        {"Account_Code": "AP_EXT",   "Account_Desc": "Accounts Payable",       "Partner_Code": "NONE",  "Debit": 0.00,      "Credit": 40000.00},
        {"Account_Code": "CAPITAL",  "Account_Desc": "Share Capital",          "Partner_Code": "NONE",  "Debit": 0.00,      "Credit": 150000.00},
        {"Account_Code": "REV_EXT",  "Account_Desc": "Service Revenue",        "Partner_Code": "NONE",  "Debit": 0.00,      "Credit": 100000.00},
        {"Account_Code": "OPEX",     "Account_Desc": "Operating Expenses",     "Partner_Code": "NONE",  "Debit": 40000.00,  "Credit": 0.00},
    ]
    # 借方合计: 150,000 + 80,000 + 20,000 + 40,000 = 290,000 USD
    # 贷方合计: 40,000 + 150,000 + 100,000 = 290,000 USD (单体借贷严格平衡)
    
    df = pd.DataFrame(data)
    file_path = os.path.join(UPLOAD_DIR, "upload_C3000.xlsx")
    df.to_excel(file_path, index=False, sheet_name="TB_202601")
    print(f"✅ 已生成标准英文TB文件: {file_path}")


def generate_c4000_excel():
    """
    生成 C4000 (新收购子公司 - 深圳, 持股80%) 的非标中文明细表
    特点：含无效表头行、无科目代码、无贸易伙伴列(藏在摘要里)、含底部合计行
    """
    file_path = os.path.join(UPLOAD_DIR, "upload_C4000.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "2026年1月财务快报"

    # 故意在顶部插入两行非结构化表头，模拟真实手工台账
    ws.append(["深圳制造子公司 - 2026年1月科目余额及往来明细表（未审计版）"])
    ws.append(["制表人：张会计", "单位：人民币元", "备注：借方为正，贷方为负"])

    # 第3行才是真正的列名
    headers = ["科目名称", "期末金额(借正贷负)", "摘要说明"]
    ws.append(headers)

    # 业务数据 (借方合计 = 1,100,000; 贷方合计 = -1,100,000)
    # 注意：注册资本 1,000,000 中，母公司 C1000 占 80% (800,000)，少数股东占 20% (200,000)
    # 本期净利润 = 收入 300,000 - 费用 100,000 = 200,000 (少数股东损益占 20% = 40,000)
    rows = [
        ["银行存款 ",     850000.00,  "基本户及一般户余额"],
        ["应收客户款",    350000.00,  "应收外部第三方客户货款"],
        ["内部往来应收",  100000.00,  "销售给上海子公司(C2000)的配件款，尚未收回"], # 贸易伙伴藏在备注里！
        ["应付供应商",   -100000.00,  "应付外部原材料供应商款项"],
        ["注册资本",     -1000000.00, "其中北京母公司C1000出资80万，自然人股东出资20万"],
        ["产品销售收入", -300000.00,  "本月主营产品销售收入（含对C2000销售10万）"],
        ["日常办公费用",  100000.00,  "房租、水电及管理人员工资"],
        ["合计",          0.00,       "自动求和校验行"] # 底部干扰行，清洗时需剔除
    ]

    for r in rows:
        ws.append(r)

    wb.save(file_path)
    print(f"✅ 已生成非标中文脏数据文件: {file_path}")


if __name__ == "__main__":
    if not os.path.exists(UPLOAD_DIR):
        os.makedirs(UPLOAD_DIR)
    
    generate_c3000_excel()
    generate_c4000_excel()
    print("\n🎉 两份模拟子公司上报 Excel 已全部就绪！你可以直接在 Finder 或 Excel 中打开查看它们的长相。")
