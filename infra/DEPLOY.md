# Deployment Steps — AGT PlayGround Subscription

Run these in order. Each step depends on the previous one completing.

## 0. Point the CLI at the right subscription

```powershell
az login
az account set --subscription "AGT PlayGround"
az account show
```

Confirm the `name` in the output says `AGT PlayGround` before continuing — deploying
into the wrong subscription is an easy mistake to make with two subscriptions
in play.

## 1. Register required resource providers

This is a fresh subscription, so these almost certainly aren't registered yet.
This step is idempotent — safe to run even if some are already registered.

```powershell
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.Sql
az provider register --namespace Microsoft.KeyVault
az provider register --namespace Microsoft.ContainerRegistry
az provider register --namespace Microsoft.OperationalInsights
az provider register --namespace Microsoft.Authorization
```

Check registration status (should all say `Registered` — can take a couple
minutes):

```powershell
az provider show --namespace Microsoft.App --query registrationState -o tsv
```

## 2. Resource group

Using the existing `AlgoTrading` resource group (already created and
Contributor-scoped to you) rather than creating a new one. It's in West
Europe — deliberately deploying there for now rather than fighting the
region question mid-migration. `main.bicep` inherits the RG's own location,
so everything lands in West Europe too. **This is a known, accepted tradeoff,
not an oversight** — revisit as its own task after migration is done (see
note at the bottom of this file).

Confirm you can see it:

```powershell
az group show --name AlgoTrading
```

## 3. Deploy the infrastructure

**Requires the six resource providers to be registered first (Step 1) —
this deploy will fail on the Container Apps / SQL / Key Vault resources
until that's done.**

You'll be prompted for `sqlAdminPassword` — type it directly, it won't be
saved to any file. Pick something meeting Azure SQL's complexity rules
(8+ chars, at least 3 of: upper, lower, digit, symbol).

```powershell
az deployment group create `
  --resource-group AlgoTrading `
  --template-file main.bicep `
  --parameters main.parameters.json `
  --parameters sqlAdminPassword=<type-it-here>
```

This takes several minutes (SQL Server + Container Apps Environment are the
slow parts). When it finishes, note the outputs — especially `acrLoginServer`
and `dashboardUrl`.

**This first deploy uses a placeholder public image** (`mcr.microsoft.com/k8se/quickstart:latest`)
for all three compute resources, because the ACR doesn't have your real image
yet, and the ACR itself doesn't exist until this step completes — chicken and
egg. That's expected. Step 4 fixes it.

## 4. Build and push your real image to the new ACR

From your project root (where the `Dockerfile` is):

```powershell
az acr build --registry <acrLoginServer-from-outputs> --image algo-trading:latest .
```

This builds *in Azure*, not locally — no local Docker install needed.

## 5. Point all three compute resources at the real image

```powershell
az containerapp update --name algo-dashboard --resource-group AlgoTrading --image <acrLoginServer>/algo-trading:latest

az containerapp update --name algo-ws-listener --resource-group AlgoTrading --image <acrLoginServer>/algo-trading:latest

az containerapp job update --name algo-scanner --resource-group AlgoTrading --image <acrLoginServer>/algo-trading:latest
```

## 6. Verify

```powershell
az containerapp show --name algo-dashboard --resource-group AlgoTrading --query properties.runningStatus
az containerapp logs show --name algo-dashboard --resource-group AlgoTrading --follow
```

Open the `dashboardUrl` from the Step 3 outputs in a browser — should load
the Streamlit dashboard (it won't have real data yet until Phase 3, data
migration, is done).

## Updating the Entra ID (MSAL) client secret

The dashboard's Microsoft sign-in reads `ENTRA_CLIENT_ID`/`ENTRA_TENANT_ID`/
`ENTRA_REDIRECT_URI` (plain, safe to leave at their `main.bicep` defaults) and
`ENTRA_CLIENT_SECRET` (secret, empty default so a normal redeploy is a safe
no-op). To push a new/rotated client secret to the live dashboard app, redeploy
with it passed explicitly — same pattern as `sqlAdminPassword`/`telegramBotToken`,
never add it to `main.parameters.json`:

```powershell
az deployment group create `
  --resource-group AlgoTrading `
  --template-file main.bicep `
  --parameters main.parameters.json `
  --parameters sqlAdminPassword=<existing-password> entraClientSecret=<fresh-secret-value>
```

## What's intentionally NOT done yet (later phases)

- **App code still points at Postgres.** The container will fail to connect
  to a database until the app's DB driver/connection string is switched to
  Azure SQL (Phase 4).
- **No data has been loaded into Azure SQL yet.** `sql-algo-trading` exists
  and is empty — schema translation + data load from `algo_trading_backup.dump`
  is Phase 3, separate from this infra deploy.
- **The scan-overlap bug isn't fixed by this template.** `parallelism: 1`
  limits replicas within one job execution, but Azure will still fire a new
  cron execution even if the previous one hasn't finished. The real fix is
  an app-level run-lock inside `run_single_scan.py` — that's a code change,
  not something Bicep can solve.
- **Upstox tokens will need re-authentication** once this is live — don't
  assume the old `upstox_tokens` rows will just work against a new deployment.
- **Region is West Europe, deliberately deferred.** Revisit after migration:
  moving to Central India later means redeploying SQL Server and the
  Container Apps Environment (both region-bound at creation), not a
  same-place config change. Budget real time for this, not a quick flag flip.