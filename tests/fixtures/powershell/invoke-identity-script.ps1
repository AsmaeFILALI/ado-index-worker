# Test-only Azure CLI emulator. There is deliberately no fallback to a real CLI.
param(
    [Parameter(Mandatory)][string]$SetupScript,
    [Parameter(Mandatory)][string]$ScenarioFile,
    [Parameter(Mandatory)][string]$ResultFile
)
$ErrorActionPreference = 'Stop'
$global:Scenario = Get-Content -Raw -LiteralPath $ScenarioFile | ConvertFrom-Json -AsHashtable
$global:Calls = [System.Collections.Generic.List[object]]::new()
$global:Results = [System.Collections.Generic.List[object]]::new()

function Get-FakeArgument {
    param([string[]]$Values, [string]$Name)
    $position = [Array]::IndexOf($Values, $Name)
    if ($position -lt 0 -or $position + 1 -ge $Values.Count) { throw "Missing fake argument $Name" }
    return $Values[$position + 1]
}
function Emit-FakeJson {
    param([object]$Value)
    ConvertTo-Json -InputObject $Value -Depth 30 -Compress
}
function global:az {
    $values = @($args | ForEach-Object { [string]$_ })
    $global:Calls.Add($values)
    $global:LASTEXITCODE = 0
    $operation = ($values | Select-Object -First 3) -join ' '
    if ($Scenario.fail_operation -and $operation.StartsWith($Scenario.fail_operation)) {
        $global:LASTEXITCODE = 42
        Write-Output 'Synthetic denied operation; no real service was called.'
        return
    }
    if ($Scenario.invalid_json) { Write-Output 'synthetic-invalid-json'; return }
    if ($values[0] -eq 'account') { Emit-FakeJson $Scenario.account; return }
    if ($operation -eq 'resource show --ids') {
        $id = Get-FakeArgument $values '--ids'
        if (-not $Scenario.resources.ContainsKey($id)) { throw 'Unexpected fake resource lookup' }
        Emit-FakeJson $Scenario.resources[$id]
        return
    }
    if ($operation -eq 'role definition list') {
        $name = Get-FakeArgument $values '--name'
        Emit-FakeJson @($Scenario.roles | Where-Object { $_.roleName -ceq $name })
        return
    }
    if ($operation -eq 'role definition create') {
        $path = (Get-FakeArgument $values '--role-definition').Substring(1)
        $definition = Get-Content -Raw -LiteralPath $path | ConvertFrom-Json -AsHashtable
        $role = @{
            id = "/subscriptions/$($Scenario.account.id)/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000099"
            roleName = $definition.Name
            roleType = 'CustomRole'
            assignableScopes = @($definition.AssignableScopes)
            permissions = @(@{
                actions = @($definition.Actions)
                notActions = @($definition.NotActions)
                dataActions = @($definition.DataActions)
                notDataActions = @($definition.NotDataActions)
            })
        }
        $Scenario.roles += @($role)
        Emit-FakeJson $role
        return
    }
    if ($operation -eq 'ad app list') { Emit-FakeJson $Scenario.apps; return }
    if ($operation -eq 'ad app show') {
        $id = Get-FakeArgument $values '--id'
        $app = @($Scenario.apps | Where-Object { $_.appId -eq $id })
        if ($app.Count -ne 1) { throw 'Unknown fake ExistingAppId' }
        Emit-FakeJson $app[0]
        return
    }
    if ($operation -eq 'ad app create') {
        $app = @{
            appId = '00000000-0000-0000-0000-000000000010'
            id = '00000000-0000-0000-0000-000000000011'
            displayName = Get-FakeArgument $values '--display-name'
            signInAudience = Get-FakeArgument $values '--sign-in-audience'
        }
        $Scenario.apps += @($app)
        Emit-FakeJson $app
        return
    }
    if ($operation -eq 'ad sp list') {
        $filter = Get-FakeArgument $values '--filter'
        Emit-FakeJson @($Scenario.principals | Where-Object { $filter -eq "appId eq '$($_.appId)'" })
        return
    }
    if ($operation -eq 'ad sp create') {
        $principal = @{
            appId = Get-FakeArgument $values '--id'
            id = '00000000-0000-0000-0000-000000000012'
            servicePrincipalType = 'Application'
            appOwnerOrganizationId = $Scenario.account.tenantId
        }
        $Scenario.principals += @($principal)
        Emit-FakeJson $principal
        return
    }
    if ($operation -eq 'role assignment list') {
        $scope = Get-FakeArgument $values '--scope'
        Emit-FakeJson @($Scenario.assignments | Where-Object { $_.scope -ieq $scope })
        return
    }
    if ($operation -eq 'role assignment create') {
        if ((Get-FakeArgument $values '--assignee-principal-type') -ne 'ServicePrincipal') {
            throw 'The setup must use a service principal object ID explicitly'
        }
        $assignment = @{
            principalId = Get-FakeArgument $values '--assignee-object-id'
            roleDefinitionId = Get-FakeArgument $values '--role'
            scope = Get-FakeArgument $values '--scope'
            condition = $null
        }
        $Scenario.assignments += @($assignment)
        Emit-FakeJson $assignment
        return
    }
    throw "Unsupported fake az operation: $operation"
}
try {
    for ($index = 0; $index -lt $Scenario.repeat; $index++) {
        $parameters = $Scenario.parameters
        $output = @(& $SetupScript @parameters)
        $result = ($output -join "`n") | ConvertFrom-Json -AsHashtable
        $global:Results.Add($result)
    }
}
finally {
    @{
        results = @($global:Results.ToArray())
        calls = @($global:Calls.ToArray())
        state = $Scenario
    } | ConvertTo-Json -Depth 40 | Set-Content -LiteralPath $ResultFile -Encoding utf8NoBOM
}
