# F5 SOS PO Validation — Databricks Deployment Guide

This guide describes how the **Digital Engineering / Enterprise Data Ecosystem (EDE)** team can deploy, configure, and schedule the F5 Sales Operations Purchase Order Validation Engine in Databricks.

---

## 1. File Overview

* **Notebook Source File:** [`databricks_sos_po_validation.py`](file:///Users/d.dhok/Desktop/po-validation/databricks_sos_po_validation.py)
* **Format:** Standard Databricks Source format (`# Databricks notebook source` with `# COMMAND ----------` cell delimiters).
* **Architecture:** 100% self-contained single notebook containing:
  - Multi-layout PDF extractor (`pdfplumber` / `pypdf`)
  - Declarative SOS Checklist Rules Engine
  - Snowflake client with dynamic schema introspection (`PRD_ENT_RAW.SALESFORCE`)
  - Dynamic Booking Form & Revenue Operations (RO) Note Generator
  - High-precision deterministic summary engine (zero external cloud AI dependencies)
  - Salesforce REST API Client (Verified TLS & Schema Introspection)
  - Interactive Databricks UI (`displayHTML` responsive approval cards)

---

## 2. Importing the Notebook into Databricks

1. Log into the F5 Databricks Workspace (AWS / Azure).
2. In the left navigation bar, navigate to **Workspace** $\rightarrow$ **Shared** (or your team's folder, e.g., `/Shared/SOS_Automation/`).
3. Click the kebab menu (`...`) or right-click and select **Import**.
4. In the Import modal:
   - Select **File**.
   - Browse to or drop `databricks_sos_po_validation.py`.
   - Click **Import**.
5. Databricks will automatically parse the `# COMMAND ----------` delimiters into an interactive multi-cell notebook.

---

## 3. Cluster Requirements & Dependencies

The notebook includes an automated `%pip install` cell at the top:
```python
%pip install --quiet pypdf pdfplumber pyyaml snowflake-connector-python requests
```

### Cluster Specifications
* **Runtime:** Databricks Runtime (DBR) 13.3 LTS or higher (Standard Python 3.10+).
* **Worker Type:** Standard general-purpose compute (e.g. `m5.large` or `Standard_D4s_v5`). Single-node clusters are sufficient for PDF batch processing.

---

## 4. Configuring Credentials (Databricks Secrets)

Digital should store service credentials in the Databricks Secret Scope `sos_pipeline`.

### Creating the Secret Scope (via Databricks CLI):
```bash
databricks secrets create-scope sos_pipeline
```

### Setting the Keys:
```bash
# Snowflake Credentials
databricks secrets put-secret sos_pipeline snowflake_user --string-value "APP_SOS_SERVICE_USER"
databricks secrets put-secret sos_pipeline snowflake_password --string-value "<SERVICE_PASSWORD>"
databricks secrets put-secret sos_pipeline snowflake_account --string-value "f5-enterprisedataecosystem"
databricks secrets put-secret sos_pipeline snowflake_warehouse --string-value "EXP_SALES_WH"
databricks secrets put-secret sos_pipeline snowflake_role --string-value "APP_EDE_SALES_EXP_ROLE"

# Salesforce Credentials
databricks secrets put-secret sos_pipeline salesforce_instance_url --string-value "https://f5--poclab.sandbox.my.salesforce.com"
databricks secrets put-secret sos_pipeline salesforce_access_token --string-value "<SF_OAUTH_TOKEN>"
```

*Note: If secrets are not configured, the notebook automatically falls back to Databricks interactive UI widgets for rapid testing.*

---

## 5. Connecting Input PO Files (Databricks Volumes / Cloud Storage)

Customer PO PDFs should land in a Unity Catalog Volume or DBFS cloud mount:
* **Unity Catalog Volume:** `/Volumes/prd_sales/sos/incoming_pos/`
* **S3 / ADLS Mount:** `/dbfs/mnt/sos_pos/incoming/`

Set the `input_dir` widget or parameter to this path.

---

## 6. Scheduling as a Databricks Workflow Job

To run the pipeline automatically:

1. In Databricks, navigate to **Workflows** $\rightarrow$ **Jobs** $\rightarrow$ **Create Job**.
2. **Task Name:** `Run_SOS_PO_Validation`.
3. **Type:** `Notebook`.
4. **Path:** Select `/Shared/SOS_Automation/databricks_sos_po_validation`.
5. **Cluster:** Select or define a small automated job cluster.
6. **Parameters:**
   - `input_dir`: `/Volumes/prd_sales/sos/incoming_pos/`
   - `dry_run`: `false` (or `true` during testing)
7. **Schedule / Trigger:**
   - **Option A (Cron Schedule):** E.g. every 10 or 15 minutes (`0 */10 * * * ?`).
   - **Option B (File Arrival Trigger):** Trigger immediately when a new `.pdf` file lands in the Databricks Volume.
8. **Notifications:** Configure email or Slack alerts (`#sos-operations-alerts`) on job failure.

---

## 7. Safe Testing & Dry-Run Mode

* **Dry-Run Mode (`dry_run=true`):**
  When enabled, the pipeline executes 100% of the extraction, Snowflake queries, rules validation, and payload construction, but logs and returns a mock Salesforce record ID without mutating any records in Salesforce.
* **Live Mode (`dry_run=false`):**
  Creates the `Booking_Form__c` object via the Salesforce REST API and attaches all auto-drafted `ContentNotes` and `ContentDocumentLink` records.
