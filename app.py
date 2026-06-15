import io
import os
import pandas as pd
from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.templating import Jinja2Templates

app = FastAPI(title="Brand Promo Claims Calculator")

# Core Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(BASE_DIR, "templates", "index.html")

# Initialize Jinja2 only for POST processing wrappers
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Global internal app cache to temporarily hold calculated datasets for export streams
app.state.final_report = None


@app.get("/", response_class=HTMLResponse)
async def read_index():
    """
    Directly streams the index.html file to bypass Jinja2's 
    Python 3.14 composite tuple dictionary cache bug on the home route.
    """
    return FileResponse(TEMPLATE_PATH)


@app.post("/calculate")
async def calculate_claims(
    request: Request,
    claim_sales: UploadFile = File(...),
    claim_promo: UploadFile = File(...)
):
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
        diagnostics = []
        diagnostics.append(f"Initial Sales Rows Uploaded: {len(df_sales)}")
        diagnostics.append(f"Initial Promo Rows Uploaded: {len(df_promo)}")

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
        diagnostics.append(f"Rows remaining after matching 'Article Name': {len(merged)}")

        if len(merged) == 0:
            error_msg = (
                f"Mismatch Warning: 'Article Name' match returned 0 rows.<br>"
                f"<strong>Sales File Samples:</strong> {df_sales['Article Name'].head(3).tolist()}<br>"
                f"<strong>Promo File Samples:</strong> {df_promo['Article Name'].head(3).tolist()}"
            )
            return templates.TemplateResponse("index.html", {
                "request": request, 
                "error": error_msg, 
                "diagnostics": diagnostics,
                "result": None
            })

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
        diagnostics.append(f"Rows remaining after Date Window Filtering: {len(valid_claims)}")

        if valid_claims.empty:
            error_msg = "Date Window Mismatch: Article Names matched perfectly, but none of your Sales Invoice dates fall between their promotion 'From' and 'To' windows."
            return templates.TemplateResponse("index.html", {
                "request": request, 
                "error": error_msg, 
                "diagnostics": diagnostics,
                "result": None
            })

        # 8. Comprehensive grouping structure to ensure Comments, Brand, and Promo Codes stay intact
        group_cols = [
            "Supporting Ref", "Fin No:", "Deal Sheet No", "Deal Mail Date", 
            "CAT", "Brand", "Article Name", "From", "To", "GV",
            "Comments", "Remarks (Promo Code)"
        ]
        # Verify column safety against what is present inside the filtered data
        available_cols = [col for col in group_cols if col in valid_claims.columns]

        # dropna=False forces pandas to keep rows even if comments or promo codes are blank!
        final_summary = valid_claims.groupby(available_cols, dropna=False).agg(
            Total_Qty_Sold=("Invoice Quantity", "sum")
        ).reset_index()

        # 9. Compute claims total amounts
        final_summary["Total Claim Amount"] = final_summary["Total_Qty_Sold"] * final_summary["GV"]

        # 10. Clean up Column Layout: Insert a default placeholder Email ID column right after Brand if needed
        if "Email id" not in final_summary.columns:
            final_summary["Email id"] = "partner@brandcontact.com"
            
        email_col = final_summary.pop("Email id")
        if "Brand" in final_summary.columns:
            brand_idx = final_summary.columns.get_loc("Brand") + 1
            final_summary.insert(brand_idx, "Email id", email_col)
        else:
            final_summary.insert(6, "Email id", email_col)

        # Cache data in global application state memory for streaming download actions
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

        return templates.TemplateResponse("index.html", {
            "request": request,
            "result": html_table,
            "diagnostics": diagnostics,
            "email_body": email_msg,
            "lines_count": len(final_summary),
            "error": None
        })

    except Exception as ex:
        return templates.TemplateResponse("index.html", {
            "request": request, 
            "error": f"An unexpected data processing error occurred: {str(ex)}",
            "result": None,
            "diagnostics": None
        })


@app.get("/download")
async def download_report():
    """
    Constructs an isolated, download-ready data stream from 
    the active application state matrix cache.
    """
    if app.state.final_report is not None:
        stream = io.StringIO()
        app.state.final_report.to_csv(stream, index=False)
        response = StreamingResponse(iter([stream.getvalue()]), media_type="text/csv")
        response.headers["Content-Disposition"] = "attachment; filename=Full_Brand_Promo_Claims_Report.csv"
        return response
    
    return HTMLResponse(content="<h3>Error: No calculation data found in workspace memory cache. Run an analysis line first.</h3>", status_code=400)
