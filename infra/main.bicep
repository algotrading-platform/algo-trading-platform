// ============================================================================
// Algo Trading Platform — Infrastructure (AGT PlayGround subscription)
// ============================================================================
// Deploys: ACR, Log Analytics, Container Apps Environment, one always-on
// Container App (dashboard), one Container App Job (scheduled scan, replaces
// run_scheduler.py), one always-on Container App (websocket listener),
// Azure SQL Server + Database, and Key Vault.
//
// Deploy with (resource group scope):
//   az deployment group create -g <rg-name> -f main.bicep -p main.parameters.json --parameters sqlAdminPassword=<typed-at-prompt>
//
// NOTE: as of this version, telegramBotToken/telegramChatId/upstoxSandboxAccessToken
// default to empty strings so a deploy without them is a safe no-op (won't wipe
// the live values) - but if you actually need to CHANGE any of those three,
// pass the real value explicitly, e.g. --parameters telegramBotToken=<value>,
// or Container Apps will overwrite the current secret with an empty one.
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

@description('Cron expression (UTC) for the scheduled scan job. Default covers ~9:15-15:30 IST, Mon-Fri, every 5 min. Adjust as needed.')
param scanCronExpression string = '*/5 3-10 * * 1-5'

@description('Azure SQL SKU tier')
param sqlSkuName string = 'Basic'

@description('Azure SQL SKU tier name')
param sqlSkuTier string = 'Basic'

@description('Object ID of the account running this deployment (cgummunur@ariqt.com) - needed to grant secret-write access on the Key Vault for this deployment to succeed.')
param deployerObjectId string = 'cf35cfc5-89f9-43fb-9cf0-7fa0fcd081fe'

@description('Telegram bot token for scan-cycle alerts. Added directly to the live scanJob resource outside this file at some point after initial deploy - added here now to keep main.bicep an accurate description of what is actually deployed. Empty default is intentional: pass the real value at deploy time, never commit it.')
@secure()
param telegramBotToken string = ''

@description('Telegram chat ID for scan-cycle alerts. Same provenance/rationale as telegramBotToken above.')
@secure()
param telegramChatId string = ''

@description('Upstox sandbox access token, used for validating live signals against Upstox before using the production Upstox flow. Same provenance/rationale as telegramBotToken above.')
@secure()
param upstoxSandboxAccessToken string = ''

@description('Entra ID (Azure AD) app registration client ID for dashboard sign-in - the "Algo-Trading" single-tenant app registration in the ariqt.com tenant.')
param entraClientId string = '6064f100-172f-4fc1-9798-b4e493e44717'

@description('Entra ID tenant ID (ariqt.com).')
param entraTenantId string = '8f6bd982-92c3-4de0-985d-0e287c55e379'

@description('OAuth redirect URI for the dashboard auth-code flow - must exactly match a registered Web redirect URI on the app registration (no trailing slash).')
param entraRedirectUri string = 'https://algo-dashboard.lemonglacier-23c89c18.westeurope.azurecontainerapps.io'

@description('Client secret for the Entra app registration. Same provenance/rationale as telegramBotToken above: empty default so a deploy without it is a safe no-op, pass the real value at deploy time only, never commit it.')
@secure()
param entraClientSecret string = ''

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
      secrets: [
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
      ]
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
          env: [
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
          ]
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
      secrets: [
        {
          name: 'acr-password'
          value: acr.listCredentials().passwords[0].value
        }
        {
          name: 'db-password'
          value: sqlAdminPassword
        }
      ]
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
          env: [
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
          ]
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
// Container App Job: Scheduled Scan (replaces run_scheduler.py entirely)
// NOTE: parallelism/replicaCompletionCount=1 limits replicas WITHIN one
// execution - it does NOT stop a new cron trigger from firing while a
// previous execution is still running. The overlap fix still needs an
// app-level lock in run_single_scan.py (Phase 4 code change, not infra).
// ----------------------------------------------------------------------------
resource scanJob 'Microsoft.App/jobs@2023-05-01' = {
  name: scanJobName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    environmentId: cae.id
    configuration: {
      triggerType: 'Schedule'
      scheduleTriggerConfig: {
        cronExpression: scanCronExpression
        parallelism: 1
        replicaCompletionCount: 1
      }
      // 1800s (30 min), not the original 300s (5 min): the real scan cycle
      // (507 instruments, RSI+MA and Volume Spike together) legitimately
      // exceeds 300s once monitor_open() runs on top of the scan itself -
      // this caused every execution to fail Aug 10-12 until someone fixed
      // it directly on the live resource. Matching that fix here so a
      // future redeploy of this file doesn't silently revert it.
      replicaTimeout: 1800
      replicaRetryLimit: 1
      registries: [
        {
          server: acr.properties.loginServer
          username: acr.listCredentials().username
          passwordSecretRef: 'acr-password'
        }
      ]
      secrets: [
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
      ]
    }
    template: {
      containers: [
        {
          name: 'scanner'
          image: containerImage
          command: [
            'python'
            'run_single_scan.py'
          ]
          env: [
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
          ]
          resources: {
            cpu: json('0.5')
            memory: '1.0Gi'
          }
        }
      ]
    }
  }
}

// ----------------------------------------------------------------------------
// Key Vault access — access policies instead of RBAC role assignments.
// This stays entirely within the Microsoft.KeyVault namespace (which
// Contributor can write), avoiding Microsoft.Authorization/roleAssignments
// entirely - that action requires Owner/User Access Administrator, which
// this account does not have at this scope.
// ----------------------------------------------------------------------------
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
        objectId: scanJob.identity.principalId
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