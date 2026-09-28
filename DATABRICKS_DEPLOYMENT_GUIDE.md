# F5 SOS PO Validation — Databricks Production Architecture Guide

This guide documents the **Thin Orchestrator Architecture** for running the F5 Sales Operations Purchase Order Validation Engine in Databricks.

---

## 1. Architectural Philosophy: Thin Orchestrator vs. Rewriting

> [!IMPORTANT]
> **Do not rewrite parsing and validation logic inside Databricks notebook cells.**
> The `po_validation` engine contains over 9,500 lines of tested geometric table parsers, composite SKU matching, and 11-rule checklist logic with 38 passing regression tests.
> 
> In Databricks, we follow standard enterprise best practice:
> 1. **The Engine (`po_validation`):** Remains the tested, version-controlled Python package.
> 2. **The Notebook ([`notebooks/databricks_sos_orchestrator.py`](file:///Users/d.dhok/Desktop/po-validation/notebooks/databricks_sos_orchestrator.py)):** Remains a thin orchestrator (under 150 lines) that handles Databricks widgets, Key Vault secrets, scheduling, and `displayHTML()` dashboard rendering.

---

## 2. Core Operational Guarantees

| Concern | How the Engine Handles It |
| :--- | :--- |
| **PDF Extraction Accuracy** | Uses `po_validation.extract` (geometric coordinate tracking and table streams) with dedicated extractors for Dell, Synnex, Carahsoft, Ingram, and WWT. |
| **Checklist Scope** | Executes all 11 declarative SOS rules from `rules/sos.yaml` (including freight isolation, Inco terms destination verification, payment terms, and F5 entity checks). |
| **No Double-Booking** | Uses `JsonLedger` on a Databricks Volume path. When run on a 15-minute cron schedule, already-processed documents are skipped. |
| **Human Approval Required** | `rules/sos.yaml` routes `VALIDATED` orders to `sos_approval_queue` with `create_booking_form: false`. Orders are never auto-booked without human review. |
| **Salesforce Schema Integrity** | Uses exact custom fields (`PO__c`, `F_5_Quote__c`, `Searchable_PO_Field__c`, `Stage__c`, `ContentNotes`) with verified TLS and schema introspection. |
| **Cloud Snowflake Auth** | Uses `SnowflakeQuoteSource` with Databricks `SalesKeyVaultScope` and PEM private key-pair auth, filtering out `line.is_deleted = false`. |

---

## 3. How to Deploy to Databricks

### Method A: Databricks Repos (Recommended for Digital)
1. In Databricks, navigate to **Workspace** $\rightarrow$ **Repos**.
2. Add Repo and point to your internal Git repository (or upload the folder).
3. Open [`notebooks/databricks_sos_orchestrator.py`](file:///Users/d.dhok/Desktop/po-validation/notebooks/databricks_sos_orchestrator.py).
4. Run or schedule as a Databricks Workflow Job.

### Method B: Package Distribution
1. Build the source package:
   ```bash
   python setup.py sdist
   ```
2. Upload `dist/po_validation-0.1.0.tar.gz` to your Databricks Volume or DBFS.
3. In the notebook, install via:
   ```python
   %pip install /Volumes/prd_sales/sos/po_validation-0.1.0.tar.gz
   ```

---

## 4. Secret Configuration (`SalesKeyVaultScope`)

Digital should store service credentials in the Databricks Secret Scope `SalesKeyVaultScope`:

```bash
# Snowflake Credentials
databricks secrets put-secret SalesKeyVaultScope snowflake-url --string-value "f5-enterprisedataecosystem.snowflakecomputing.com"
databricks secrets put-secret SalesKeyVaultScope snowflake-user --string-value "APP_SOS_SERVICE_USER"
databricks secrets put-secret SalesKeyVaultScope snowflake-warehouse --string-value "EXP_SALES_WH"
databricks secrets put-secret SalesKeyVaultScope snowflake-role --string-value "APP_EDE_SALES_EXP_ROLE"
databricks secrets put-secret SalesKeyVaultScope snowflake-database --string-value "PRD_ENT_RAW"
databricks secrets put-secret SalesKeyVaultScope snowflake-schema --string-value "SALESFORCE"
databricks secrets put-secret SalesKeyVaultScope snowflake-private-key --string-value "<PEM_PRIVATE_KEY>"

# Salesforce Credentials
databricks secrets put-secret SalesKeyVaultScope salesforce-token --string-value "<SF_JWT_OR_OAUTH_TOKEN>"
```

---

## 5. Sharing the Codebase

A clean repository archive is ready in the workspace root:
* **Archive Path:** [`po_validation_repo.zip`](file:///Users/d.dhok/Desktop/po-validation/po_validation_repo.zip) (116 KB)
* Contains all 51 codebase files, the complete geometric parser, hardened booking logic, 11-rule YAML checklist, and full test suite (with zero proprietary PO data or `.venv` binaries).
