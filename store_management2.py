import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime, date, timedelta
import io

st.set_page_config(page_title="Factory Store Management", page_icon="🏭", layout="wide")
DB_PATH = "factory_store.db"

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS items (
        item_id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_code TEXT UNIQUE NOT NULL,
        item_name TEXT NOT NULL,
        unit TEXT NOT NULL,
        location_code TEXT NOT NULL,
        location_name TEXT NOT NULL,
        category TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS inward_entries (
        inward_id INTEGER PRIMARY KEY AUTOINCREMENT,
        bill_no TEXT NOT NULL,
        bill_date TEXT NOT NULL,
        supplier_name TEXT NOT NULL,
        security_entry_no TEXT,
        security_entry_date TEXT,
        remarks TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS inward_details (
        detail_id INTEGER PRIMARY KEY AUTOINCREMENT,
        inward_id INTEGER NOT NULL,
        item_id INTEGER NOT NULL,
        quantity REAL NOT NULL,
        unit_price REAL NOT NULL,
        gst_percent REAL NOT NULL DEFAULT 0,
        gst_amount REAL NOT NULL DEFAULT 0,
        total_value REAL NOT NULL,
        FOREIGN KEY(inward_id) REFERENCES inward_entries(inward_id),
        FOREIGN KEY(item_id) REFERENCES items(item_id)
    );
    CREATE TABLE IF NOT EXISTS fifo_batches (
        batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL,
        inward_id INTEGER NOT NULL,
        detail_id INTEGER NOT NULL,
        bill_date TEXT NOT NULL,
        qty_received REAL NOT NULL,
        qty_remaining REAL NOT NULL,
        unit_price REAL NOT NULL,
        FOREIGN KEY(item_id) REFERENCES items(item_id)
    );
    CREATE TABLE IF NOT EXISTS outward_entries (
        outward_id INTEGER PRIMARY KEY AUTOINCREMENT,
        indent_no TEXT NOT NULL,
        indent_date TEXT NOT NULL,
        location TEXT,
        section TEXT,
        issued_to TEXT,
        remarks TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS outward_details (
        detail_id INTEGER PRIMARY KEY AUTOINCREMENT,
        outward_id INTEGER NOT NULL,
        item_id INTEGER NOT NULL,
        quantity REAL NOT NULL,
        avg_unit_price REAL NOT NULL DEFAULT 0,
        total_value REAL NOT NULL DEFAULT 0,
        FOREIGN KEY(outward_id) REFERENCES outward_entries(outward_id),
        FOREIGN KEY(item_id) REFERENCES items(item_id)
    );
    """)
    conn.commit(); conn.close()

init_db()

def get_items():
    conn = get_conn()
    df = pd.read_sql("SELECT * FROM items ORDER BY item_name", conn)
    conn.close(); return df

def get_item_options():
    df = get_items()
    if df.empty: return {}
    return {f"{r['item_code']} - {r['item_name']} ({r['unit']})": r['item_id'] for _, r in df.iterrows()}

def get_current_stock(item_id=None):
    conn = get_conn()
    where = f"WHERE b.item_id = {item_id}" if item_id else ""
    query = f"""
    SELECT i.item_id, i.item_code, i.item_name, i.unit,
           i.location_code, i.location_name, i.category,
           COALESCE(SUM(b.qty_remaining),0) AS qty_in_stock,
           CASE WHEN COALESCE(SUM(b.qty_remaining),0)>0
                THEN ROUND(SUM(b.qty_remaining*b.unit_price)/SUM(b.qty_remaining),4)
                ELSE 0 END AS avg_price,
           ROUND(COALESCE(SUM(b.qty_remaining*b.unit_price),0),2) AS stock_value
    FROM items i
    LEFT JOIN fifo_batches b ON i.item_id=b.item_id AND b.qty_remaining>0
    {where}
    GROUP BY i.item_id ORDER BY i.item_name"""
    df = pd.read_sql(query, conn); conn.close(); return df

def process_fifo_issue(item_id, qty_required, conn):
    c = conn.cursor()
    batches = c.execute(
        "SELECT batch_id,qty_remaining,unit_price FROM fifo_batches "
        "WHERE item_id=? AND qty_remaining>0 ORDER BY bill_date ASC,batch_id ASC",
        (item_id,)).fetchall()
    total_available = sum(b[1] for b in batches)
    if total_available < qty_required:
        return None, f"Insufficient stock. Available: {total_available}"
    remaining = qty_required; total_cost = 0.0
    for batch_id, qty_rem, unit_price in batches:
        if remaining <= 0: break
        take = min(qty_rem, remaining)
        total_cost += take * unit_price
        c.execute("UPDATE fifo_batches SET qty_remaining=qty_remaining-? WHERE batch_id=?", (take, batch_id))
        remaining -= take
    return round(total_cost / qty_required, 4), None

def format_inr(val): return f"₹{val:,.2f}"

def delete_item(item_id):
    conn = get_conn(); c = conn.cursor()
    used = c.execute("SELECT COUNT(*) FROM inward_details WHERE item_id=?", (item_id,)).fetchone()[0]
    used += c.execute("SELECT COUNT(*) FROM outward_details WHERE item_id=?", (item_id,)).fetchone()[0]
    if used > 0:
        conn.close(); return False, "Cannot delete — item has existing inward/outward transactions."
    c.execute("DELETE FROM items WHERE item_id=?", (item_id,))
    conn.commit(); conn.close(); return True, "Item deleted successfully."

def delete_inward(inward_id):
    conn = get_conn(); c = conn.cursor()
    batches = c.execute(
        "SELECT batch_id, qty_received, qty_remaining FROM fifo_batches WHERE inward_id=?",
        (inward_id,)).fetchall()
    for b in batches:
        if round(b[1] - b[2], 6) > 0:
            conn.close()
            return False, "Cannot delete — some stock from this entry has already been issued."
    c.execute("DELETE FROM fifo_batches WHERE inward_id=?", (inward_id,))
    c.execute("DELETE FROM inward_details WHERE inward_id=?", (inward_id,))
    c.execute("DELETE FROM inward_entries WHERE inward_id=?", (inward_id,))
    conn.commit(); conn.close(); return True, "Inward entry deleted successfully."

def delete_outward(outward_id):
    conn = get_conn(); c = conn.cursor()
    details = c.execute(
        "SELECT item_id, quantity, avg_unit_price, indent_date "
        "FROM outward_details d JOIN outward_entries e ON d.outward_id=e.outward_id "
        "WHERE d.outward_id=?", (outward_id,)).fetchall()
    for item_id, qty, avg_price, indent_date in details:
        c.execute(
            "INSERT INTO fifo_batches (item_id,inward_id,detail_id,bill_date,qty_received,qty_remaining,unit_price) "
            "VALUES (?,0,0,?,?,?,?)",
            (item_id, indent_date, qty, qty, avg_price))
    c.execute("DELETE FROM outward_details WHERE outward_id=?", (outward_id,))
    c.execute("DELETE FROM outward_entries WHERE outward_id=?", (outward_id,))
    conn.commit(); conn.close(); return True, "Issue entry deleted and stock restored successfully."

st.sidebar.markdown("## 🏭 Factory Stores")
st.sidebar.markdown("---")
menu = st.sidebar.radio("📂 Navigation", [
    "🏠 Dashboard", "📦 Item Master",
    "📥 Inward Goods", "📤 Outward Goods",
    "📊 Stock Register", "📋 Reports",
])
st.sidebar.markdown("---")
st.sidebar.caption("Factory Store Management System v2.0")

if menu == "🏠 Dashboard":
    st.title("🏭 Factory Store Management System")
    st.markdown("---")
    conn = get_conn()
    total_items = pd.read_sql("SELECT COUNT(*) AS c FROM items", conn).iloc[0]['c']
    today = date.today().isoformat()
    inward_today = pd.read_sql(
        "SELECT COALESCE(SUM(d.total_value),0) AS v FROM inward_details d "
        "JOIN inward_entries e ON d.inward_id=e.inward_id WHERE e.bill_date=?",
        conn, params=(today,)).iloc[0]['v']
    outward_today = pd.read_sql(
        "SELECT COALESCE(SUM(d.total_value),0) AS v FROM outward_details d "
        "JOIN outward_entries e ON d.outward_id=e.outward_id WHERE e.indent_date=?",
        conn, params=(today,)).iloc[0]['v']
    conn.close()
    stock_df = get_current_stock()
    total_stock_value = stock_df['stock_value'].sum() if not stock_df.empty else 0
    low_stock = stock_df[stock_df['qty_in_stock']==0].shape[0] if not stock_df.empty else 0
    c1,c2,c3,c4,c5 = st.columns(5)
    c1.metric("📦 Total Items", int(total_items))
    c2.metric("📥 Inward Today", format_inr(inward_today))
    c3.metric("📤 Outward Today", format_inr(outward_today))
    c4.metric("💰 Stock Value", format_inr(total_stock_value))
    c5.metric("⚠️ Zero Stock Items", int(low_stock))
    st.markdown("---")
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("📊 Stock Summary by Category")
        if not stock_df.empty:
            cat_df = stock_df.groupby('category')['stock_value'].sum().reset_index()
            cat_df.columns = ['Category','Value (₹)']
            cat_df['Value (₹)'] = cat_df['Value (₹)'].map(lambda x: f"₹{x:,.2f}")
            st.dataframe(cat_df, use_container_width=True, hide_index=True)
        else:
            st.info("No items found. Add items in Item Master.")
    with col2:
        st.subheader("📋 Recent Inward Entries")
        conn = get_conn()
        recent_in = pd.read_sql(
            "SELECT bill_no,bill_date,supplier_name FROM inward_entries ORDER BY created_at DESC LIMIT 8", conn)
        conn.close()
        if not recent_in.empty:
            st.dataframe(recent_in, use_container_width=True, hide_index=True)
        else:
            st.info("No inward entries yet.")

elif menu == "📦 Item Master":
    st.title("📦 Item Master")
    tab1, tab2, tab3 = st.tabs(["➕ Add New Item", "📋 View All Items", "✏️ Edit / Delete Item"])
    with tab1:
        st.subheader("Add New Item")
        with st.form("item_form", clear_on_submit=True):
            c1,c2,c3 = st.columns(3)
            item_code = c1.text_input("Item Code *", placeholder="e.g. ELE-001")
            item_name = c2.text_input("Item Name *", placeholder="e.g. Bearing 6205")
            unit      = c3.selectbox("Unit *", ["Nos","Kg","Ltrs","Mtrs","Box","Set","Pair","Roll","Bag","Pkt"])
            c4,c5,c6 = st.columns(3)
            loc_code  = c4.text_input("Location Code *", placeholder="e.g. R-01-S-02")
            loc_name  = c5.text_input("Location Name *", placeholder="e.g. Rack 01 - Shelf 02")
            category  = c6.text_input("Category", placeholder="e.g. Electrical")
            if st.form_submit_button("💾 Save Item", use_container_width=True):
                if not all([item_code, item_name, unit, loc_code, loc_name]):
                    st.error("Please fill all required fields (*)")
                else:
                    try:
                        conn = get_conn()
                        conn.execute(
                            "INSERT INTO items (item_code,item_name,unit,location_code,location_name,category) VALUES (?,?,?,?,?,?)",
                            (item_code.upper(), item_name, unit, loc_code.upper(), loc_name, category))
                        conn.commit(); conn.close()
                        st.success(f"✅ Item '{item_name}' added successfully!")
                    except sqlite3.IntegrityError:
                        st.error(f"Item Code '{item_code}' already exists.")
    with tab2:
        st.subheader("All Items")
        df = get_items()
        if df.empty:
            st.info("No items added yet.")
        else:
            search = st.text_input("🔍 Search", placeholder="Item name, code or category...")
            if search:
                df = df[df.apply(lambda r: search.lower() in str(r).lower(), axis=1)]
            st.dataframe(df[['item_code','item_name','unit','location_code','location_name','category']],
                         use_container_width=True, hide_index=True)
            buf = io.BytesIO(); df.to_excel(buf, index=False)
            st.download_button("⬇️ Download Item Master", buf.getvalue(), "item_master.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    with tab3:
        st.subheader("Edit or Delete an Item")
        df_items = get_items()
        if df_items.empty:
            st.info("No items to edit.")
        else:
            item_opts_edit = {f"{r['item_code']} - {r['item_name']}": r['item_id'] for _, r in df_items.iterrows()}
            sel_edit = st.selectbox("Select Item to Edit / Delete", list(item_opts_edit.keys()), key="edit_item_sel")
            sel_item_id = item_opts_edit[sel_edit]
            row = df_items[df_items['item_id']==sel_item_id].iloc[0]
            st.markdown("---")
            st.markdown("#### ✏️ Edit Item Details")
            with st.form("edit_item_form"):
                ec1,ec2,ec3 = st.columns(3)
                new_code  = ec1.text_input("Item Code", value=row['item_code'])
                new_name  = ec2.text_input("Item Name", value=row['item_name'])
                unit_list = ["Nos","Kg","Ltrs","Mtrs","Box","Set","Pair","Roll","Bag","Pkt"]
                unit_idx  = unit_list.index(row['unit']) if row['unit'] in unit_list else 0
                new_unit  = ec3.selectbox("Unit", unit_list, index=unit_idx)
                ec4,ec5,ec6 = st.columns(3)
                new_lcode = ec4.text_input("Location Code", value=row['location_code'])
                new_lname = ec5.text_input("Location Name", value=row['location_name'])
                new_cat   = ec6.text_input("Category", value=row['category'])
                if st.form_submit_button("💾 Update Item", use_container_width=True):
                    if not all([new_code, new_name, new_lcode, new_lname]):
                        st.error("All fields are required.")
                    else:
                        try:
                            conn = get_conn()
                            conn.execute(
                                "UPDATE items SET item_code=?,item_name=?,unit=?,location_code=?,location_name=?,category=? WHERE item_id=?",
                                (new_code.upper(), new_name, new_unit, new_lcode.upper(), new_lname, new_cat, sel_item_id))
                            conn.commit(); conn.close()
                            st.success(f"✅ Item '{new_name}' updated successfully!")
                            st.rerun()
                        except sqlite3.IntegrityError:
                            st.error(f"Item Code '{new_code}' already exists for another item.")
            st.markdown("---")
            st.markdown("#### 🗑️ Delete Item")
            st.warning(f"⚠️ You are about to delete: **{row['item_name']}** ({row['item_code']})")
            st.caption("Note: Items with existing inward/outward transactions cannot be deleted.")
            if st.button("🗑️ Delete This Item", type="primary", key="del_item_btn"):
                success, msg = delete_item(sel_item_id)
                if success: st.success(f"✅ {msg}"); st.rerun()
                else: st.error(f"❌ {msg}")

elif menu == "📥 Inward Goods":
    st.title("📥 Inward Goods Entry")
    tab1, tab2 = st.tabs(["➕ New Inward Entry", "📋 View / Edit / Delete Records"])
    with tab1:
        if "inward_items" not in st.session_state:
            st.session_state.inward_items = []
        st.subheader("Bill / Invoice Details")
        c1,c2,c3 = st.columns(3)
        bill_no       = c1.text_input("Bill No. *", key="in_bill_no")
        bill_date     = c2.date_input("Bill Date *", key="in_bill_date")
        supplier_name = c3.text_input("Supplier Name *", key="in_supplier")
        c4,c5,c6 = st.columns(3)
        sec_entry_no   = c4.text_input("Security Entry No.", key="in_sec_no")
        sec_entry_date = c5.date_input("Security Entry Date", key="in_sec_date")
        remarks        = c6.text_input("Remarks", key="in_remarks")
        st.markdown("---")
        st.subheader("➕ Add Items to Entry")
        item_opts = get_item_options()
        if not item_opts:
            st.warning("⚠️ No items found. Please add items in Item Master first.")
        else:
            with st.form("add_inward_item", clear_on_submit=True):
                ic1,ic2,ic3 = st.columns([3,1,1])
                sel_item   = ic1.selectbox("Select Item *", list(item_opts.keys()))
                quantity   = ic2.number_input("Quantity *", min_value=0.001, step=0.001, format="%.3f")
                unit_price = ic3.number_input("Unit Price (₹) *", min_value=0.01, step=0.01, format="%.4f")
                ic4,ic5,_ = st.columns([1,1,2])
                gst_pct   = ic4.number_input("GST %", min_value=0.0, max_value=28.0, value=18.0, step=0.5)
                gst_amt   = round(quantity * unit_price * gst_pct / 100, 2)
                ic5.metric("GST Amount", format_inr(gst_amt))
                if st.form_submit_button("➕ Add Item to List"):
                    total_val = round(quantity * unit_price + gst_amt, 2)
                    st.session_state.inward_items.append({
                        "item_id": item_opts[sel_item], "item_name": sel_item,
                        "quantity": quantity, "unit_price": unit_price,
                        "gst_percent": gst_pct, "gst_amount": gst_amt, "total_value": total_val,
                    })
                    st.rerun()
        if st.session_state.inward_items:
            st.subheader("🧾 Items List")
            df_items = pd.DataFrame(st.session_state.inward_items)
            st.dataframe(df_items[['item_name','quantity','unit_price','gst_percent','gst_amount','total_value']],
                         use_container_width=True, hide_index=True)
            st.markdown(f"**Grand Total: {format_inr(df_items['total_value'].sum())}**")
            ccl, csub = st.columns(2)
            if ccl.button("🗑️ Clear All Items", use_container_width=True):
                st.session_state.inward_items = []; st.rerun()
            if csub.button("✅ Submit Inward Entry", type="primary", use_container_width=True):
                if not all([bill_no, supplier_name]):
                    st.error("Bill No. and Supplier Name are required.")
                else:
                    conn = get_conn(); c = conn.cursor()
                    c.execute(
                        "INSERT INTO inward_entries (bill_no,bill_date,supplier_name,security_entry_no,security_entry_date,remarks) VALUES (?,?,?,?,?,?)",
                        (bill_no, bill_date.isoformat(), supplier_name, sec_entry_no,
                         sec_entry_date.isoformat() if sec_entry_date else None, remarks))
                    inward_id = c.lastrowid
                    for row in st.session_state.inward_items:
                        c.execute(
                            "INSERT INTO inward_details (inward_id,item_id,quantity,unit_price,gst_percent,gst_amount,total_value) VALUES (?,?,?,?,?,?,?)",
                            (inward_id, row['item_id'], row['quantity'], row['unit_price'],
                             row['gst_percent'], row['gst_amount'], row['total_value']))
                        detail_id = c.lastrowid
                        c.execute(
                            "INSERT INTO fifo_batches (item_id,inward_id,detail_id,bill_date,qty_received,qty_remaining,unit_price) VALUES (?,?,?,?,?,?,?)",
                            (row['item_id'], inward_id, detail_id, bill_date.isoformat(),
                             row['quantity'], row['quantity'], row['unit_price']))
                    conn.commit(); conn.close()
                    st.success(f"✅ Inward Entry saved! Bill No: {bill_no}")
                    st.session_state.inward_items = []; st.rerun()
    with tab2:
        st.subheader("Inward Records")
        conn = get_conn()
        df_all = pd.read_sql("SELECT * FROM inward_entries ORDER BY bill_date DESC", conn)
        conn.close()
        if df_all.empty:
            st.info("No inward records found.")
        else:
            fc1,fc2,fc3 = st.columns(3)
            search_bill = fc1.text_input("🔍 Search Bill No / Supplier", key="in_srch")
            from_dt = fc2.date_input("From Date", value=date.today()-timedelta(days=30), key="in_from")
            to_dt   = fc3.date_input("To Date", value=date.today(), key="in_to")
            df_all['bill_date'] = pd.to_datetime(df_all['bill_date'])
            df_f = df_all[(df_all['bill_date']>=pd.Timestamp(from_dt)) & (df_all['bill_date']<=pd.Timestamp(to_dt))]
            if search_bill:
                df_f = df_f[df_f.apply(lambda r: search_bill.lower() in str(r).lower(), axis=1)]
            st.dataframe(df_f[['bill_no','bill_date','supplier_name','security_entry_no','security_entry_date','remarks']],
                         use_container_width=True, hide_index=True)
            if not df_f.empty:
                sel_id = st.selectbox("Select Entry to View / Edit / Delete",
                                      df_f['inward_id'].tolist(),
                                      format_func=lambda x: f"Bill: {df_f.set_index('inward_id').loc[x,'bill_no']} | {df_f.set_index('inward_id').loc[x,'supplier_name']}")
                conn = get_conn()
                entry = pd.read_sql("SELECT * FROM inward_entries WHERE inward_id=?", conn, params=(sel_id,)).iloc[0]
                det = pd.read_sql(
                    "SELECT d.*,i.item_code,i.item_name,i.unit FROM inward_details d "
                    "JOIN items i ON d.item_id=i.item_id WHERE d.inward_id=?", conn, params=(sel_id,))
                conn.close()
                action_tab1, action_tab2, action_tab3 = st.tabs(["📄 View Details", "✏️ Edit Entry", "🗑️ Delete Entry"])
                with action_tab1:
                    st.dataframe(det[['item_code','item_name','unit','quantity','unit_price','gst_percent','gst_amount','total_value']],
                                 use_container_width=True, hide_index=True)
                    st.metric("Total Bill Value", format_inr(det['total_value'].sum()))
                with action_tab2:
                    st.markdown("##### Edit Bill Header")
                    with st.form("edit_inward_form"):
                        ei1,ei2,ei3 = st.columns(3)
                        new_bill_no  = ei1.text_input("Bill No.", value=entry['bill_no'])
                        new_bill_dt  = ei2.date_input("Bill Date", value=date.fromisoformat(str(entry['bill_date'])[:10]))
                        new_supplier = ei3.text_input("Supplier Name", value=entry['supplier_name'])
                        ei4,ei5,ei6 = st.columns(3)
                        new_sec_no   = ei4.text_input("Security Entry No.", value=entry['security_entry_no'] or "")
                        new_sec_dt_val = entry['security_entry_date']
                        new_sec_dt   = ei5.date_input("Security Entry Date",
                                                       value=date.fromisoformat(str(new_sec_dt_val)[:10]) if new_sec_dt_val else date.today())
                        new_remarks  = ei6.text_input("Remarks", value=entry['remarks'] or "")
                        if st.form_submit_button("💾 Update Header", use_container_width=True):
                            conn = get_conn()
                            conn.execute(
                                "UPDATE inward_entries SET bill_no=?,bill_date=?,supplier_name=?,security_entry_no=?,security_entry_date=?,remarks=? WHERE inward_id=?",
                                (new_bill_no, new_bill_dt.isoformat(), new_supplier, new_sec_no, new_sec_dt.isoformat(), new_remarks, sel_id))
                            conn.commit(); conn.close()
                            st.success("✅ Inward entry header updated!"); st.rerun()
                    st.markdown("##### Edit Item Quantities / Price")
                    st.caption("⚠️ Quantity can only be increased or kept the same if stock is partially consumed.")
                    for _, drow in det.iterrows():
                        with st.expander(f"📦 {drow['item_name']} | Qty: {drow['quantity']} | Price: ₹{drow['unit_price']}"):
                            conn = get_conn()
                            batch = conn.execute(
                                "SELECT qty_received,qty_remaining FROM fifo_batches WHERE detail_id=?",
                                (drow['detail_id'],)).fetchone()
                            conn.close()
                            consumed = round(batch[0] - batch[1], 6) if batch else 0
                            with st.form(f"edit_det_{drow['detail_id']}"):
                                dc1,dc2,dc3 = st.columns(3)
                                new_qty   = dc1.number_input("New Quantity", value=float(drow['quantity']), min_value=float(consumed), step=0.001, format="%.3f")
                                new_price = dc2.number_input("Unit Price (₹)", value=float(drow['unit_price']), min_value=0.01, step=0.01, format="%.4f")
                                new_gst   = dc3.number_input("GST %", value=float(drow['gst_percent']), min_value=0.0, max_value=28.0, step=0.5)
                                if st.form_submit_button(f"💾 Update Item", use_container_width=True):
                                    new_gst_amt = round(new_qty * new_price * new_gst / 100, 2)
                                    new_total   = round(new_qty * new_price + new_gst_amt, 2)
                                    conn = get_conn()
                                    conn.execute(
                                        "UPDATE inward_details SET quantity=?,unit_price=?,gst_percent=?,gst_amount=?,total_value=? WHERE detail_id=?",
                                        (new_qty, new_price, new_gst, new_gst_amt, new_total, drow['detail_id']))
                                    conn.execute(
                                        "UPDATE fifo_batches SET qty_received=?,qty_remaining=?,unit_price=? WHERE detail_id=?",
                                        (new_qty, new_qty - consumed, new_price, drow['detail_id']))
                                    conn.commit(); conn.close()
                                    st.success("✅ Item updated!"); st.rerun()
                with action_tab3:
                    conn = get_conn()
                    batches_check = conn.execute(
                        "SELECT SUM(qty_received-qty_remaining) FROM fifo_batches WHERE inward_id=?",
                        (sel_id,)).fetchone()[0] or 0
                    conn.close()
                    st.warning(f"⚠️ You are about to delete Bill No: **{entry['bill_no']}** from **{entry['supplier_name']}**")
                    if batches_check > 0:
                        st.error(f"❌ Cannot delete — {round(batches_check,3)} units from this entry have already been issued.")
                    else:
                        st.info("✅ Safe to delete — no stock from this entry has been issued yet.")
                        confirm = st.checkbox("I confirm I want to delete this entry permanently")
                        if confirm:
                            if st.button("🗑️ Confirm Delete Inward Entry", type="primary"):
                                success, msg = delete_inward(sel_id)
                                if success: st.success(f"✅ {msg}"); st.rerun()
                                else: st.error(f"❌ {msg}")

elif menu == "📤 Outward Goods":
    st.title("📤 Outward Goods Issue Entry")
    tab1, tab2 = st.tabs(["➕ New Issue Entry", "📋 View / Edit / Delete Records"])
    with tab1:
        if "outward_items" not in st.session_state:
            st.session_state.outward_items = []
        st.subheader("Indent / Issue Details")
        c1,c2,c3 = st.columns(3)
        indent_no   = c1.text_input("Indent No. *", key="out_indent")
        indent_date = c2.date_input("Indent Date *", key="out_date")
        issued_to   = c3.text_input("Issued To *", key="out_issued_to")
        c4,c5,c6 = st.columns(3)
        location    = c4.text_input("Location / Department", key="out_location")
        section     = c5.text_input("Section", key="out_section")
        out_remarks = c6.text_input("Remarks", key="out_remarks")
        st.markdown("---")
        st.subheader("➕ Add Items to Issue")
        item_opts = get_item_options(); stock_df = get_current_stock()
        if not item_opts:
            st.warning("⚠️ No items found. Please add items in Item Master first.")
        else:
            with st.form("add_outward_item", clear_on_submit=True):
                oc1,oc2 = st.columns([3,1])
                sel_item_out = oc1.selectbox("Select Item *", list(item_opts.keys()))
                qty_out      = oc2.number_input("Quantity *", min_value=0.001, step=0.001, format="%.3f")
                if not stock_df.empty:
                    iid = item_opts.get(sel_item_out)
                    avail = stock_df[stock_df['item_id']==iid]['qty_in_stock'].values
                    avg_p = stock_df[stock_df['item_id']==iid]['avg_price'].values
                    st.caption(f"📦 Available: **{avail[0] if len(avail)>0 else 0}** | Avg Price: **{format_inr(avg_p[0] if len(avg_p)>0 else 0)}**")
                if st.form_submit_button("➕ Add Item to List"):
                    item_id_sel = item_opts[sel_item_out]
                    avail_row = stock_df[stock_df['item_id']==item_id_sel]
                    avail_qty = avail_row['qty_in_stock'].values[0] if not avail_row.empty else 0
                    if qty_out > avail_qty:
                        st.error(f"⚠️ Only {avail_qty} in stock.")
                    else:
                        st.session_state.outward_items.append({"item_id": item_id_sel, "item_name": sel_item_out, "quantity": qty_out})
                        st.rerun()
        if st.session_state.outward_items:
            st.subheader("🧾 Items to Issue")
            st.dataframe(pd.DataFrame(st.session_state.outward_items)[['item_name','quantity']],
                         use_container_width=True, hide_index=True)
            co1,co2 = st.columns(2)
            if co1.button("🗑️ Clear All", use_container_width=True):
                st.session_state.outward_items = []; st.rerun()
            if co2.button("✅ Submit Issue Entry", type="primary", use_container_width=True):
                if not all([indent_no, issued_to]):
                    st.error("Indent No. and Issued To are required.")
                else:
                    conn = get_conn(); c = conn.cursor()
                    c.execute(
                        "INSERT INTO outward_entries (indent_no,indent_date,location,section,issued_to,remarks) VALUES (?,?,?,?,?,?)",
                        (indent_no, indent_date.isoformat(), location, section, issued_to, out_remarks))
                    outward_id = c.lastrowid; errors = []
                    for row in st.session_state.outward_items:
                        avg_price, err = process_fifo_issue(row['item_id'], row['quantity'], conn)
                        if err: errors.append(f"{row['item_name']}: {err}")
                        else:
                            c.execute(
                                "INSERT INTO outward_details (outward_id,item_id,quantity,avg_unit_price,total_value) VALUES (?,?,?,?,?)",
                                (outward_id, row['item_id'], row['quantity'], avg_price, round(avg_price*row['quantity'],2)))
                    if errors:
                        conn.rollback(); conn.close()
                        for e in errors: st.error(e)
                    else:
                        conn.commit(); conn.close()
                        st.success(f"✅ Issue Entry saved! Indent No: {indent_no}")
                        st.session_state.outward_items = []; st.rerun()
    with tab2:
        st.subheader("Issue Records")
        conn = get_conn()
        df_out_all = pd.read_sql("SELECT * FROM outward_entries ORDER BY indent_date DESC", conn)
        conn.close()
        if df_out_all.empty:
            st.info("No issue records found.")
        else:
            oc1,oc2,oc3 = st.columns(3)
            srch   = oc1.text_input("🔍 Search", key="out_srch")
            fr_dt  = oc2.date_input("From", value=date.today()-timedelta(days=30), key="out_from")
            to_dt2 = oc3.date_input("To",   value=date.today(), key="out_to")
            df_out_all['indent_date'] = pd.to_datetime(df_out_all['indent_date'])
            df_of = df_out_all[(df_out_all['indent_date']>=pd.Timestamp(fr_dt)) & (df_out_all['indent_date']<=pd.Timestamp(to_dt2))]
            if srch:
                df_of = df_of[df_of.apply(lambda r: srch.lower() in str(r).lower(), axis=1)]
            st.dataframe(df_of[['indent_no','indent_date','issued_to','location','section','remarks']],
                         use_container_width=True, hide_index=True)
            if not df_of.empty:
                sel_out_id = st.selectbox("Select Entry to View / Edit / Delete",
                                           df_of['outward_id'].tolist(),
                                           format_func=lambda x: f"Indent: {df_of.set_index('outward_id').loc[x,'indent_no']} | {df_of.set_index('outward_id').loc[x,'issued_to']}")
                conn = get_conn()
                out_entry = pd.read_sql("SELECT * FROM outward_entries WHERE outward_id=?", conn, params=(sel_out_id,)).iloc[0]
                det2 = pd.read_sql(
                    "SELECT d.*,i.item_code,i.item_name,i.unit FROM outward_details d "
                    "JOIN items i ON d.item_id=i.item_id WHERE d.outward_id=?", conn, params=(sel_out_id,))
                conn.close()
                oat1, oat2, oat3 = st.tabs(["📄 View Details", "✏️ Edit Entry", "🗑️ Delete Entry"])
                with oat1:
                    st.dataframe(det2[['item_code','item_name','unit','quantity','avg_unit_price','total_value']],
                                 use_container_width=True, hide_index=True)
                    st.metric("Total Issued Value", format_inr(det2['total_value'].sum()))
                with oat2:
                    st.markdown("##### Edit Issue Header Details")
                    st.caption("💡 To change items or quantities, delete and re-enter the issue.")
                    with st.form("edit_outward_form"):
                        eo1,eo2,eo3 = st.columns(3)
                        new_indent   = eo1.text_input("Indent No.", value=out_entry['indent_no'])
                        new_ind_dt   = eo2.date_input("Indent Date", value=date.fromisoformat(str(out_entry['indent_date'])[:10]))
                        new_issued   = eo3.text_input("Issued To", value=out_entry['issued_to'] or "")
                        eo4,eo5,eo6 = st.columns(3)
                        new_loc      = eo4.text_input("Location", value=out_entry['location'] or "")
                        new_sec      = eo5.text_input("Section", value=out_entry['section'] or "")
                        new_rem      = eo6.text_input("Remarks", value=out_entry['remarks'] or "")
                        if st.form_submit_button("💾 Update Issue Entry", use_container_width=True):
                            conn = get_conn()
                            conn.execute(
                                "UPDATE outward_entries SET indent_no=?,indent_date=?,location=?,section=?,issued_to=?,remarks=? WHERE outward_id=?",
                                (new_indent, new_ind_dt.isoformat(), new_loc, new_sec, new_issued, new_rem, sel_out_id))
                            conn.commit(); conn.close()
                            st.success("✅ Issue entry updated!"); st.rerun()
                with oat3:
                    st.warning(f"⚠️ Deleting Indent No: **{out_entry['indent_no']}** issued to **{out_entry['issued_to']}**")
                    st.info("ℹ️ Deleting this entry will restore the issued stock back to the store automatically.")
                    st.dataframe(det2[['item_name','quantity','avg_unit_price']], use_container_width=True, hide_index=True)
                    confirm_out = st.checkbox("I confirm I want to delete this issue entry and restore stock")
                    if confirm_out:
                        if st.button("🗑️ Confirm Delete Issue Entry", type="primary"):
                            success, msg = delete_outward(sel_out_id)
                            if success: st.success(f"✅ {msg}"); st.rerun()
                            else: st.error(f"❌ {msg}")

elif menu == "📊 Stock Register":
    st.title("📊 Balance Stock Register")
    st.caption("Stock valued on Average Price (FIFO consumption basis)")
    st.markdown("---")
    col1,col2 = st.columns([3,1])
    search_stk = col1.text_input("🔍 Filter by Item Name / Code / Location / Category")
    show_zero  = col2.checkbox("Show Zero Stock", value=False)
    stock_df = get_current_stock()
    if not stock_df.empty:
        if not show_zero: stock_df = stock_df[stock_df['qty_in_stock']>0]
        if search_stk: stock_df = stock_df[stock_df.apply(lambda r: search_stk.lower() in str(r).lower(), axis=1)]
    if stock_df.empty:
        st.info("No stock data available.")
    else:
        stock_df['stock_value_fmt'] = stock_df['stock_value'].map(lambda x: f"₹{x:,.2f}")
        stock_df['avg_price_fmt']   = stock_df['avg_price'].map(lambda x: f"₹{x:,.4f}")
        st.dataframe(stock_df[['item_code','item_name','unit','location_code','location_name','category','qty_in_stock','avg_price_fmt','stock_value_fmt']].rename(columns={
            'item_code':'Code','item_name':'Item Name','unit':'Unit','location_code':'Loc Code',
            'location_name':'Location','category':'Category','qty_in_stock':'Qty in Stock',
            'avg_price_fmt':'Avg Price','stock_value_fmt':'Stock Value'}),
            use_container_width=True, hide_index=True)
        rc1,rc2,rc3 = st.columns(3)
        rc1.metric("Total Items", len(stock_df))
        rc2.metric("Total Stock Value", format_inr(stock_df['stock_value'].sum()))
        rc3.metric("Zero Stock Items", int((stock_df['qty_in_stock']==0).sum()))
        buf = io.BytesIO(); stock_df.to_excel(buf, index=False)
        st.download_button("⬇️ Download Stock Register", buf.getvalue(), "stock_register.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.markdown("---")
        st.subheader("📦 FIFO Batch Details")
        item_opts2 = get_item_options()
        if item_opts2:
            sel_batch_item = st.selectbox("Select Item to view FIFO batches", list(item_opts2.keys()), key="fifo_view")
            conn = get_conn()
            batch_df = pd.read_sql(
                "SELECT batch_id,bill_date,qty_received,qty_remaining,unit_price,"
                "ROUND(qty_remaining*unit_price,2) AS batch_value "
                "FROM fifo_batches WHERE item_id=? AND qty_remaining>0 ORDER BY bill_date ASC",
                conn, params=(item_opts2[sel_batch_item],))
            conn.close()
            if not batch_df.empty: st.dataframe(batch_df, use_container_width=True, hide_index=True)
            else: st.info("No remaining FIFO batches for this item.")

elif menu == "📋 Reports":
    st.title("📋 Reports")
    report_type = st.selectbox("📂 Select Report", [
        "Stock Summary Report","Inward Goods Register","Outward Goods Register",
        "Item Ledger (Stock Movement)","Supplier-wise Inward Report",
        "Location-wise Stock Report","Section-wise Issue Report",
    ])
    st.markdown("---")
    col1,col2,col3 = st.columns([2,2,2])
    from_date = col1.date_input("From Date", value=date.today().replace(day=1), key="rpt_from")
    to_date   = col2.date_input("To Date",   value=date.today(), key="rpt_to")
    conn = get_conn(); df = pd.DataFrame()
    if report_type == "Stock Summary Report":
        st.subheader("📊 Stock Summary Report")
        df = get_current_stock()
        if not df.empty:
            cat_filter = col3.multiselect("Filter by Category", df['category'].unique().tolist())
            if cat_filter: df = df[df['category'].isin(cat_filter)]
            df['avg_price']   = df['avg_price'].map(lambda x: f"₹{x:,.4f}")
            df['stock_value'] = df['stock_value'].map(lambda x: f"₹{x:,.2f}")
            st.dataframe(df[['item_code','item_name','unit','location_code','location_name','category','qty_in_stock','avg_price','stock_value']],
                         use_container_width=True, hide_index=True)
    elif report_type == "Inward Goods Register":
        st.subheader("📥 Inward Goods Register")
        df = pd.read_sql("""
            SELECT e.bill_no,e.bill_date,e.supplier_name,i.item_code,it.item_name,it.unit,
                   i.quantity,i.unit_price,i.gst_percent,i.gst_amount,i.total_value,
                   e.security_entry_no,e.security_entry_date
            FROM inward_entries e JOIN inward_details i ON e.inward_id=i.inward_id
            JOIN items it ON i.item_id=it.item_id
            WHERE e.bill_date BETWEEN ? AND ? ORDER BY e.bill_date,e.bill_no
        """, conn, params=(from_date.isoformat(), to_date.isoformat()))
        if df.empty: st.info("No inward records for selected period.")
        else:
            item_filter = col3.multiselect("Filter Item", df['item_name'].unique().tolist())
            if item_filter: df = df[df['item_name'].isin(item_filter)]
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.metric("Total Inward Value", format_inr(df['total_value'].sum()))
    elif report_type == "Outward Goods Register":
        st.subheader("📤 Outward Goods Register")
        df = pd.read_sql("""
            SELECT e.indent_no,e.indent_date,e.issued_to,e.location,e.section,
                   it.item_code,it.item_name,it.unit,d.quantity,d.avg_unit_price,d.total_value
            FROM outward_entries e JOIN outward_details d ON e.outward_id=d.outward_id
            JOIN items it ON d.item_id=it.item_id
            WHERE e.indent_date BETWEEN ? AND ? ORDER BY e.indent_date,e.indent_no
        """, conn, params=(from_date.isoformat(), to_date.isoformat()))
        if df.empty: st.info("No issue records for selected period.")
        else:
            item_filter2 = col3.multiselect("Filter Item", df['item_name'].unique().tolist())
            if item_filter2: df = df[df['item_name'].isin(item_filter2)]
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.metric("Total Issued Value", format_inr(df['total_value'].sum()))
    elif report_type == "Item Ledger (Stock Movement)":
        st.subheader("📒 Item Ledger - Stock Movement")
        item_opts3 = get_item_options()
        if item_opts3:
            sel_item_ledger = col3.selectbox("Select Item *", list(item_opts3.keys()))
            item_id_led = item_opts3[sel_item_ledger]
            inward_led = pd.read_sql("""
                SELECT e.bill_date AS txn_date,'INWARD' AS txn_type,e.bill_no AS ref_no,
                       e.supplier_name AS party,d.quantity AS qty_in,0 AS qty_out,
                       d.unit_price AS rate,d.total_value AS value
                FROM inward_entries e JOIN inward_details d ON e.inward_id=d.inward_id
                WHERE d.item_id=? AND e.bill_date BETWEEN ? AND ?
            """, conn, params=(item_id_led, from_date.isoformat(), to_date.isoformat()))
            outward_led = pd.read_sql("""
                SELECT e.indent_date AS txn_date,'ISSUE' AS txn_type,e.indent_no AS ref_no,
                       e.issued_to AS party,0 AS qty_in,d.quantity AS qty_out,
                       d.avg_unit_price AS rate,d.total_value AS value
                FROM outward_entries e JOIN outward_details d ON e.outward_id=d.outward_id
                WHERE d.item_id=? AND e.indent_date BETWEEN ? AND ?
            """, conn, params=(item_id_led, from_date.isoformat(), to_date.isoformat()))
            ledger_df = pd.concat([inward_led, outward_led]).sort_values('txn_date').reset_index(drop=True)
            if ledger_df.empty: st.info("No transactions for this item in selected period.")
            else:
                ledger_df['balance'] = (ledger_df['qty_in'] - ledger_df['qty_out']).cumsum()
                st.dataframe(ledger_df, use_container_width=True, hide_index=True)
                m1,m2,m3 = st.columns(3)
                m1.metric("Total Received", ledger_df['qty_in'].sum())
                m2.metric("Total Issued",   ledger_df['qty_out'].sum())
                m3.metric("Closing Balance", ledger_df['qty_in'].sum()-ledger_df['qty_out'].sum())
    elif report_type == "Supplier-wise Inward Report":
        st.subheader("🏢 Supplier-wise Inward Report")
        df = pd.read_sql("""
            SELECT e.supplier_name,COUNT(DISTINCT e.inward_id) AS bills,
                   SUM(d.quantity) AS total_qty,SUM(d.total_value) AS total_value
            FROM inward_entries e JOIN inward_details d ON e.inward_id=d.inward_id
            WHERE e.bill_date BETWEEN ? AND ?
            GROUP BY e.supplier_name ORDER BY total_value DESC
        """, conn, params=(from_date.isoformat(), to_date.isoformat()))
        if df.empty: st.info("No data for selected period.")
        else:
            df['total_value'] = df['total_value'].map(lambda x: f"₹{x:,.2f}")
            st.dataframe(df, use_container_width=True, hide_index=True)
    elif report_type == "Location-wise Stock Report":
        st.subheader("📍 Location-wise Stock Report")
        stock_loc = get_current_stock()
        if not stock_loc.empty:
            grp = stock_loc.groupby(['location_code','location_name']).agg(
                Items=('item_id','count'), Total_Qty=('qty_in_stock','sum'), Stock_Value=('stock_value','sum')
            ).reset_index()
            grp['Stock_Value'] = grp['Stock_Value'].map(lambda x: f"₹{x:,.2f}")
            st.dataframe(grp, use_container_width=True, hide_index=True)
    elif report_type == "Section-wise Issue Report":
        st.subheader("🏗️ Section-wise Issue Report")
        df = pd.read_sql("""
            SELECT e.section,e.location,COUNT(DISTINCT e.outward_id) AS indents,
                   SUM(d.quantity) AS total_qty,SUM(d.total_value) AS total_value
            FROM outward_entries e JOIN outward_details d ON e.outward_id=d.outward_id
            WHERE e.indent_date BETWEEN ? AND ?
            GROUP BY e.section,e.location ORDER BY total_value DESC
        """, conn, params=(from_date.isoformat(), to_date.isoformat()))
        if df.empty: st.info("No issue data for selected period.")
        else:
            df['total_value'] = df['total_value'].map(lambda x: f"₹{x:,.2f}")
            st.dataframe(df, use_container_width=True, hide_index=True)
    conn.close()
    if not df.empty:
        buf2 = io.BytesIO()
        try:
            df.to_excel(buf2, index=False)
            st.download_button("⬇️ Download Report as Excel", buf2.getvalue(),
                               f"report_{report_type.replace(' ','_')}.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        except: pass
