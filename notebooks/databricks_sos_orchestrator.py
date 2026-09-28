# Databricks notebook source
# MAGIC %md
# MAGIC # F5 Sales Operations (SOS) Purchase Order Validation & Booking Pipeline
# MAGIC **Enterprise Data Ecosystem (EDE) & Digital Engineering Production Orchestrator**
# MAGIC 
# MAGIC This notebook is a **thin orchestrator** that imports the tested, battle-hardened `po_validation` engine.
# MAGIC It does NOT rewrite parsing or validation logic in notebook cells.
# MAGIC 
# MAGIC ### Core Guarantees:
# MAGIC 1. **Full Geometric Parser:** Preserves table stream detection, coordinate tracking, and multi-layout extractors.
# MAGIC 2. **Full 11-Item SOS Checklist:** Evaluates all rules from `rules/sos.yaml` (price, quantity, Inco terms, payment terms, F5 entities, freight).
# MAGIC 3. **Deduplication Ledger (`JsonLedger`):** Prevents reprocessing or double-booking files on scheduled cron runs.
# MAGIC 4. **Enforced Human Approval:** Orders with `VALIDATED` status route to `sos_approval_queue`; **no auto-booking without specialist sign-off**.
# MAGIC 5. **Native Snowflake Key-Pair Auth:** Integrates with Databricks `spark.read.format("snowflake")` and `SalesKeyVaultScope`.
# MAGIC 6. **Correct Salesforce Schema:** Uses real custom fields (`PO__c`, `F_5_Quote__c`, `Searchable_PO_Field__c`, `ContentNotes`).

# COMMAND ----------
# MAGIC %md
# MAGIC ### 1. Dependencies & Environment Setup

# COMMAND ----------
# Install required libraries
%pip install --quiet pypdf pdfplumber pyyaml snowflake-connector-python requests

# COMMAND ----------
import os
import sys
import logging
from pathlib import Path

# Add repo workspace directory to sys.path so po_validation is importable directly
workspace_dir = os.path.dirname(os.path.abspath("__file__"))
if workspace_dir not in sys.path:
    sys.path.insert(0, workspace_dir)

# Import the tested po_validation engine
from po_validation.models import Outcome, Status
from po_validation.pipeline import Pipeline, ExceptionRouter
from po_validation.validate.engine import load_engine
from po_validation.ingest.sources import LocalFolderSource
from po_validation.ingest.ledger import JsonLedger
from po_validation.resolve.snowflake import SnowflakeQuoteSource
from po_validation.act.salesforce import SalesforceClient
from po_validation.booking.builder import BookingFormBuilder
from po_validation.booking.card import render_html_email_preview
from po_validation.ai.client import F5AIClient

logger = logging.getLogger("sos_orchestrator")
logger.setLevel(logging.INFO)

# COMMAND ----------
# MAGIC %md
# MAGIC ### 2. Configuration, Widgets & Key Vault Secrets

# COMMAND ----------
# Databricks Widgets for interactive execution / Job parameterization
dbutils.widgets.text("input_dir", "/Volumes/prd_sales/sos/incoming_pos/", "1. Input PO Directory (Volume/Cloud Path)")
dbutils.widgets.text("ledger_path", "/Volumes/prd_sales/sos/ledger.jsonl", "2. Audit Ledger Path (Deduplication)")
dbutils.widgets.dropdown("dry_run", "true", ["true", "false"], "3. Salesforce Dry-Run Mode")
dbutils.widgets.text("sf_instance_url", "https://f5--poclab.sandbox.my.salesforce.com", "4. Salesforce URL")

INPUT_DIR = Path(dbutils.widgets.get("input_dir"))
LEDGER_PATH = Path(dbutils.widgets.get("ledger_path"))
DRY_RUN = dbutils.widgets.get("dry_run").lower() in ("true", "1", "yes")
SF_URL = dbutils.widgets.get("sf_instance_url")

# Securely retrieve Snowflake credentials from Databricks Secret Scope (SalesKeyVaultScope)
try:
    snowflake_options = {
        "sfUrl": dbutils.secrets.get(scope="SalesKeyVaultScope", key="snowflake-url"),
        "sfUser": dbutils.secrets.get(scope="SalesKeyVaultScope", key="snowflake-user"),
        "sfWarehouse": dbutils.secrets.get(scope="SalesKeyVaultScope", key="snowflake-warehouse"),
        "sfRole": dbutils.secrets.get(scope="SalesKeyVaultScope", key="snowflake-role"),
        "sfDatabase": dbutils.secrets.get(scope="SalesKeyVaultScope", key="snowflake-database"),
        "sfSchema": dbutils.secrets.get(scope="SalesKeyVaultScope", key="snowflake-schema"),
        "pem_private_key": dbutils.secrets.get(scope="SalesKeyVaultScope", key="snowflake-private-key"),
    }
except Exception as e:
    logger.warning(f"Could not load SalesKeyVaultScope Snowflake secrets: {e}. Falling back to env.")
    snowflake_options = {}

# Securely retrieve Salesforce Token
sf_token = os.environ.get("SF_ACCESS_TOKEN", "")
try:
    sf_token = dbutils.secrets.get(scope="SalesKeyVaultScope", key="salesforce-token") or sf_token
except Exception:
    pass

# COMMAND ----------
# MAGIC %md
# MAGIC ### 3. Pipeline Initialization

# COMMAND ----------
# 1. Deduplication Ledger: Prevents reprocessing or double-booking files on scheduled cron runs
ledger = JsonLedger(LEDGER_PATH)

# 2. Declarative SOS Checklist: All 11 rules loaded from rules/sos.yaml
rules_dir = Path(workspace_dir) / "rules"
engine = load_engine("sos", ledger=ledger, rules_dir=rules_dir)

# 3. Authoritative Quote Source: Queries Snowflake with line.is_deleted = false
quote_source = SnowflakeQuoteSource(spark=spark, options=snowflake_options)

# 4. Exception Router: Enforces human approval (VALIDATED routes to approval queue, create_booking_form: false)
router = ExceptionRouter(sinks=[], routing=engine.checklist.spec["routing"])

# 5. Salesforce Client: Uses exact field names (PO__c, F_5_Quote__c), verified TLS, schema describe, and safe dry-run
sf_client = SalesforceClient.from_token(
    instance_url=SF_URL,
    token=sf_token,
    dry_run=DRY_RUN
)

# 6. Pipeline: Orchestrates reading, parsing (geometric), resolving, validating, routing, and ledger recording
pipeline = Pipeline(
    source=LocalFolderSource(INPUT_DIR),
    engine=engine,
    quote_source=quote_source,
    router=router,
    ledger=ledger,
    salesforce=sf_client
)

logger.info(f"Pipeline initialized. Monitoring '{INPUT_DIR}' (Dry-Run = {DRY_RUN}).")

# COMMAND ----------
# MAGIC %md
# MAGIC ### 4. Batch Execution

# COMMAND ----------
# Execute batch processing across new documents
results = pipeline.run()
logger.info(f"Processed {len(results)} document(s).")

# COMMAND ----------
# MAGIC %md
# MAGIC ### 5. Interactive Approval Cards (`displayHTML`) & Summary

# COMMAND ----------
import pandas as pd

# High-precision deterministic summary client (0 cloud AI dependencies)
summary_client = F5AIClient(api_key="")

summary_rows = []

for res in results:
    form = BookingFormBuilder.build(res)
    
    # Generate factual executive summary
    summary_text = summary_client.generate_sos_summary(
        po_number=form.po_number or "UNKNOWN",
        account=form.account_name or "Customer",
        amount=f"{form.currency} ${form.amount:,.2f}",
        blockers=[f.message for f in res.findings if f.status == Status.FAIL and f.severity == "BLOCKER"],
        majors=[f.message for f in res.findings if f.status != Status.PASS and f.severity == "MAJOR"],
        notes=[n.title for n in form.notes]
    )
    
    # Render responsive approval card in Databricks cell output
    card_html = render_html_email_preview(form, summary_text, instance_url=SF_URL)
    displayHTML(card_html)
    
    summary_rows.append({
        "PO Number": form.po_number,
        "Quote Number": form.f5_quote_number,
        "Account": form.account_name,
        "Booked Amount": f"{form.currency} ${form.amount:,.2f}",
        "Outcome": res.outcome.value if hasattr(res.outcome, "value") else str(res.outcome),
        "Order Issues": form.order_issues,
        "RO Notes Count": len(form.notes),
        "Action Taken": res.action.get("reason", "Pending Specialist Review"),
    })

if summary_rows:
    display(pd.DataFrame(summary_rows))
else:
    print("No new purchase orders to process (all documents are clean in ledger).")
