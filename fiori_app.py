import streamlit as st
import sqlite3
import pandas as pd
import altair as alt
import subprocess
import sys
import os

DB_FILE = "sap_consolidation.db"
st.set_page_config(page_title="SAP S/4HANA Group Reporting Monitor", page_icon="📊", layout="wide")

st.markdown("""
<style>
    .fiori-shell {
        background: linear-gradient(90deg, #2C3E50 0%, #354A5F 100%);
        padding: 18px 24px;
        border-radius: 8px;
        color: white;
        margin-bottom: 20px;
        border-left: 6px solid #0A6ED1;
        box-shadow: 0 4px 12px rgba(0,0,0,0.08);
    }
    .fiori-shell h2 { margin: 0; font-size: 22px; font-weight: 600; color: #FFFFFF; }
    .fiori-shell p { margin: 5px 0 0 0; font-size: 13px; color: #D1E0EE; }
    div[data-testid="stMetric"] {
        background-color: #FFFFFF;
        border: 1px solid #E2E8F0;
        border-top: 4px solid #0A6ED1;
        border-radius: 8px;
        padding: 14px 18px;
        box-shadow: 0 2px 6px rgba(15, 23, 42, 0.05);
    }
</style>
<div class="fiori-shell">
    <h2>SAP S/4HANA Group Reporting — 集团合并财务报表实时监控台</h2>
    <p>Consolidation Group: CG01 | Group Currency: CNY | Universal Journal (ACDOCU) 多维实时审计与智能披露</p>
</div>
""", unsafe_allow_html=True)

with st.sidebar:
    st.markdown("### 🎛️ 合并监控全局参数")
    if not os.path.exists(DB_FILE):
        subprocess.run([sys.executable, "run_full_demo.py"], check=True)
    conn_init = sqlite3.connect(DB_FILE)
    periods = pd.read_sql_query("SELECT DISTINCT fiscal_period FROM fact_acdocu ORDER BY fiscal_period", conn_init)['fiscal_period'].tolist()
    conn_init.close()
    selected_period = st.selectbox("📅 会计期间 (Fiscal Period)", periods if periods else ["2026.01"])
    selected_gaap = st.radio("⚖️ 会计准则 (Accounting Principle)", ["CAS (中国准则 - COMMON)", "IFRS (国际准则 - COMMON + DELTA)"])
    gaap_filter = "COMMON" if "CAS" in selected_gaap else "ALL"

    st.markdown("---")
    st.markdown("### ⚡ SAP Monitor 任务调度")
    if st.button("🔄 一键全生命周期重置与重算", use_container_width=True, type="primary"):
        with st.spinner("正在执行五幕全生命周期合并演练..."):
            subprocess.run([sys.executable, "run_full_demo.py"], check=True)
            st.success("✅ 全周期合并重算完成！")
            st.rerun()
    if st.button("🚦 触发严格模式关账门禁测试", use_container_width=True):
        with st.spinner("正在执行严格模式门禁检查..."):
            subprocess.run([sys.executable, "master_pipeline.py"])
            st.info("门禁检查完成，请切换至【5. 状态机门禁日志】查看拦截详情！")

def load_data(period, gaap_mode):
    conn = sqlite3.connect(DB_FILE)
    gaap_cond = "AND COALESCE(f.accounting_principle, 'COMMON') = 'COMMON'" if gaap_mode == "COMMON" else ""
    df_fact = pd.read_sql_query(f"""
        SELECT f.id, f.fiscal_period, f.unit_id, u.unit_name, u.consol_method,
               f.partner_unit, f.fs_item, i.description AS fs_desc, i.item_type,
               f.subitem, f.posting_level, f.doc_type,
               COALESCE(f.accounting_principle, 'COMMON') AS accounting_principle,
               f.amount_lc, f.amount_gc, f.audit_remark
        FROM fact_acdocu f
        JOIN dim_fs_item i ON f.fs_item = i.fs_item
        LEFT JOIN dim_unit u ON f.unit_id = u.unit_id
        WHERE f.fiscal_period = ? {gaap_cond}
    """, conn, params=(period,))
    df_units = pd.read_sql_query("SELECT * FROM dim_unit", conn)
    has_log = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sys_task_log'").fetchone()
    df_logs = pd.read_sql_query("SELECT * FROM sys_task_log ORDER BY log_id DESC LIMIT 20", conn) if has_log else pd.DataFrame()
    conn.close()
    return df_fact, df_units, df_logs

df_fact, df_units, df_logs = load_data(selected_period, gaap_filter)
total_assets = df_fact[df_fact['item_type'] == 'AST']['amount_gc'].sum()
total_rev = -df_fact[df_fact['fs_item'].isin(['600100', '600199'])]['amount_gc'].sum()
net_profit_parent = -df_fact[df_fact['item_type'].isin(['INC', 'EXP'])]['amount_gc'].sum()
trial_balance_diff = round(df_fact['amount_gc'].sum(), 2)

col1, col2, col3, col4 = st.columns(4)
col1.metric("🏦 集团合并总资产 (CNY)", f"¥ {total_assets:,.2f}")
col2.metric("📈 集团合并营业收入 (CNY)", f"¥ {total_rev:,.2f}")
col3.metric("💰 归属于母公司净利润 (CNY)", f"¥ {net_profit_parent:,.2f}")
if abs(trial_balance_diff) >= 0.01:
    col4.metric("🚦 全集团试算平衡校验", f"🔴 {trial_balance_diff:,.2f}", delta="存在未平差额", delta_color="inverse")
else:
    col4.metric("🚦 全集团试算平衡校验", "🟢 0.00", delta="借贷完全平衡", delta_color="normal")
st.markdown("---")

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "🚦 1. 实体红绿灯状态",
    "📊 2. 多维合并工作底稿",
    "📈 3. 财务结构分析图表",
    "🔍 4. ACDOCU 凭证流审计",
    "🛡️ 5. 状态机门禁日志",
    "📑 6. 现金流与附注披露"
])

with tab1:
    st.subheader(f"实体合并范围与单体试算平衡监控矩阵 ({selected_period})")
    st.caption("注：单体平衡校验严格针对单体上报及调整层 (PL 00 + PL 01)；跨实体抵消调整 (PL 10/20/30) 单独列示。")
    entity_summary = []
    for _, u in df_units.iterrows():
        uid = u['unit_id']
        u_local = df_fact[(df_fact['unit_id'] == uid) & (df_fact['posting_level'].isin(['00', '01']))]
        u_elim = df_fact[(df_fact['unit_id'] == uid) & (df_fact['posting_level'].isin(['10', '20', '30']))]
        debit_gc = u_local[u_local['amount_gc'] > 0]['amount_gc'].sum()
        credit_gc = u_local[u_local['amount_gc'] < 0]['amount_gc'].sum()
        net_local = round(u_local['amount_gc'].sum(), 2)
        elim_net = round(u_elim['amount_gc'].sum(), 2)
        
        light = f"🔴 红灯 (差额 {net_local})" if abs(net_local) >= 0.01 else "🟢 绿灯 (单体平衡)"
        if u['consol_method'] == 'EQUITY':
            light = "🔵 权益法单行合并"
        elif u['consol_method'] == 'DIVESTED' and selected_period == '2027.01':
            light = "⚪ 已处置出表 (期末已清零)"
        entity_summary.append({
            "合并单元": uid, "实体名称": u['unit_name'], "本位币": u['local_curr'],
            "持股比例": f"{u['ownership_pct']*100:.0f}%", "合并方法": u['consol_method'],
            "单体借方(CNY)": debit_gc, "单体贷方(CNY)": credit_gc,
            "合并抵消净影响": elim_net, "Data Monitor 状态": light
        })
    df_mon = pd.DataFrame(entity_summary)
    def highlight_status(val):
        if "绿灯" in str(val): return "background-color: #E6F4EA; color: #137333; font-weight: 600;"
        if "红灯" in str(val): return "background-color: #FCE8E6; color: #C5221F; font-weight: 600;"
        return "background-color: #F1F3F4; color: #3C4043;"
    st.dataframe(
        df_mon.style.format({"单体借方(CNY)": "{:,.2f}", "单体贷方(CNY)": "{:,.2f}", "合并抵消净影响": "{:,.2f}"}).map(highlight_status, subset=["Data Monitor 状态"]),
        use_container_width=True, hide_index=True
    )
with tab2:
    st.subheader(f"集团合并财务报表工作底稿 ({selected_period} | {selected_gaap})")
    piv_entities = df_fact[df_fact['posting_level'].isin(['00', '01'])].pivot_table(
        index=['item_type', 'fs_item', 'fs_desc'], columns='unit_id', values='amount_gc', aggfunc='sum', fill_value=0.0
    )
    piv_pl = df_fact[df_fact['posting_level'].isin(['10', '20', '30'])].pivot_table(
        index=['item_type', 'fs_item', 'fs_desc'], columns='posting_level', values='amount_gc', aggfunc='sum', fill_value=0.0
    ).rename(columns={'10': '准则标准化(PL10)', '20': '内部抵消(PL20)', '30': '权益合并(PL30)'})
    ws = piv_entities.join(piv_pl, how='outer').fillna(0.0)
    ent_cols = [c for c in ['C1000', 'C2000', 'C3000', 'C4000'] if c in ws.columns]
    adj_cols = [c for c in ['准则标准化(PL10)', '内部抵消(PL20)', '权益合并(PL30)'] if c in ws.columns]
    ws['单体简单加总'] = ws[ent_cols].sum(axis=1)
    ws['集团最终合并数'] = ws['单体简单加总'] + ws[adj_cols].sum(axis=1)
    ordered_cols = ent_cols + ['单体简单加总'] + adj_cols + ['集团最终合并数']
    ws = ws[ordered_cols].reset_index()
    type_order = {'AST': 1, 'LEQ': 2, 'INC': 3, 'EXP': 4}
    ws['sort_key'] = ws['item_type'].map(type_order)
    ws = ws.sort_values(['sort_key', 'fs_item']).drop(columns=['sort_key'])
    tot = {'item_type': 'CHECK', 'fs_item': 'TOTAL', 'fs_desc': '试算平衡总校验 (借正贷负合计应为 0.00)'}
    for c in ordered_cols:
        tot[c] = round(ws[c].sum(), 2)
    ws = pd.concat([ws, pd.DataFrame([tot])], ignore_index=True)
    st.dataframe(
        ws.style.format({c: "{:,.2f}" for c in ordered_cols}),
        use_container_width=True,
        height=720,
        hide_index=True,
        column_config={
            "item_type": st.column_config.TextColumn("要素", width="small"),
            "fs_item": st.column_config.TextColumn("科目代码", width="small"),
            "fs_desc": st.column_config.TextColumn("合并报表项目名称", width=230, pinned=True),
        }
    )

with tab3:
    st.subheader("合并抵消前后核心科目对比与资产结构分析")
    c_left, c_right = st.columns(2)
    with c_left:
        st.markdown("**📊 合并前(单体加总) vs 合并后(最终数) 关键科目并排对比**")
        key_items = ['100100', '112200', '112299', '151100', '300100', '600100', '600199']
        df_chart = ws[ws['fs_item'].isin(key_items)][['fs_desc', '单体简单加总', '集团最终合并数']].copy()
        df_chart['单体简单加总'] = df_chart['单体简单加总'].abs()
        df_chart['集团最终合并数'] = df_chart['集团最终合并数'].abs()
        df_melt = df_chart.melt(id_vars='fs_desc', var_name='口径', value_name='金额')
        
        chart1 = alt.Chart(df_melt).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
            x=alt.X('fs_desc:N', title=None, axis=alt.Axis(labelAngle=-45, labelFontSize=11, labelLimit=200, labelOverlap=False, labelPadding=6)),
            y=alt.Y('金额:Q', title='金额 (CNY)', axis=alt.Axis(labelFontSize=11)),
            color=alt.Color('口径:N', scale=alt.Scale(domain=['单体简单加总', '集团最终合并数'], range=['#94A3B8', '#0A6ED1']), legend=alt.Legend(orient='top', title=None)),
            xOffset='口径:N',
            tooltip=['fs_desc', '口径', alt.Tooltip('金额:Q', format=',.2f')]
        ).properties(height=400)
        st.altair_chart(chart1, use_container_width=True)

    with c_right:
        st.markdown("**🏛️ 集团合并资产构成分布 (按资产类 FS Item)**")
        df_ast = ws[(ws['item_type'] == 'AST') & (ws['集团最终合并数'] > 0)][['fs_desc', '集团最终合并数']].copy()
        chart2 = alt.Chart(df_ast).mark_bar(color='#0A6ED1', cornerRadiusTopLeft=4, cornerRadiusTopRight=4, width=42).encode(
            x=alt.X('fs_desc:N', title=None, axis=alt.Axis(labelAngle=-45, labelFontSize=11, labelLimit=200, labelOverlap=False, labelPadding=6)),
            y=alt.Y('集团最终合并数:Q', title='合并期末余额 (CNY)', axis=alt.Axis(labelFontSize=11)),
            tooltip=['fs_desc', alt.Tooltip('集团最终合并数:Q', format=',.2f')]
        ).properties(height=400)
        st.altair_chart(chart2, use_container_width=True)

with tab4:
    st.subheader("ACDOCU 合并核心事实表底层明细追溯 (Audit Trail Drill-down)")
    f1, f2, f3 = st.columns(3)
    f_unit = f1.multiselect("筛选合并单元 (Unit)", sorted(df_fact['unit_id'].unique()), default=[])
    f_pl = f2.multiselect("筛选过账层级 (Posting Level)", sorted(df_fact['posting_level'].unique()), default=[])
    f_item = f3.multiselect("筛选报表项目 (FS Item)", sorted(df_fact['fs_item'].unique()), default=[])
    df_drill = df_fact.copy()
    if f_unit: df_drill = df_drill[df_drill['unit_id'].isin(f_unit)]
    if f_pl: df_drill = df_drill[df_drill['posting_level'].isin(f_pl)]
    if f_item: df_drill = df_drill[df_drill['fs_item'].isin(f_item)]
    st.caption(f"共检索到 {len(df_drill)} 条底层行项目记录 | 当前筛选集团币净额: {df_drill['amount_gc'].sum():,.2f} CNY")
    cols = ['id', 'unit_id', 'partner_unit', 'fs_item', 'fs_desc', 'subitem', 'posting_level', 'doc_type', 'accounting_principle', 'amount_lc', 'amount_gc', 'audit_remark']
    st.dataframe(df_drill[cols].style.format({"amount_lc": "{:,.2f}", "amount_gc": "{:,.2f}"}), use_container_width=True, hide_index=True)

with tab5:
    st.subheader("SAP Consolidation Monitor 任务流与门禁阻断历史日志 (sys_task_log)")
    if len(df_logs) > 0:
        st.dataframe(df_logs, use_container_width=True, hide_index=True)
    else:
        st.info("暂无门禁日志记录，可点击左侧边栏的【触发严格模式关账门禁测试】生成审计门禁日志。")

with tab6:
    st.subheader("合并现金流量表与年报附注在线直览 (2026.01 审计底稿)")
    col_cf, col_note = st.columns(2)
    with col_cf:
        st.markdown("**💵 合并现金流量表 (间接法)**")
        if os.path.exists("Consolidated_CashFlow_2026.01.xlsx"):
            st.dataframe(pd.read_excel("Consolidated_CashFlow_2026.01.xlsx"), use_container_width=True, hide_index=True)
        else:
            st.info("请先点击左侧【一键全生命周期重置与重算】生成现金流量表。")
    with col_note:
        st.markdown("**📑 附注1：关联方未达账项调节表**")
        if os.path.exists("Consolidated_Notes_Disclosure_2026.01.xlsx"):
            st.dataframe(pd.read_excel("Consolidated_Notes_Disclosure_2026.01.xlsx", sheet_name="附注1_关联方未达账项调节表"), use_container_width=True, hide_index=True)
            st.markdown("**📑 附注2：长期股权投资变动明细表**")
            st.dataframe(pd.read_excel("Consolidated_Notes_Disclosure_2026.01.xlsx", sheet_name="附注2_长期股权投资变动表"), use_container_width=True, hide_index=True)
