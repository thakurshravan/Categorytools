import io
import os
import pandas as pd
from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import HTMLResponse, StreamingResponse

app = FastAPI(title="Brand Promo Claims Calculator")

# --- Dynamic Path Resolution to match your filename ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Matches the exact custom root filename uploaded in your system
TEMPLATE_PATH = os.path.join(BASE_DIR, "templates index.html")

# Global internal app cache to temporarily hold calculated datasets for export streams
app.state.final_report = None


def get_base_html() -> str:
    """Reads your template file cleanly as a plain text string from the workspace root."""
    if not os.path.exists(TEMPLATE_PATH):
        # Fallback safeguard in case you decide to rename it inside a folder later
        alternative_path = os.path.join(BASE_DIR, "templates", "index.html")
        if os.path.exists(alternative_path):
            with open(alternative_path, "r", encoding="utf-8") as f:
                return f.read()
        raise FileNotFoundError(
            f"Could not find template file. System searched root for 'templates index.html' "
            f"and folder for 'templates/index.html'. Available files: {os.listdir(BASE_DIR)}"
        )
        
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/", response_class=HTMLResponse)
async def read_index():
    """
    Bypasses Jinja2 entirely by rendering the raw HTML template string,
    removing any chance of the Python 3.14 dictionary cache bug.
    """
    try:
        html_content = get_base_html()
    except Exception as e:
        return HTMLResponse(content=f"<h3>Configuration Error</h3><p>{str(e)}</p>", status_code=500)
    
    # Clean up Jinja syntax placeholder anchors for the initial load
    html_content = html_content.replace("{% if diagnostics %}{% endif %}", "")
    html_content = html_content.replace("{% if error %}{% endif %}", "")
    html_content = html_content.replace("{% if result %}{% endif %}", "")
    
    return HTMLResponse(content=html_content)


@app.post("/calculate", response_class=HTMLResponse)
async def calculate_claims(
    claim_sales: UploadFile = File(...),
    claim_promo: UploadFile = File(...)
):
    try:
        base_html = get_base_html()
    except Exception as e:
        return HTMLResponse(content=f"<h3>Configuration Error</h3><p>{str(e)}</p>", status_code=500)
    
    try:
        # 1. Dynamically read all file streams safely (.csv or .xlsx)
        if claim_sales.filename.endswith('.csv'):
            df_sales = pd.read_csv(io.BytesIO(await claim_sales.read()))
        else:
            df_sales = pd.read_excel(io.BytesIO(await claim_sales.read()))

        if claim_promo.filename.endswith('.csv'):
            df_promo = pd.read_csv(io.BytesIO(await claim_promo.read()))
        else:
            df_promo = pd.read_excel(io.BytesIO(await claim_promo.read()))

        # Live diagnostic tracking logs array list
        diagnostics_list = []
        diagnostics_list.append(f"Initial Sales Rows Uploaded: {len(df_sales)}")
        diagnostics_list.append(f"Initial Promo Rows Uploaded: {len(df_promo)}")

        # 2. Harmonize column name headers to ensure 'Article Name' is strictly uniform
        if "Code" in df_promo.columns and "Article Name" not in df_promo.columns:
            df_promo = df_promo.rename(columns={"Code": "Article Name"})

        # 3. Safe Date Parsing & Normalization (Strips hidden times)
        df_sales["Invoice Created On"] = pd.to_datetime(df_sales["Invoice Created On"], errors="coerce").dt.normalize()
        df_promo["From"] = pd.to_datetime(df_promo["From"], errors="coerce").dt.normalize()
        df_promo["To"] = pd.to_datetime(df_promo["To"], errors="coerce").dt.normalize()

        # 4. Clean and normalize string matching keys (strip whitespace, force uppercase)
        df_sales["Article Name"] = df_sales["Article Name"].astype(str).str.strip().str.upper()
        df_promo["Article Name"] = df_promo["Article Name"].astype(str).str.strip().str.upper()

        # 5. Handle numbers safely to prevent broken aggregation calculations
        df_sales["Invoice Quantity"] = pd.to_numeric(df_sales["Invoice Quantity"], errors="coerce").fillna(0)
        df_promo["GV"] = pd.to_numeric(df_promo["GV"], errors="coerce").fillna(0)

        # 6. Step 1 Join: Link Sales directly to the Promotion parameters using common 'Article Name'
        merged = pd.merge(df_sales, df_promo, on="Article Name", how="inner")
        diagnostics_list.append(f"Rows remaining after matching 'Article Name': {len(merged)}")

        if len(merged) == 0:
            error_html = (
                f"<div class='alert alert-danger mt-4' role='alert'>"
                f"Mismatch Warning: 'Article Name' match returned 0 rows.<br>"
                f"<strong>Sales File Samples:</strong> {df_sales['Article Name'].head(3).tolist()}<br>"
                f"<strong>Promo File Samples:</strong> {df_promo['Article Name'].head(3).tolist()}"
                f"</div>"
            )
            diagnostics_html = "".join([f"<li><code>{log}</code></li>" for log in diagnostics_list])
            
            output = base_html.replace('{% if error %}{% endif %}', error_html)
            output = output.replace('{% if diagnostics %}{% endif %}', f"<div class='card p-4 bg-light border-start border-info border-3'><h5>🔍 Live Diagnostic Data Check:</h5><ul class='mb-0'>{diagnostics_html}</ul></div>")
            return HTMLResponse(content=output)

        # Resolve duplicate 'Brand' columns resulting from the merge step (Brand_x vs Brand_y)
        if "Brand_y" in merged.columns:
            merged = merged.rename(columns={"Brand_y": "Brand"})
        elif "Brand_x" in merged.columns:
            merged = merged.rename(columns={"Brand_x": "Brand"})

        # 7. Inclusive day-to-day validation (Direct Datetime Comparison)
        valid_claims = merged[
            (merged["Invoice Created On"] >= merged["From"]) & 
            (merged["Invoice Created On"] <= merged["To"])
        ]
        diagnostics_list.append(f"Rows remaining after Date Window Filtering: {len(valid_claims)}")

        if valid_claims.empty:
            error_html = "<div class='alert alert-danger mt-4' role='alert'>Date Window Mismatch: Article Names matched perfectly, but none of your Sales Invoice dates fall between their promotion 'From' and 'To' windows.</div>"
            diagnostics_html = "".join([f"<li><code>{log}</code></li>" for log in diagnostics_list])
            
            output = base_html.replace('{% if error %}{% endif %}', error_html)
            output = output.replace('{% if diagnostics %}{% endif %}', f"<div class='card p-4 bg-light border-start border-info border-3'><h5>🔍 Live Diagnostic Data Check:</h5><ul class='mb-0'>{diagnostics_html}</ul></div>")
            return HTMLResponse(content=output)

        # 8. Comprehensive grouping structure to ensure Comments, Brand, and Promo Codes stay intact
        group_cols = [
            "Supporting Ref", "Fin No:", "Deal Sheet No", "Deal Mail Date", 
            "CAT", "Brand", "Article Name", "From", "To", "GV",
            "Comments", "Remarks (Promo Code)"
        ]
        available_cols = [col for col in group_cols if col in valid_claims.columns]

        final_summary = valid_claims.groupby(available_cols, dropna=False).agg(
            Total_Qty_Sold=("Invoice Quantity", "sum")
        ).reset_index()

        # 9. Compute claims total amounts
        final_summary["Total Claim Amount"] = final_summary["Total_Qty_Sold"] * final_summary["GV"]

        # 10. Clean up Column Layout: Assign Email placeholder column safely
        final_summary["Email id"] = "partner@brandcontact.com"
            
        # Reorder Email id safely right after Brand if Brand exists
        cols = list(final_summary.columns)
        if "Brand" in cols:
            brand_idx = cols.index("Brand") + 1
            cols.insert(brand_idx, cols.pop(cols.index("Email id")))
            final_summary = final_summary[cols]

        # Cache data in global memory for downloads
        app.state.final_report = final_summary.copy()

        # Convert date objects cleanly for the Bootstrap table render engine
        for col in ["From", "To", "Deal Mail Date"]:
            if col in final_summary.columns:
                final_summary[col] = pd.to_datetime(final_summary[col], errors="coerce").dt.strftime('%Y-%m-%d').fillna("-")

        # 11. Convert layout matrix to HTML string elements
        html_table = final_summary.to_html(classes="table table-striped table-hover table-bordered text-center align-middle", index=False)
        
        email_msg = (
            "Dear Value Partner,\n\n"
            "Please find the attached claim for the promotions mentioned above. "
            "Kindly review and verify the documents within five working days.\n\n"
            "Dear @Ashwitha Shetty, once the claim has been verified (or after the five working days "
            "window has passed), Kindly share the corresponding debit note.\n\n"
            "Best regards,\nCategory Manager"
        )

        # Build replacement chunks natively
        diagnostics_html = f"<div class='card p-4 bg-light border-start border-info border-3'><h5>🔍 Live Diagnostic Data Check:</h5><ul class='mb-0'>{''.join([f'<li><code>{log}</code></li>' for log in diagnostics_list])}</ul></div>"
        
        result_html = (
            f"<div class='alert alert-success mt-4 d-flex justify-content-between align-items-center'>"
            f"<span>🎉 Success! Compiled a full report of <strong>{len(final_summary)}</strong> matching promotion claim lines.</span>"
            f"<a href='/download' class='btn btn-success'>📥 Download Full Report (.csv)</a>"
            f"</div>"
            f"<div class='card p-3'><h5 class='mb-3'>Processed Summary Output Matrix</h5><div class='table-responsive'>{html_table}</div></div>"
            f"<div class='card p-4 mt-4'><h5>📧 Generated Email Body Template</h5><p class='mb-1'><strong>Target Destination To:</strong> <code>partner@brandcontact.com</code></p><pre class='bg-dark text-white p-3 rounded'><code>{email_msg}</code></pre></div>"
        )

        # Inject dynamically into base template file string
        output = base_html.replace('{% if diagnostics %}{% endif %}', diagnostics_html)
        output = output.replace('{% if result %}{% endif %}', result_html)
        return HTMLResponse(content=output)

    except Exception as ex:
        error_html = f"<div class='alert alert-danger mt-4' role='alert'>An unexpected data processing error occurred: {str(ex)}</div>"
        output = base_html.replace('{% if error %}{% endif %}', error_html)
        return HTMLResponse(content=output)


@app.get("/download")
async def download_report():
    if app.state.final_report is not None:
        stream = io.StringIO()
        app.state.final_report.to_csv(stream, index=False)
        response = StreamingResponse(iter([stream.getvalue()]), media_type="text/csv")
        response.headers["Content-Disposition"] = "attachment; filename=Full_Brand_Promo_Claims_Report.csv"
        return response
    
    return HTMLResponse(content="<h3>Error: No calculation data found in workspace memory cache. Run an analysis line first.</h3>", status_code=400)
