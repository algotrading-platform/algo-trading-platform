// ============================================================================
// Algo Trading Platform — Infrastructure (AGT PlayGround subscription)
// ============================================================================
// Deploys: ACR, Log Analytics, Container Apps Environment, one always-on
// Container App (dashboard), four Container App Jobs -- one per timeframe
// group, algo-scanner-5min/15min/1hour/eod (enterprise Phase 2, 2026-09-21;
// was a single algo-scanner Job driving all 6 timeframes sequentially
// behind one global run-lock, see run_single_scan.py's GROUPS/--group for
// why that was split), one always-on Container App (websocket listener),
// Azure SQL Server + Database, and Key Vault.
//
// Deploy with (resource group scope):
//   az deployment group create -g <rg-name> -f main.bicep -p main.parameters.json --parameters sqlAdminPassword=<typed-at-prompt>
//
// NOTE: telegramBotToken/telegramChatId/upstoxSandboxAccessToken/entraClientSecret
// have NO default value - Container Apps' `secrets` block is fully replaced on
// every deploy (there is no way to read back an existing secret's value in
// Bicep to preserve it), so a default of '' is NOT a safe no-op: it silently
// wipes the live secret on any deploy that forgets to pass it, which once
// broke Entra sign-in tenant-wide right after an unrelated redeploy. Making
// these required forces every deploy to pass the current value explicitly
// (az deployment group create prompts for missing @secure() params with no
// default) - see DEPLOY.md for the exact command.
// ============================================================================

@description('Azure region for compute/support resources (ACR, Log Analytics, Container Apps). Inherits the AlgoTrading resource group location (West Europe) - already successfully deployed there.')
param location string = resourceGroup().location

@description('Azure region for Azure SQL specifically. West Europe is blocked for SQL on this subscription (ProvisioningDisabled - likely an Azure Sponsorship region restriction), so this is set separately to a region confirmed to work (matches the old Postgres server and other SQL resources already in this tenant).')
param sqlLocation string = 'centralindia'

@description('Short prefix used to name all resources')
param namePrefix string = 'algo'

@description('SQL Server admin login name')
param sqlAdminLogin string = 'algoadmin'

@description('SQL Server admin password - pass at deploy time, do not commit a real value')
@secure()
param sqlAdminPassword string

@description('Container image for all three compute resources. Points at the real built-and-pushed image.')
param containerImage string = 'algoacrrjw4desia2hqk.azurecr.io/algo-trading:latest'

@description('Application Insights connection string (enterprise Phase 1, 2026-09-20) — shared by all three compute resources for scan-duration/signal-latency/sandbox-result telemetry. Empty string is a safe no-op: core/telemetry.py disables itself cleanly when this env var is unset.')
@secure()
param appInsightsConnectionString string = ''

@description('Azure SQL SKU tier')
param sqlSkuName string = 'Basic'

@description('Azure SQL SKU tier name')
param sqlSkuTier string = 'Basic'

@description('Object ID of the account running this deployment (cgummunur@ariqt.com) - needed to grant secret-write access on the Key Vault for this deployment to succeed.')
param deployerObjectId string = 'cf35cfc5-89f9-43fb-9cf0-7fa0fcd081fe'

@description('Telegram bot token for scan-cycle alerts. Added directly to the live scanJob resource outside this file at some point after initial deploy - added here now to keep main.bicep an accurate description of what is actually deployed. No default: pass the current value on every deploy, never commit it - see the note at the top of this file for why an empty default is unsafe here.')
@secure()
param telegramBotToken string

@description('Telegram chat ID for scan-cycle alerts. Same provenance/rationale as telegramBotToken above.')
@secure()
param telegramChatId string

@description('Upstox sandbox access token, used for validating live signals against Upstox before using the production Upstox flow. Same provenance/rationale as telegramBotToken above.')
@secure()
param upstoxSandboxAccessToken string

@description('Entra ID (Azure AD) app registration client ID for dashboard sign-in - the "Algo-Trading" single-tenant app registration in the ariqt.com tenant.')
param entraClientId string = '6064f100-172f-4fc1-9798-b4e493e44717'

@description('Entra ID tenant ID (ariqt.com).')
param entraTenantId string = '8f6bd982-92c3-4de0-985d-0e287c55e379'

@description('OAuth redirect URI for the dashboard auth-code flow - must exactly match a registered Web redirect URI on the app registration (no trailing slash).')
param entraRedirectUri string = 'https://algo-dashboard.lemonglacier-23c89c18.westeurope.azurecontainerapps.io'

@description('Client secret for the Entra app registration. Same provenance/rationale as telegramBotToken above: no default - pass the current value on every deploy, never commit it.')
@secure()
param entraClientSecret string

@description('Comma-separated allow-list of UPNs permitted to use the dashboard after a successful Entra sign-in - app-level defense in depth on top of the app registration\'s "assignment required" toggle, which is otherwise the ONLY thing enforcing this. Not secret, safe to default here.')
param entraAllowedUpns string = 'cgummunur@ariqt.com,rkumar@ariqt.com,algotrading@ariqt.com'

var uniqueSuffix = uniqueString(resourceGroup().id)
var acrName = '${namePrefix}acr${uniqueSuffix}'
var lawName = '${namePrefix}-law-${uniqueSuffix}'
var caeName = '${namePrefix}-cae'
var dashboardAppName = '${namePrefix}-dashboard'
var wsListenerAppName = '${namePrefix}-ws-listener'
var scanJobName = '${namePrefix}-scanner'
var sqlServerName = '${namePrefix}-sql2-${uniqueSuffix}'
var sqlDbName = '${namePrefix}db'
var kvName = take('${namePrefix}kv${uniqueSuffix}', 24)

// ----------------------------------------------------------------------------
// Container Registry
// ----------------------------------------------------------------------------
resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: true
  }
}

// ----------------------------------------------------------------------------
// Log Analytics + Container Apps Environment
// ----------------------------------------------------------------------------
resource law 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: lawName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource cae 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: caeName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: law.properties.customerId
        sharedKey: law.listKeys().primarySharedKey
      }
    }
  }
}

// ----------------------------------------------------------------------------
// Key Vault
// ----------------------------------------------------------------------------
resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: kvName
  location: location
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: false
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    accessPolicies: [
      {
        tenantId: subscription().tenantId
        objectId: deployerObjectId
        permissions: {
          secrets: [
            'get'
            'list'
            'set'
          ]
        }
      }
    ]
  }
}

resource sqlPasswordSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'sql-admin-password'
  properties: {
    value: sqlAdminPassword
  }
}

// ----------------------------------------------------------------------------
// Azure SQL
// ----------------------------------------------------------------------------
resource sqlServer 'Microsoft.Sql/servers@2022-05-01-preview' = {
  name: sqlServerName
  location: sqlLocation
  properties: {
    administratorLogin: sqlAdminLogin
    administratorLoginPassword: sqlAdminPassword
    version: '12.0'
    minimalTlsVersion: '1.2'
  }
}

resource sqlDb 'Microsoft.Sql/servers/databases@2022-05-01-preview' = {
  parent: sqlServer
  name: sqlDbName
  location: sqlLocation
  sku: {
    name: sqlSkuName
    tier: sqlSkuTier
  }
}

// Allows Azure services (Container Apps) to reach the SQL server.
// Tighten to specific outbound IPs / private endpoint later if needed.
resource sqlFirewallAzureServices 'Microsoft.Sql/servers/firewallRules@2022-05-01-preview' = {
  parent: sqlServer
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

// ----------------------------------------------------------------------------
// Container App: Dashboard (always-on, serves the Streamlit UI)
// ----------------------------------------------------------------------------
resource dashboardApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: dashboardAppName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    managedEnvironmentId: cae.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8501
        transport: 'auto'
      }
      registries: [
        {
          server: acr.properties.loginServer
          username: acr.listCredentials().username
          passwordSecretRef: 'acr-password'
        }
      ]
      secrets: concat([
        {
          name: 'acr-password'
          value: acr.listCredentials().passwords[0].value
        }
        {
          name: 'db-password'
          value: sqlAdminPassword
        }
        {
          name: 'entra-client-secret'
          value: entraClientSecret
        }
      ], appInsightsConnectionString != '' ? [{
        name: 'appinsights-connection-string'
        value: appInsightsConnectionString
      }] : [])
    }
    template: {
      containers: [
        {
          name: 'dashboard'
          image: containerImage
          command: [
            'streamlit'
            'run'
            'app/dashboard/dashboard.py'
            '--server.port=8501'
            '--server.address=0.0.0.0'
          ]
          env: concat([
            {
              name: 'AZURE_DB_HOST'
              value: sqlServer.properties.fullyQualifiedDomainName
            }
            {
              name: 'AZURE_DB_PORT'
              value: '1433'
            }
            {
              name: 'AZURE_DB_NAME'
              value: sqlDb.name
            }
            {
              name: 'AZURE_DB_USER'
              value: sqlAdminLogin
            }
            {
              name: 'AZURE_DB_PASSWORD'
              secretRef: 'db-password'
            }
            {
              name: 'ENTRA_CLIENT_ID'
              value: entraClientId
            }
            {
              name: 'ENTRA_TENANT_ID'
              value: entraTenantId
            }
            {
              name: 'ENTRA_REDIRECT_URI'
              value: entraRedirectUri
            }
            {
              name: 'ENTRA_CLIENT_SECRET'
              secretRef: 'entra-client-secret'
            }
            {
              name: 'ENTRA_ALLOWED_UPNS'
              value: entraAllowedUpns
            }
          ], appInsightsConnectionString != '' ? [{
            name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
            secretRef: 'appinsights-connection-string'
          }] : [])
          resources: {
            cpu: json('0.5')
            memory: '1.0Gi'
          }
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

// ----------------------------------------------------------------------------
// Container App: Websocket Listener (always-on, run_ws_listener.py)
// No external ingress - this is a background worker, not a web endpoint.
// ----------------------------------------------------------------------------
resource wsListenerApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: wsListenerAppName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    managedEnvironmentId: cae.id
    configuration: {
      registries: [
        {
          server: acr.properties.loginServer
          username: acr.listCredentials().username
          passwordSecretRef: 'acr-password'
        }
      ]
      secrets: concat([
        {
          name: 'acr-password'
          value: acr.listCredentials().passwords[0].value
        }
        {
          name: 'db-password'
          value: sqlAdminPassword
        }
        {
          name: 'telegram-bot-token'
          value: telegramBotToken
        }
        {
          name: 'telegram-chat-id'
          value: telegramChatId
        }
        {
          name: 'upstox-sandbox-token'
          value: upstoxSandboxAccessToken
        }
      ], appInsightsConnectionString != '' ? [{
        name: 'appinsights-connection-string'
        value: appInsightsConnectionString
      }] : [])
    }
    template: {
      containers: [
        {
          name: 'ws-listener'
          image: containerImage
          command: [
            'python'
            'run_ws_listener.py'
          ]
          env: concat([
            {
              name: 'AZURE_DB_HOST'
              value: sqlServer.properties.fullyQualifiedDomainName
            }
            {
              name: 'AZURE_DB_PORT'
              value: '1433'
            }
            {
              name: 'AZURE_DB_NAME'
              value: sqlDb.name
            }
            {
              name: 'AZURE_DB_USER'
              value: sqlAdminLogin
            }
            {
              name: 'AZURE_DB_PASSWORD'
              secretRef: 'db-password'
            }
            {
              // Sep 6: ws_listener.py's ops alert (token expiry / connect
              // failure) had no way to actually send until these were
              // added here -- it was silently logging "Telegram not
              // configured" in production the whole time, exactly the
              // kind of silent failure this alert exists to catch.
              name: 'TELEGRAM_BOT_TOKEN'
              secretRef: 'telegram-bot-token'
            }
            {
              name: 'TELEGRAM_CHAT_ID'
              secretRef: 'telegram-chat-id'
            }
            {
              // Sep 7: the fast breakout-watch thread (see
              // strategy_engine.py's ARBITRAGE_* docstring / ws_listener.py's
              // _act_on_breakout) opens paper trades via its OWN PaperTrader
              // instance in THIS container -- it needs its own sandbox token,
              // separate from the scanner job's. Missing here caused a real,
              // observed miss: a PNB.NS breakout was correctly detected and
              // triggered live by the fast watch, but silently failed to
              // open (empty/expired token), and only opened ~5 min later
              // through the slower normal-scan fallback at a worse price.
              name: 'UPSTOX_SANDBOX_ACCESS_TOKEN'
              secretRef: 'upstox-sandbox-token'
            }
          ], appInsightsConnectionString != '' ? [{
            name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
            secretRef: 'appinsights-connection-string'
          }] : [])
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

// ----------------------------------------------------------------------------
// Container App Jobs: Scheduled Scan — one per timeframe group (enterprise
// Phase 2, 2026-09-21).
//
// Was a single Job whose cron fired every 5 min and, inside one process,
// looped through ALL 6 timeframes sequentially behind one global run-lock —
// a slow 5-Minute scan (the one most likely to be slow, since it's the most
// frequent) delayed or starved every other timeframe in the same execution,
// with no way for e.g. 1-Hour to run independently. Split into 4 Jobs, each
// with its own cron matching its actual cadence (see run_single_scan.py's
// GROUPS/_is_scan_due for where these minute values come from — IST
// minutes converted to the UTC cron field, IST = UTC+5:30) and its own
// run-lock key (single_scan_<group>, see run_single_scan.py) — a slow
// 1-Hour cycle can no longer touch 5-Minute's schedule at all.
//
// NOTE: parallelism/replicaCompletionCount=1 limits replicas WITHIN one
// execution - it does NOT stop a new cron trigger from firing while a
// previous execution is still running. The overlap fix is the per-group
// app-level lock in run_single_scan.py, not infra.
// ----------------------------------------------------------------------------
var scanJobGroups = [
  {
    suffix: '5min'
    group: '5min'
    cron: '*/5 3-10 * * 1-5'      // every 5 min, unchanged from the original single job
  }
  {
    suffix: '15min'
    group: '15min'
    cron: '0,15,30,45 3-10 * * 1-5'  // IST minute in {0,15,30,45} -- see _is_scan_due
  }
  {
    suffix: '1hour'
    group: '1hour'
    cron: '35 3-10 * * 1-5'       // IST minute==5 -> UTC minute 35
  }
  {
    suffix: 'eod'
    group: 'eod'
    // Fires at all three EOD trigger times (1 Day@UTC10:00, 1 Week@10:05
    // Fri-only, 1 Month@10:10 last-trading-day-only) every weekday; the
    // existing _is_scan_due() logic inside this same process gates each
    // one down to its real day/date condition -- cron alone can't express
    // "last trading day of the month", so that check stays in Python.
    cron: '0,5,10 10 * * 1-5'
  }
]

resource scanJobs 'Microsoft.App/jobs@2023-05-01' = [for g in scanJobGroups: {
  name: '${scanJobName}-${g.suffix}'
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    environmentId: cae.id
    configuration: {
      triggerType: 'Schedule'
      scheduleTriggerConfig: {
        cronExpression: g.cron
        parallelism: 1
        replicaCompletionCount: 1
      }
      // 1800s (30 min), not the original 300s (5 min): the real scan cycle
      // (507 instruments) legitimately exceeds 300s once monitor_open()
      // runs on top of the scan itself (5min group only) - this caused
      // every execution to fail Aug 10-12 until someone fixed it directly
      // on the live resource. Matching that fix here so a future redeploy
      // of this file doesn't silently revert it. Kept uniform across all
      // 4 groups for simplicity, even though 15min/1hour/eod need far less.
      replicaTimeout: 1800
      replicaRetryLimit: 1
      registries: [
        {
          server: acr.properties.loginServer
          username: acr.listCredentials().username
          passwordSecretRef: 'acr-password'
        }
      ]
      secrets: concat([
        {
          name: 'acr-password'
          value: acr.listCredentials().passwords[0].value
        }
        {
          name: 'db-password'
          value: sqlAdminPassword
        }
        {
          name: 'telegram-bot-token'
          value: telegramBotToken
        }
        {
          name: 'telegram-chat-id'
          value: telegramChatId
        }
        {
          name: 'upstox-sandbox-token'
          value: upstoxSandboxAccessToken
        }
      ], appInsightsConnectionString != '' ? [{
        name: 'appinsights-connection-string'
        value: appInsightsConnectionString
      }] : [])
    }
    template: {
      containers: [
        {
          name: 'scanner'
          image: containerImage
          command: [
            'python'
            'run_single_scan.py'
            '--group'
            g.group
          ]
          env: concat([
            {
              name: 'AZURE_DB_HOST'
              value: sqlServer.properties.fullyQualifiedDomainName
            }
            {
              name: 'AZURE_DB_PORT'
              value: '1433'
            }
            {
              name: 'AZURE_DB_NAME'
              value: sqlDb.name
            }
            {
              name: 'AZURE_DB_USER'
              value: sqlAdminLogin
            }
            {
              name: 'AZURE_DB_PASSWORD'
              secretRef: 'db-password'
            }
            {
              name: 'TELEGRAM_BOT_TOKEN'
              secretRef: 'telegram-bot-token'
            }
            {
              name: 'TELEGRAM_CHAT_ID'
              secretRef: 'telegram-chat-id'
            }
            {
              name: 'UPSTOX_SANDBOX_ACCESS_TOKEN'
              secretRef: 'upstox-sandbox-token'
            }
          ], appInsightsConnectionString != '' ? [{
            name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
            secretRef: 'appinsights-connection-string'
          }] : [])
          resources: {
            cpu: json('0.5')
            memory: '1.0Gi'
          }
        }
      ]
    }
  }
}]

// ----------------------------------------------------------------------------
// Key Vault access — access policies instead of RBAC role assignments.
// This stays entirely within the Microsoft.KeyVault namespace (which
// Contributor can write), avoiding Microsoft.Authorization/roleAssignments
// entirely - that action requires Owner/User Access Administrator, which
// this account does not have at this scope.
// ----------------------------------------------------------------------------
// Bicep won't allow looping a resource-collection reference (scanJobs)
// through a variable or a for-expression indirection when the loop body
// needs a runtime-only property like .identity.principalId (BCP178/
// BCP182/BCP144, all tried and rejected) -- so this is spelled out
// explicitly per index instead of genuinely DRY. scanJobGroups has
// exactly 4 entries (enterprise Phase 2: one identity per timeframe
// group, was a single scanJob identity) -- keep this in sync with that
// array's length if it's ever changed.
resource kvAccessPolicies 'Microsoft.KeyVault/vaults/accessPolicies@2023-07-01' = {
  parent: kv
  name: 'add'
  properties: {
    accessPolicies: [
      {
        tenantId: subscription().tenantId
        objectId: dashboardApp.identity.principalId
        permissions: {
          secrets: [
            'get'
            'list'
          ]
        }
      }
      {
        tenantId: subscription().tenantId
        objectId: wsListenerApp.identity.principalId
        permissions: {
          secrets: [
            'get'
            'list'
          ]
        }
      }
      {
        tenantId: subscription().tenantId
        objectId: scanJobs[0].identity.principalId  // scanJobGroups[0] = 5min
        permissions: {
          secrets: [
            'get'
            'list'
          ]
        }
      }
      {
        tenantId: subscription().tenantId
        objectId: scanJobs[1].identity.principalId  // scanJobGroups[1] = 15min
        permissions: {
          secrets: [
            'get'
            'list'
          ]
        }
      }
      {
        tenantId: subscription().tenantId
        objectId: scanJobs[2].identity.principalId  // scanJobGroups[2] = 1hour
        permissions: {
          secrets: [
            'get'
            'list'
          ]
        }
      }
      {
        tenantId: subscription().tenantId
        objectId: scanJobs[3].identity.principalId  // scanJobGroups[3] = eod
        permissions: {
          secrets: [
            'get'
            'list'
          ]
        }
      }
    ]
  }
}

// ----------------------------------------------------------------------------
// Outputs
// ----------------------------------------------------------------------------
output acrLoginServer string = acr.properties.loginServer
output sqlServerFqdn string = sqlServer.properties.fullyQualifiedDomainName
output sqlDatabaseName string = sqlDb.name
output dashboardUrl string = 'https://${dashboardApp.properties.configuration.ingress.fqdn}'
output keyVaultName string = kv.name
output keyVaultUri string = kv.properties.vaultUri
