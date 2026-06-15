import io
import pandas as pd
from fastapi import FastAPI, UploadFile, File, Request, Form
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

app = FastAPI(title="Brand Promo Claims Calculator")
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def read_index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "result": None, "diagnostics": None})

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

        # Setup diagnostic logs
        diagnostics = []
        diagnostics.append(f"Initial Sales Rows Uploaded: {len(df_sales)}")
        diagnostics.append(f"Initial Promo Rows Uploaded: {len(df_promo)}")

        # 2. Harmonize column name headers
        if "Code" in df_promo.columns and "Article Name" not in df_promo.columns:
            df_promo = df_promo.rename(columns={"Code": "Article Name"})

        # 3. Safe Date Parsing & Normalization
        df_sales["Invoice Created On"] = pd.to_datetime(df_sales["Invoice Created On"], errors="coerce").dt.normalize()
        df_promo["From"] = pd.to_datetime(df_promo["From"], errors="coerce").dt.normalize()
        df_promo["To"] = pd.to_datetime(df_promo["To"], errors="coerce").dt.normalize()

        # 4. Clean and normalize string matching keys
        df_sales["Article Name"] = df_sales["Article Name"].astype(str).str.strip().str.upper()
        df_promo["Article Name"] = df_promo["Article Name"].astype(str).str.strip().str.upper()

        # 5. Handle numbers safely
        df_sales["Invoice Quantity"] = pd.to_numeric(df_sales["Invoice Quantity"], errors="coerce").fillna(0)
        df_promo["GV"] = pd.to_numeric(df_promo["GV"], errors="coerce").fillna(0)

        # 6. Step 1 Join
        merged = pd.merge(df_sales, df_promo, on="Article Name", how="inner")
        diagnostics.append(f"Rows remaining after matching 'Article Name': {len(merged)}")

        if len(merged) == 0:
            error_msg = f"Mismatch Warning: 'Article Name' match returned 0 rows.<br>Sales Samples: {df_sales['Article Name'].head(3).tolist()}<br>Promo Samples: {df_promo['Article Name'].head(3).tolist()}"
            return templates.TemplateResponse("index.html", {"request": request, "error": error_msg, "diagnostics": diagnostics})

        # Resolve duplicate 'Brand' columns
        if "Brand_y" in merged.columns:
            merged = merged.rename(columns={"Brand_y": "Brand"})
        elif "Brand_x" in merged.columns:
            merged = merged.rename(columns={"Brand_x": "Brand"})

        # 7. Inclusive day-to-day validation
        valid_claims = merged[
            (merged["Invoice Created On"] >= merged["From"]) & 
            (merged["Invoice Created On"] <= merged["To"])
        ]
        diagnostics.append(f"Rows remaining after Date Window Filtering: {len(valid_claims)}")

        if valid_claims.empty:
            error_msg = "Date Window Mismatch: Article Names matched perfectly, but none of your Sales Invoice dates fall between their promotion windows."
            return templates.TemplateResponse("index.html", {"request": request, "error": error_msg, "diagnostics": diagnostics})

        # 8. Comprehensive grouping structure
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

        # 10. Clean up Column Layout
        if "Email id" not in final_summary.columns:
            final_summary["Email id"] = "partner@brandcontact.com"
            
        email_col = final_summary.pop("Email id")
        if "Brand" in final_summary.columns:
            brand_idx = final_summary.columns.get_loc("Brand") + 1
            final_summary.insert(brand_idx, "Email id", email_col)
        else:
            final_summary.insert(6, "Email id", email_col)

        # Convert date columns back to clean string format for HTML display
        for col in ["From", "To"]:
            if col in final_summary.columns:
                final_summary[col] = final_summary[col].dt.strftime('%Y-%m-%d')

        # Cache data in global application memory for streaming download link
        app.state.final_report = final_summary

        # Transform dataframe to HTML representation for our frontend table wrapper
        html_table = final_summary.to_html(classes="table table-striped table-hover table-bordered", index=False)
        
        email_template = (
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
            "email_body": email_template,
            "lines_count": len(final_summary)
        })

    except Exception as e:
        return templates.TemplateResponse("index.html", {"request": request, "error": f"An infrastructure error occurred: {str(e)}"})

@app.get("/download")
async def download_report():
    if hasattr(app.state, 'final_report') and app.state.final_report is not None:
        stream = io.StringIO()
        app.state.final_report.to_csv(stream, index=False)
        response = StreamingResponse(iter([stream.getvalue()]), media_type="text/csv")
        response.headers["Content-Disposition"] = "attachment; filename=Full_Brand_Promo_Claims_Report.csv"
        return response
    return {"error": "No processing data is currently found in cache. Run a calculation line first."}