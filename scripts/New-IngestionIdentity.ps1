#requires -Version 7.0
<#
.SYNOPSIS
Plans or applies admin-owned ingestion identity and narrowly scoped RBAC setup.
.DESCRIPTION
Defaults to read-only preview. Requires an existing Azure CLI session in the
explicit tenant/subscription. -Apply is required for changes; -WhatIf also works.
Never logs in, switches subscriptions, creates secrets, or provisions resources.
Federation/service connections are configured separately using Azure DevOps' exact values.
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory)][Guid]$TenantId,
    [Parameter(Mandatory)][Guid]$SubscriptionId,
    [Parameter(Mandatory)][string]$SearchServiceResourceId,
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9][a-z0-9-]{1,126}[a-z0-9]$')][string]$IndexName,
    [Parameter(Mandatory)][string]$OpenAIResourceId,
    [Parameter(Mandatory)][string]$StateContainerResourceId,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._ -]{2,100}$')]
    [string]$AppDisplayName = 'ado-markdown-ingestion',
    [Guid]$ExistingAppId = [Guid]::Empty,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._ -]{2,100}$')]
    [string]$SchemaReaderRoleName = 'ADO Markdown Index Schema Reader',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$tenant = $TenantId.ToString('D')
$subscription = $SubscriptionId.ToString('D')
if ($TenantId -eq [Guid]::Empty -or $SubscriptionId -eq [Guid]::Empty) {
    throw 'TenantId and SubscriptionId must be explicit, nonzero GUIDs.'
}
if ($IndexName.Contains('--')) { throw 'IndexName cannot contain consecutive hyphens.' }
[void](Get-Command az -ErrorAction Stop)

function Invoke-AzJson {
    param([Parameter(Mandatory)][string[]]$Arguments)
    $output = @(& az @Arguments --only-show-errors --output json 2>&1)
    $code = $LASTEXITCODE
    $operation = ($Arguments | Select-Object -First 3) -join ' '
    if ($code -ne 0) {
        throw "Azure CLI operation '$operation' failed (exit $code). Check directory/RBAC permissions and rerun. No automatic rollback or privilege escalation is performed."
    }
    try {
        $text = ($output | ForEach-Object { $_.ToString() }) -join "`n"
        return ConvertFrom-Json -InputObject $text -AsHashtable -Depth 64 -ErrorAction Stop
    }
    catch [System.ArgumentException] {
        throw "Azure CLI operation '$operation' returned invalid JSON."
    }
}

function Assert-ResourceScope {
    param([string]$Id, [string]$Suffix)
    $pattern = '^/subscriptions/(?<sub>[0-9a-f-]{36})/resourceGroups/(?<rg>[^/]+)/providers/' + $Suffix + '$'
    $match = [regex]::Match($Id, $pattern, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
    if (-not $match.Success -or $match.Groups['sub'].Value -ine $subscription) {
        throw 'Resource IDs must be exact existing resource scopes in the explicitly selected subscription.'
    }
    return "/subscriptions/$subscription/resourceGroups/$($match.Groups['rg'].Value)"
}

$SearchServiceResourceId = $SearchServiceResourceId.TrimEnd('/')
$OpenAIResourceId = $OpenAIResourceId.TrimEnd('/')
$StateContainerResourceId = $StateContainerResourceId.TrimEnd('/')
$searchGroup = Assert-ResourceScope $SearchServiceResourceId 'Microsoft\.Search/searchServices/[^/]+'
[void](Assert-ResourceScope $OpenAIResourceId 'Microsoft\.CognitiveServices/accounts/[^/]+')
[void](Assert-ResourceScope $StateContainerResourceId 'Microsoft\.Storage/storageAccounts/[a-z0-9]{3,24}/blobServices/default/containers/[a-z0-9][a-z0-9-]{1,61}[a-z0-9]')
$indexScope = "$SearchServiceResourceId/indexes/$IndexName"

$account = Invoke-AzJson @('account', 'show')
if ($account.tenantId -ine $tenant -or $account.id -ine $subscription -or $account.state -ne 'Enabled') {
    throw 'The existing Azure CLI session must select the explicit tenant and Enabled subscription. Log in/select context yourself; this script never changes it.'
}
if ($account.environmentName -ne 'AzureCloud') {
    throw 'This worker setup targets AzureCloud; a different cloud requires a reviewed configuration.'
}

foreach ($resource in @(
    @{ Id = $SearchServiceResourceId; Type = 'Microsoft.Search/searchServices' },
    @{ Id = $OpenAIResourceId; Type = 'Microsoft.CognitiveServices/accounts' },
    @{ Id = $StateContainerResourceId; Type = 'Microsoft.Storage/storageAccounts/blobServices/containers' }
)) {
    $found = Invoke-AzJson @('resource', 'show', '--ids', $resource.Id, '--subscription', $subscription)
    if ($found.id -ine $resource.Id -or $found.type -ine $resource.Type) {
        throw 'A supplied resource ID did not resolve to the expected existing resource type.'
    }
    if ($resource.Type -eq 'Microsoft.CognitiveServices/accounts' -and $found.kind -notin @('OpenAI', 'AIServices')) {
        throw 'The embedding resource must be an OpenAI or AIServices account.'
    }
    if ($resource.Type -like '*containers' -and $found.properties.publicAccess -notin @($null, 'None')) {
        throw 'The existing state container must not allow public access.'
    }
}

function Get-Role {
    param([string]$Name)
    $roles = @(Invoke-AzJson @('role', 'definition', 'list', '--name', $Name, '--subscription', $subscription))
    $exact = @($roles | Where-Object { $_.roleName -ceq $Name })
    if ($exact.Count -gt 1) { throw "Ambiguous role definition '$Name'; resolve it before continuing." }
    if ($exact.Count -eq 1) { return $exact[0] }
    return $null
}

function Assert-SchemaRole {
    param([hashtable]$Role)
    $permissions = @($Role.permissions)
    if (
        -not $Role.id -or $Role.roleType -ne 'CustomRole' -or $permissions.Count -ne 1 -or
        @($permissions[0].actions).Count -ne 1 -or
        $permissions[0].actions[0] -ine 'Microsoft.Search/searchServices/indexes/read' -or
        @($permissions[0].notActions).Count -ne 0 -or
        @($permissions[0].dataActions).Count -ne 0 -or
        @($permissions[0].notDataActions).Count -ne 0 -or
        @($Role.assignableScopes).Count -ne 1 -or $Role.assignableScopes[0] -ine $searchGroup
    ) {
        throw 'The existing schema-reader role differs from the exact read-only definition/assignable resource group. Use an owner-reviewed distinct role name; this script will not alter it.'
    }
}

$schemaRole = Get-Role $SchemaReaderRoleName
if ($null -ne $schemaRole) { Assert-SchemaRole $schemaRole }
$assignments = @(
    @{ Role = 'Search Index Data Contributor'; Scope = $indexScope },
    @{ Role = 'Cognitive Services OpenAI User'; Scope = $OpenAIResourceId },
    @{ Role = 'Storage Blob Data Contributor'; Scope = $StateContainerResourceId }
)
foreach ($assignment in $assignments) {
    $definition = Get-Role $assignment.Role
    if ($null -eq $definition -or $definition.roleType -ne 'BuiltInRole' -or -not $definition.id) {
        throw "Required built-in role '$($assignment.Role)' was not found."
    }
    $assignment.DefinitionId = $definition.id
}
$assignments += @{ Role = $SchemaReaderRoleName; Scope = $SearchServiceResourceId; DefinitionId = $null }

$app = $null
if ($ExistingAppId -ne [Guid]::Empty) {
    $app = Invoke-AzJson @('ad', 'app', 'show', '--id', $ExistingAppId.ToString('D'))
}
else {
    $apps = @(Invoke-AzJson @('ad', 'app', 'list', '--display-name', $AppDisplayName))
    $exact = @($apps | Where-Object { $_.displayName -ceq $AppDisplayName })
    if ($exact.Count -gt 1) {
        throw 'Multiple app registrations have this exact display name. Supply the intended ExistingAppId; no identity was selected automatically.'
    }
    if ($exact.Count -eq 1) { $app = $exact[0] }
}
if ($null -ne $app -and $app.signInAudience -ne 'AzureADMyOrg') {
    throw 'Only an explicitly reviewed single-tenant app registration can be reused.'
}

$plan = [ordered]@{
    mode = 'preview'
    tenant_id = $tenant
    subscription_id = $subscription
    app_display_name = $AppDisplayName
    existing_client_id = $(if ($null -ne $app) { $app.appId } else { $null })
    create_app = ($null -eq $app)
    create_schema_reader_role = ($null -eq $schemaRole)
    schema_role_assignable_scope = $searchGroup
    assignments = @($assignments | ForEach-Object { @{ role = $_.Role; scope = $_.Scope } })
    federation = 'Configure separately from the exact Azure DevOps service connection issuer/subject/audience'
    creates_secrets = $false
}
Write-Host "Target tenant: $tenant; subscription: $subscription"
Write-Host "App: $AppDisplayName; index: $IndexName; four scoped role assignments."
if (-not $Apply -or -not $PSCmdlet.ShouldProcess(
    "$AppDisplayName in tenant $tenant; four explicitly scoped role assignments",
    'Create/reuse single-tenant app and service principal; create/reuse schema-read role; assign permissions'
)) {
    $plan | ConvertTo-Json -Depth 8
    return
}

if ($null -eq $app) {
    $app = Invoke-AzJson @('ad', 'app', 'create', '--display-name', $AppDisplayName, '--sign-in-audience', 'AzureADMyOrg')
}
$clientId = [Guid]::Parse($app.appId).ToString('D')
$applicationObjectId = [Guid]::Parse($app.id).ToString('D')
Write-Host "Application client ID: $clientId"
$principals = @(Invoke-AzJson @('ad', 'sp', 'list', '--filter', "appId eq '$clientId'"))
$principals = @($principals | Where-Object { $_.appId -ieq $clientId })
if ($principals.Count -gt 1) { throw 'Multiple service principals matched the client ID; manual review required.' }
$principal = if ($principals.Count -eq 1) { $principals[0] } else {
    Invoke-AzJson @('ad', 'sp', 'create', '--id', $clientId)
}
if (
    $principal.appId -ine $clientId -or $principal.servicePrincipalType -ne 'Application' -or
    $principal.appOwnerOrganizationId -ine $tenant
) { throw 'Service principal ownership/type does not match the selected single-tenant application.' }
$principalId = [Guid]::Parse($principal.id).ToString('D')
Write-Host "Service principal object ID: $principalId"

if ($null -eq $schemaRole) {
    $temporary = [System.IO.Path]::GetTempFileName()
    try {
        @{
            Name = $SchemaReaderRoleName
            IsCustom = $true
            Description = 'Read Search index definitions only. No schema writes, keys, or document actions.'
            Actions = @('Microsoft.Search/searchServices/indexes/read')
            NotActions = @()
            DataActions = @()
            NotDataActions = @()
            AssignableScopes = @($searchGroup)
        } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporary -Encoding utf8NoBOM
        $schemaRole = Invoke-AzJson @('role', 'definition', 'create', '--role-definition', "@$temporary", '--subscription', $subscription)
    }
    finally { Remove-Item -LiteralPath $temporary -Force }
    Assert-SchemaRole $schemaRole
}
$assignments[-1].DefinitionId = $schemaRole.id
$results = @()
foreach ($assignment in $assignments) {
    $existing = @(Invoke-AzJson @(
        'role', 'assignment', 'list',         '--scope', $assignment.Scope,
        '--fill-principal-name', 'false', '--subscription', $subscription
    ))
    $matches = @($existing | Where-Object {
        $_.principalId -ieq $principalId -and $_.scope -ieq $assignment.Scope -and
        $_.roleDefinitionId -ieq $assignment.DefinitionId
    })
    if ($matches.Count -gt 1 -or @($matches | Where-Object { $_.condition }).Count -gt 0) {
        throw 'Duplicate or conditional role assignment requires owner review; no replacement was attempted.'
    }
    $created = $false
    if ($matches.Count -eq 0) {
        $granted = Invoke-AzJson @(
            'role', 'assignment', 'create', '--assignee-object-id', $principalId,
            '--assignee-principal-type', 'ServicePrincipal', '--role', $assignment.DefinitionId,
            '--scope', $assignment.Scope, '--subscription', $subscription
        )
        if (
            $granted.principalId -ine $principalId -or $granted.scope -ine $assignment.Scope -or
            $granted.roleDefinitionId -ine $assignment.DefinitionId
        ) { throw 'Role assignment response did not match the intended principal, role and scope.' }
        $created = $true
    }
    $results += @{ role = $assignment.Role; scope = $assignment.Scope; created = $created }
}
[ordered]@{
    mode = 'applied'
    tenant_id = $tenant
    subscription_id = $subscription
    client_id = $clientId
    application_object_id = $applicationObjectId
    service_principal_object_id = $principalId
    assignments = $results
    federation = 'Not configured: create the WIF service connection draft, then add its exact federated credential'
    secrets_created = $false
    runtime_access_verified = $false
} | ConvertTo-Json -Depth 8
